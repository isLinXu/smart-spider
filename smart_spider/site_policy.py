# coding=utf-8
"""授权站点爬取策略（ADR-0014）。

边界
----
- 域允许/拒绝列表、礼貌限速、可选 robots.txt、会话 Cookie 路径、是否启用浏览器。
- 不提供指纹伪装、验证码打码或对抗式反爬绕过。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from .rate_limit import RateLimiter
from .url_policy import URLPolicy, UnsafeURLError


def _normalize_host(host: str) -> str:
    return (host or "").rstrip(".").lower()


def host_of(url: str) -> str:
    return _normalize_host(urlparse(url).hostname or "")


def host_matches(host: str, patterns: Sequence[str]) -> bool:
    host = _normalize_host(host)
    if not host:
        return False
    for pattern in patterns:
        item = _normalize_host(pattern)
        if not item:
            continue
        if host == item or host.endswith("." + item):
            return True
    return False


@dataclass
class SiteCrawlPolicy:
    """单次授权爬取任务的站点策略。"""

    allow_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()
    requests_per_second: float = 1.0
    action_delay_seconds: float = 0.5
    prefer_browser: bool = True
    respect_robots: bool = True
    robots_user_agent: str = "smart-spider"
    session_cookies_path: str = ""
    max_depth: int = 2
    max_links_per_page: int = 50
    scroll_passes: int = 1
    allow_private_hosts: bool = False

    def __post_init__(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if self.action_delay_seconds < 0:
            raise ValueError("action_delay_seconds cannot be negative")
        if self.max_depth < 0:
            raise ValueError("max_depth cannot be negative")
        if self.max_links_per_page <= 0:
            raise ValueError("max_links_per_page must be positive")
        if self.scroll_passes < 0:
            raise ValueError("scroll_passes cannot be negative")
        object.__setattr__(
            self,
            "allow_hosts",
            tuple(_normalize_host(h) for h in self.allow_hosts if h),
        )
        object.__setattr__(
            self,
            "deny_hosts",
            tuple(_normalize_host(h) for h in self.deny_hosts if h),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "SiteCrawlPolicy":
        data = dict(raw or {})
        allow = data.get("allow_hosts") or data.get("allowed_hosts") or ()
        deny = data.get("deny_hosts") or data.get("blocked_hosts") or ()
        return cls(
            allow_hosts=tuple(allow),
            deny_hosts=tuple(deny),
            requests_per_second=float(
                data.get("requests_per_second") or data.get("qps") or 1.0
            ),
            action_delay_seconds=float(data.get("action_delay_seconds") or 0.5),
            prefer_browser=bool(data.get("prefer_browser", True)),
            respect_robots=bool(data.get("respect_robots", True)),
            robots_user_agent=str(data.get("robots_user_agent") or "smart-spider"),
            session_cookies_path=str(data.get("session_cookies_path") or ""),
            max_depth=int(data.get("max_depth") if data.get("max_depth") is not None else 2),
            max_links_per_page=int(
                data.get("max_links_per_page")
                if data.get("max_links_per_page") is not None
                else 50
            ),
            scroll_passes=int(
                data.get("scroll_passes") if data.get("scroll_passes") is not None else 1
            ),
            allow_private_hosts=bool(data.get("allow_private_hosts", False)),
        )

    def load_cookies(self) -> list[dict[str, Any]]:
        path = (self.session_cookies_path or "").strip()
        if not path:
            return []
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and "cookies" in payload:
            payload = payload["cookies"]
        if not isinstance(payload, list):
            raise ValueError("session cookies file must be a list or {cookies: [...]}")
        return [dict(item) for item in payload]

    def allows_host(self, host: str) -> bool:
        host = _normalize_host(host)
        if not host:
            return False
        if self.deny_hosts and host_matches(host, self.deny_hosts):
            return False
        if self.allow_hosts:
            return host_matches(host, self.allow_hosts)
        return True

    def allows_url(self, url: str, *, url_policy: Optional[URLPolicy] = None) -> bool:
        policy = url_policy or URLPolicy(allow_private_hosts=self.allow_private_hosts)
        try:
            policy.validate(url)
        except UnsafeURLError:
            return False
        return self.allows_host(host_of(url))


class HostRateLimiter:
    """按 host 的礼貌限速。"""

    def __init__(self, requests_per_second: float = 1.0):
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self._rate = float(requests_per_second)
        self._limiters: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()

    def acquire(self, host_or_url: str) -> None:
        host = host_of(host_or_url) if "://" in host_or_url else _normalize_host(host_or_url)
        if not host:
            host = "_"
        with self._lock:
            limiter = self._limiters.get(host)
            if limiter is None:
                limiter = RateLimiter(self._rate)
                self._limiters[host] = limiter
        limiter.acquire()


class RobotsGate:
    """可选 robots.txt 门禁；失败时默认放行（fail-open）并记录原因。"""

    def __init__(
        self,
        *,
        user_agent: str = "smart-spider",
        enabled: bool = True,
        fetcher=None,
    ):
        self.user_agent = user_agent
        self.enabled = enabled
        self._fetcher = fetcher or self._default_fetch
        self._cache: dict[str, RobotFileParser] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_fetch(robots_url: str) -> Optional[str]:
        try:
            from urllib.request import Request, urlopen

            request = Request(
                robots_url,
                headers={"User-Agent": "smart-spider-robots-check"},
            )
            with urlopen(request, timeout=5) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception:
            return None

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        base = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = f"{base}/robots.txt"
        with self._lock:
            parser = self._cache.get(base)
            if parser is None:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                body = self._fetcher(robots_url)
                if body is None:
                    # fail-open: treat as allow-all when robots cannot be fetched
                    parser.parse([])
                else:
                    parser.parse(body.splitlines())
                self._cache[base] = parser
        try:
            return bool(parser.can_fetch(self.user_agent, url))
        except Exception:
            return True


def polite_sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
