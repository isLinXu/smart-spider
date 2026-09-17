# coding=utf-8
"""授权浏览器交互剧本：打开 → 声明式步骤 → 滚动 → 抽链 → 同域 BFS。

仅用于用户有权访问的站点；配合 SiteCrawlPolicy，不做对抗式绕过。
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urljoin, urldefrag

from .block_signals import BlockSignal, BlockKind, classify_page_html
from .metrics import observe_browse_block, observe_browse_skip
from .site_policy import (
    HostRateLimiter,
    RobotsGate,
    SiteCrawlPolicy,
    host_of,
    polite_sleep,
)
from .tools import extract_links
from .url_policy import URLPolicy

PLAYBOOK_ACTIONS = frozenset({"wait_for", "click", "type", "scroll"})


@dataclass(frozen=True)
class PlaybookStep:
    """User-declared interaction. Selectors and typed text come from the operator."""

    action: str
    selector: str = ""
    text: str = ""
    timeout_ms: int = 10_000

    def __post_init__(self) -> None:
        action = str(self.action or "").strip().lower()
        if action == "type_text":
            action = "type"
        object.__setattr__(self, "action", action)
        if self.action not in PLAYBOOK_ACTIONS:
            raise ValueError(
                f"unsupported playbook action {self.action!r}; "
                f"allowed: {sorted(PLAYBOOK_ACTIONS)}"
            )
        if self.action in {"wait_for", "click", "type"} and not str(self.selector).strip():
            raise ValueError(f"{self.action} requires a CSS selector")
        if self.action == "type" and not str(self.text):
            raise ValueError("type requires non-empty text")
        if int(self.timeout_ms) <= 0:
            raise ValueError("timeout_ms must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Any) -> "PlaybookStep":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            raise TypeError("playbook step must be a mapping")
        action = raw.get("action") or raw.get("op") or ""
        return cls(
            action=str(action),
            selector=str(raw.get("selector") or raw.get("css") or ""),
            text=str(raw.get("text") or raw.get("value") or ""),
            timeout_ms=int(raw.get("timeout_ms") or raw.get("timeout") or 10_000),
        )


def parse_playbook_steps(raw: Any) -> tuple[PlaybookStep, ...]:
    if not raw:
        return ()
    if isinstance(raw, PlaybookStep):
        return (raw,)
    if not isinstance(raw, (list, tuple)):
        raise TypeError("steps must be a list")
    return tuple(PlaybookStep.from_mapping(item) for item in raw)


@dataclass
class DiscoveredLink:
    url: str
    text: str = ""
    depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BrowsePageResult:
    seed_url: str
    final_url: str = ""
    html: str = ""
    links: list[DiscoveredLink] = field(default_factory=list)
    block: Optional[BlockSignal] = None
    scrolled: int = 0
    skipped_by_policy: int = 0
    skipped_by_robots: int = 0
    steps_run: int = 0
    failed_step: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed_url": self.seed_url,
            "final_url": self.final_url,
            "link_count": len(self.links),
            "links": [item.to_dict() for item in self.links],
            "block": None if self.block is None else self.block.to_dict(),
            "scrolled": self.scrolled,
            "skipped_by_policy": self.skipped_by_policy,
            "skipped_by_robots": self.skipped_by_robots,
            "html_bytes": len(self.html or ""),
            "steps_run": self.steps_run,
            "failed_step": self.failed_step,
        }


class AuthorizedBrowsePlaybook:
    """受策略约束的浏览器浏览剧本。"""

    def __init__(
        self,
        controller,
        policy: Optional[SiteCrawlPolicy] = None,
        *,
        rate_limiter: Optional[HostRateLimiter] = None,
        robots: Optional[RobotsGate] = None,
        url_policy: Optional[URLPolicy] = None,
        steps: Sequence[PlaybookStep] = (),
    ):
        self.controller = controller
        self.policy = policy or SiteCrawlPolicy()
        self.rate_limiter = rate_limiter or HostRateLimiter(
            self.policy.requests_per_second
        )
        self.robots = robots or RobotsGate(
            user_agent=self.policy.robots_user_agent,
            enabled=self.policy.respect_robots,
        )
        self.url_policy = url_policy or URLPolicy(
            allow_private_hosts=self.policy.allow_private_hosts
        )
        self.steps = parse_playbook_steps(steps)

    def run(
        self,
        url: str,
        *,
        depth: int = 0,
        steps: Optional[Sequence[PlaybookStep]] = None,
    ) -> BrowsePageResult:
        result = BrowsePageResult(seed_url=url)
        if not self.policy.allows_url(url, url_policy=self.url_policy):
            result.block = BlockSignal(
                BlockKind.FORBIDDEN, False, "policy_denied_seed", 0
            )
            observe_browse_skip("policy_denied")
            observe_browse_block("forbidden")
            return result
        if not self.robots.allowed(url):
            result.block = BlockSignal(
                BlockKind.FORBIDDEN, False, "robots_disallow", 0
            )
            result.skipped_by_robots = 1
            observe_browse_skip("robots_skip")
            observe_browse_block("forbidden")
            return result

        self.rate_limiter.acquire(url)
        html = self.controller.navigate(url) or ""
        polite_sleep(self.policy.action_delay_seconds)

        active_steps = self.steps if steps is None else parse_playbook_steps(steps)
        if active_steps:
            ok, html, failed = self._run_steps(active_steps, html)
            result.steps_run = len(active_steps) if ok else max(result.steps_run, 0)
            if not ok:
                result.html = html
                result.final_url = getattr(self.controller, "current_url", "") or url
                result.failed_step = failed
                result.block = BlockSignal(
                    BlockKind.UNKNOWN, False, f"step_failed:{failed}", 0
                )
                observe_browse_block("unknown")
                return result
            result.steps_run = len(active_steps)

        scrolled = 0
        for _ in range(int(self.policy.scroll_passes)):
            ok, updated = self.controller.scroll()
            if ok and updated:
                html = updated
            scrolled += 1
            polite_sleep(self.policy.action_delay_seconds)
        result.scrolled = scrolled
        result.html = html
        result.final_url = getattr(self.controller, "current_url", "") or url
        signal = classify_page_html(html, status_code=200)
        result.block = signal
        if signal.kind != BlockKind.OK:
            observe_browse_block(signal.kind.value)
            if signal.kind == BlockKind.CHALLENGE:
                observe_browse_skip("challenge_dead")
            return result

        base = result.final_url or url
        raw_links = extract_links(html)
        seen: set[str] = set()
        discovered: list[DiscoveredLink] = []
        for item in raw_links:
            href = (item.get("href") or "").strip()
            if not href:
                continue
            absolute, _frag = urldefrag(urljoin(base, href))
            if absolute in seen:
                continue
            seen.add(absolute)
            if not self.policy.allows_url(absolute, url_policy=self.url_policy):
                result.skipped_by_policy += 1
                observe_browse_skip("policy_denied")
                continue
            if not self.robots.allowed(absolute):
                result.skipped_by_robots += 1
                observe_browse_skip("robots_skip")
                continue
            discovered.append(
                DiscoveredLink(
                    url=absolute,
                    text=str(item.get("text") or "")[:100],
                    depth=depth + 1,
                )
            )
            if len(discovered) >= self.policy.max_links_per_page:
                break
        result.links = discovered
        return result

    def crawl_bfs(
        self,
        seeds: Sequence[str],
        *,
        max_pages: Optional[int] = None,
        steps: Optional[Sequence[PlaybookStep]] = None,
    ) -> list[BrowsePageResult]:
        """Same-host BFS constrained by policy.max_depth and link budget."""
        queue: deque[tuple[str, int]] = deque()
        seen: set[str] = set()
        results: list[BrowsePageResult] = []
        for seed in seeds:
            item = str(seed or "").strip()
            if item:
                queue.append((item, 0))
        while queue:
            if max_pages is not None and len(results) >= int(max_pages):
                break
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            result = self.run(url, depth=depth, steps=steps)
            results.append(result)
            if result.block is not None and result.block.kind != BlockKind.OK:
                continue
            if depth >= self.policy.max_depth:
                continue
            for link in result.links:
                if link.url in seen:
                    continue
                if host_of(link.url) and host_of(url) and host_of(link.url) != host_of(url):
                    continue
                queue.append((link.url, link.depth))
        return results

    def _run_steps(
        self,
        steps: Sequence[PlaybookStep],
        html: str,
    ) -> tuple[bool, str, str]:
        current = html
        for step in steps:
            ok, current = self._apply_step(step, current)
            if not ok:
                label = step.action
                if step.selector:
                    label = f"{step.action}:{step.selector}"
                return False, current, label
            polite_sleep(self.policy.action_delay_seconds)
        return True, current, ""

    def _apply_step(self, step: PlaybookStep, html: str) -> tuple[bool, str]:
        controller = self.controller
        if step.action == "wait_for":
            waiter = getattr(controller, "wait_for_selector", None)
            if waiter is None:
                return False, html
            result = waiter(step.selector, timeout_ms=step.timeout_ms)
            return _unpack_step_result(result, html)
        if step.action == "click":
            result = controller.click(step.selector)
            return _unpack_step_result(result, html)
        if step.action == "type":
            result = controller.type_text(step.selector, step.text)
            return _unpack_step_result(result, html)
        if step.action == "scroll":
            result = controller.scroll()
            return _unpack_step_result(result, html)
        return False, html


def _unpack_step_result(result: Any, html: str) -> tuple[bool, str]:
    if isinstance(result, tuple):
        success = bool(result[0])
        updated = result[1] if len(result) > 1 and result[1] else html
        return success, updated
    if isinstance(result, str):
        return True, result or html
    return bool(result), html


def enqueue_discovered_links(
    queue,
    links: Sequence[DiscoveredLink],
    *,
    policy: SiteCrawlPolicy,
    kind: str = "authorized_browse",
    max_attempts: int = 3,
    extra_payload: Optional[dict[str, Any]] = None,
) -> list[str]:
    """将未超深的链接作为后续 authorized_browse 任务入队。"""
    task_ids: list[str] = []
    extra = dict(extra_payload or {})
    for link in links:
        if link.depth > policy.max_depth:
            continue
        payload = {
            "url": link.url,
            "depth": link.depth,
            "policy": policy.to_dict(),
            **extra,
        }
        record = queue.enqueue(
            kind,
            payload,
            max_attempts=max_attempts,
        )
        task_ids.append(record.task_id)
    return task_ids
