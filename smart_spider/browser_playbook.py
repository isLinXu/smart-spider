# coding=utf-8
"""授权浏览器交互剧本：打开 → 礼貌等待 → 滚动 → 抽链。

仅用于用户有权访问的站点；配合 SiteCrawlPolicy，不做对抗式绕过。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence
from urllib.parse import urljoin, urldefrag

from .block_signals import BlockSignal, BlockKind, classify_page_html
from .site_policy import (
    HostRateLimiter,
    RobotsGate,
    SiteCrawlPolicy,
    host_of,
    polite_sleep,
)
from .tools import extract_links
from .url_policy import URLPolicy


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

    def run(self, url: str, *, depth: int = 0) -> BrowsePageResult:
        result = BrowsePageResult(seed_url=url)
        if not self.policy.allows_url(url, url_policy=self.url_policy):
            result.block = BlockSignal(
                BlockKind.FORBIDDEN, False, "policy_denied_seed", 0
            )
            return result
        if not self.robots.allowed(url):
            result.block = BlockSignal(
                BlockKind.FORBIDDEN, False, "robots_disallow", 0
            )
            result.skipped_by_robots = 1
            return result

        self.rate_limiter.acquire(url)
        html = self.controller.navigate(url) or ""
        polite_sleep(self.policy.action_delay_seconds)
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
                continue
            if not self.robots.allowed(absolute):
                result.skipped_by_robots += 1
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


def enqueue_discovered_links(
    queue,
    links: Sequence[DiscoveredLink],
    *,
    policy: SiteCrawlPolicy,
    kind: str = "authorized_browse",
    max_attempts: int = 3,
) -> list[str]:
    """将未超深的链接作为后续 authorized_browse 任务入队。"""
    task_ids: list[str] = []
    for link in links:
        if link.depth > policy.max_depth:
            continue
        record = queue.enqueue(
            kind,
            {
                "url": link.url,
                "depth": link.depth,
                "policy": policy.to_dict(),
            },
            max_attempts=max_attempts,
        )
        task_ids.append(record.task_id)
    return task_ids
