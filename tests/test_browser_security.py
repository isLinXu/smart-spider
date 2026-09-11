# coding=utf-8
"""Browser URL policy regression tests (no Chromium required)."""

from smart_spider.browser import _validate_browser_request
from smart_spider.url_policy import URLPolicy


def test_browser_request_policy_allows_public_http_and_local_page_resources():
    policy = URLPolicy(resolve_dns=False)
    assert _validate_browser_request("https://example.com/page", policy)
    assert _validate_browser_request("data:text/plain,hello", policy)
    assert _validate_browser_request("blob:https://example.com/id", policy)


def test_browser_request_policy_blocks_private_and_unsupported_targets():
    policy = URLPolicy(resolve_dns=False)
    assert not _validate_browser_request("http://127.0.0.1/admin", policy)
    assert not _validate_browser_request("file:///etc/passwd", policy)
