# coding=utf-8
"""站点策略与 robots 门禁测试。"""
from __future__ import annotations

import json

import pytest

from smart_spider.site_policy import (
    HostRateLimiter,
    RobotsGate,
    SiteCrawlPolicy,
    host_matches,
)


def test_site_policy_allow_deny_and_cookies(tmp_path):
    cookies_path = tmp_path / "cookies.json"
    cookies_path.write_text(
        json.dumps([{"name": "sid", "value": "1", "domain": "example.com"}]),
        encoding="utf-8",
    )
    policy = SiteCrawlPolicy.from_mapping(
        {
            "allow_hosts": ["example.com"],
            "deny_hosts": ["ads.example.com"],
            "session_cookies_path": str(cookies_path),
            "requests_per_second": 2,
            "max_depth": 1,
        }
    )
    assert policy.allows_url("https://example.com/a")
    assert policy.allows_url("https://www.example.com/a")
    assert not policy.allows_url("https://ads.example.com/x")
    assert not policy.allows_url("https://other.com/")
    assert policy.load_cookies()[0]["name"] == "sid"
    assert host_matches("www.example.com", ["example.com"])


def test_site_policy_rejects_invalid_rate():
    with pytest.raises(ValueError):
        SiteCrawlPolicy(requests_per_second=0)


def test_robots_gate_respects_disallow():
    body = "User-agent: *\nDisallow: /private\nAllow: /\n"

    def fetch(_url: str) -> str:
        return body

    gate = RobotsGate(enabled=True, fetcher=fetch, user_agent="smart-spider")
    assert gate.allowed("https://example.com/")
    assert not gate.allowed("https://example.com/private/x")


def test_robots_gate_fail_open_when_missing():
    gate = RobotsGate(enabled=True, fetcher=lambda _u: None)
    assert gate.allowed("https://example.com/anything")


def test_host_rate_limiter_acquire():
    limiter = HostRateLimiter(requests_per_second=1000.0)
    limiter.acquire("https://example.com/a")
    limiter.acquire("example.com")
