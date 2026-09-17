# coding=utf-8
"""授权站点画像 SiteProfile（ADR-0018 / S17）。

将可复用的域策略、种子 URL 与合规声明收成一份 YAML/JSON，
供 browse CLI / API / worker 入队使用。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .compliance import CompliancePolicy
from .config_io import load_mapping
from .browser_playbook import PlaybookStep, parse_playbook_steps
from .site_policy import SiteCrawlPolicy, host_of


_POLICY_KEYS = {
    "allow_hosts",
    "deny_hosts",
    "requests_per_second",
    "qps",
    "action_delay_seconds",
    "prefer_browser",
    "respect_robots",
    "robots_user_agent",
    "session_cookies_path",
    "storage_state_path",
    "storage_state",
    "max_depth",
    "max_links_per_page",
    "scroll_passes",
    "allow_private_hosts",
}


@dataclass
class SiteProfile:
    """可复用的授权站点爬取画像。"""

    name: str
    seed_urls: tuple[str, ...] = ()
    policy: SiteCrawlPolicy = field(default_factory=SiteCrawlPolicy)
    license: str = ""
    source_terms: str = ""
    headless: bool = True
    enqueue_links: bool = True
    max_attempts: int = 3
    description: str = ""
    steps: tuple[PlaybookStep, ...] = ()

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        if not self.name:
            raise ValueError("SiteProfile.name is required")
        seeds = tuple(str(u).strip() for u in self.seed_urls if str(u).strip())
        object.__setattr__(self, "seed_urls", seeds)
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if not isinstance(self.policy, SiteCrawlPolicy):
            raise TypeError("policy must be SiteCrawlPolicy")
        object.__setattr__(self, "steps", parse_playbook_steps(self.steps))
        # If allow_hosts empty but seeds present, default allow seed hosts.
        if not self.policy.allow_hosts and seeds:
            hosts = tuple(sorted({host_of(url) for url in seeds if host_of(url)}))
            object.__setattr__(
                self,
                "policy",
                SiteCrawlPolicy.from_mapping(
                    {**self.policy.to_dict(), "allow_hosts": hosts}
                ),
            )

    def compliance_policy(self) -> CompliancePolicy:
        return CompliancePolicy(
            license=self.license,
            source_terms=self.source_terms,
            respect_robots=self.policy.respect_robots,
            redact_urls=True,
            allow_hosts=self.policy.allow_hosts,
            deny_hosts=self.policy.deny_hosts,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "seed_urls": list(self.seed_urls),
            "policy": self.policy.to_dict(),
            "license": self.license,
            "source_terms": self.source_terms,
            "headless": self.headless,
            "enqueue_links": self.enqueue_links,
            "max_attempts": self.max_attempts,
            "steps": [step.to_dict() for step in self.steps],
        }

    def resolve_seeds(self, extra_urls: Optional[Sequence[str]] = None) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for url in list(self.seed_urls) + list(extra_urls or ()):
            item = str(url or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def validate_seeds(self, urls: Optional[Sequence[str]] = None) -> list[str]:
        """返回通过策略的 URL；非法 URL 抛 ValueError（汇总原因）。"""
        targets = list(urls) if urls is not None else list(self.seed_urls)
        if not targets:
            raise ValueError("at least one seed url is required")
        accepted: list[str] = []
        rejected: list[str] = []
        for url in targets:
            if self.policy.allows_url(url):
                accepted.append(url)
            else:
                rejected.append(url)
        if rejected:
            raise ValueError(
                "urls denied by site policy or URL safety checks: "
                + ", ".join(rejected)
            )
        return accepted

    def browse_payload(self, url: str, *, depth: int = 0) -> dict[str, Any]:
        """构造 authorized_browse 队列 payload。"""
        return {
            "url": url,
            "depth": int(depth),
            "policy": self.policy.to_dict(),
            "enqueue_links": bool(self.enqueue_links),
            "headless": bool(self.headless),
            "max_attempts": int(self.max_attempts),
            "profile": self.name,
            "license": self.license,
            "source_terms": self.source_terms,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SiteProfile":
        data = dict(raw or {})
        nested = data.get("policy")
        policy_raw: dict[str, Any] = {}
        if isinstance(nested, SiteCrawlPolicy):
            policy = nested
        else:
            if isinstance(nested, Mapping):
                policy_raw.update(dict(nested))
            for key in _POLICY_KEYS:
                if key in data and key not in policy_raw:
                    policy_raw[key] = data[key]
            policy = SiteCrawlPolicy.from_mapping(policy_raw)

        seeds = data.get("seed_urls") or data.get("urls") or ()
        name = data.get("name") or data.get("profile") or ""
        if not name and seeds:
            host = host_of(list(seeds)[0])
            name = host or "unnamed"
        return cls(
            name=str(name),
            seed_urls=tuple(seeds),
            policy=policy,
            license=str(data.get("license") or ""),
            source_terms=str(data.get("source_terms") or data.get("terms") or ""),
            headless=bool(data.get("headless", True)),
            enqueue_links=bool(data.get("enqueue_links", True)),
            max_attempts=int(data.get("max_attempts") or 3),
            description=str(data.get("description") or ""),
            steps=parse_playbook_steps(data.get("steps") or ()),
        )


def load_site_profile(
    path: str,
    *,
    overrides: Optional[Mapping[str, Any]] = None,
    use_env: bool = False,
) -> SiteProfile:
    """从 YAML/JSON 加载 SiteProfile，可叠加 overrides。"""
    mapping = load_mapping(path, overrides=overrides, use_env=use_env)
    return SiteProfile.from_mapping(mapping)
