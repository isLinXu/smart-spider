# coding=utf-8
"""授权浏览剧本测试（Mock 浏览器，无 Playwright）。"""
from __future__ import annotations

from smart_spider.browser_playbook import (
    AuthorizedBrowsePlaybook,
    PlaybookStep,
    enqueue_discovered_links,
)
from smart_spider.pipeline import LocalSqliteTaskQueue
from smart_spider.site_policy import RobotsGate, SiteCrawlPolicy


class FakeController:
    def __init__(self, html: str, url: str = "https://example.com/"):
        self._html = html
        self.pages = {url: html}
        self.current_url = url
        self.clicked = []
        self.typed = []
        self.waiting = []

    def navigate(self, url: str) -> str:
        self.current_url = url
        return self.pages.get(url, self._html)

    def scroll(self):
        return True, self.navigate(self.current_url)

    def click(self, selector: str):
        self.clicked.append(selector)
        return True, self.navigate(self.current_url)

    def type_text(self, selector: str, text: str):
        self.typed.append((selector, text))
        return True, self.navigate(self.current_url)

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.waiting.append(selector)
        if selector.startswith("missing"):
            return False, self.navigate(self.current_url)
        return True, self.navigate(self.current_url)


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


def test_playbook_runs_declarative_steps():
    html = "<html><body><button id='next'>n</button><p>ok page content here</p></body></html>"
    policy = SiteCrawlPolicy(
        allow_hosts=("example.com",),
        action_delay_seconds=0,
        scroll_passes=0,
        respect_robots=False,
        requests_per_second=1000,
    )
    controller = FakeController(html)
    playbook = AuthorizedBrowsePlaybook(
        controller,
        policy,
        steps=[
            PlaybookStep(action="wait_for", selector="#next"),
            PlaybookStep(action="click", selector="#next"),
            PlaybookStep(action="type", selector="#q", text="cat"),
        ],
    )
    result = playbook.run("https://example.com/")
    assert result.block.kind.value == "ok"
    assert result.steps_run == 3
    assert controller.clicked == ["#next"]
    assert controller.typed == [("#q", "cat")]
    assert controller.waiting == ["#next"]


def test_playbook_bfs_stays_on_same_host():
    listing = """
    <html><body>
      <a href="/p2">two</a>
      <a href="https://evil.com/x">evil</a>
      <p>ok page content here</p>
    </body></html>
    """
    page2 = "<html><body><p>ok page content here second</p></body></html>"
    controller = FakeController(listing, url="https://example.com/")
    controller.pages["https://example.com/p2"] = page2
    policy = SiteCrawlPolicy(
        allow_hosts=("example.com",),
        action_delay_seconds=0,
        scroll_passes=0,
        max_depth=1,
        respect_robots=False,
        requests_per_second=1000,
    )
    playbook = AuthorizedBrowsePlaybook(
        controller, policy, robots=RobotsGate(enabled=False)
    )
    results = playbook.crawl_bfs(["https://example.com/"])
    urls = [item.seed_url for item in results]
    assert "https://example.com/" in urls
    assert "https://example.com/p2" in urls
    assert all("evil.com" not in item.seed_url for item in results)
