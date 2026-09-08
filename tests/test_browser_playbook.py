# coding=utf-8
"""授权浏览剧本测试（Mock 浏览器，无 Playwright）。"""
from __future__ import annotations

from smart_spider.browser_playbook import (
    AuthorizedBrowsePlaybook,
    enqueue_discovered_links,
)
from smart_spider.pipeline import LocalSqliteTaskQueue
from smart_spider.site_policy import RobotsGate, SiteCrawlPolicy


class FakeController:
    def __init__(self, html: str, url: str = "https://example.com/"):
        self._html = html
        self.current_url = url

    def navigate(self, url: str) -> str:
        self.current_url = url
        return self._html

    def scroll(self):
        return True, self._html


def test_playbook_extracts_allowed_links_only():
    html = """
    <html><body>
      <a href="/ok">OK</a>
      <a href="https://evil.com/x">Evil</a>
      <a href="https://example.com/next">Next</a>
    </body></html>
    """
    policy = SiteCrawlPolicy(
        allow_hosts=("example.com",),
        action_delay_seconds=0,
        scroll_passes=1,
        max_links_per_page=10,
        respect_robots=False,
        requests_per_second=1000,
    )
    playbook = AuthorizedBrowsePlaybook(
        FakeController(html),
        policy,
        robots=RobotsGate(enabled=False),
    )
    result = playbook.run("https://example.com/")
    assert result.block is not None and result.block.kind.value == "ok"
    urls = {item.url for item in result.links}
    assert "https://example.com/ok" in urls
    assert "https://example.com/next" in urls
    assert "https://evil.com/x" not in urls
    assert result.skipped_by_policy >= 1


def test_playbook_stops_on_challenge_page():
    html = "<html>verify you are human captcha</html>"
    policy = SiteCrawlPolicy(
        allow_hosts=("example.com",),
        action_delay_seconds=0,
        scroll_passes=0,
        respect_robots=False,
        requests_per_second=1000,
    )
    playbook = AuthorizedBrowsePlaybook(FakeController(html), policy)
    result = playbook.run("https://example.com/")
    assert result.block.kind.value == "challenge"
    assert result.links == []


def test_enqueue_discovered_links(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    policy = SiteCrawlPolicy(allow_hosts=("example.com",), max_depth=2)
    from smart_spider.browser_playbook import DiscoveredLink

    ids = enqueue_discovered_links(
        queue,
        [
            DiscoveredLink(url="https://example.com/a", depth=1),
            DiscoveredLink(url="https://example.com/b", depth=3),
        ],
        policy=policy,
    )
    assert len(ids) == 1
    assert queue.get(ids[0]).kind == "authorized_browse"
