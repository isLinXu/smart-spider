import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

import pytest

from smart_spider.douyin import (DouyinCrawler, download_item, load_browser_cookies,
                                 normalize_input, parse_page, parse_payload, video_id)
from smart_spider.engines import get_engine


@pytest.fixture
def payload():
    return {"aweme_id": "7123456789012345678", "desc": "测试视频", "author": {"nickname": "作者"},
            "video": {"duration": 12340, "width": 720, "height": 1280,
                      "play_addr": {"url_list": ["https://video.example.com/a.mp4"]}}}


@pytest.mark.parametrize("value,expected", [
    ("7123456789012345678", "https://www.douyin.com/video/7123456789012345678"),
    ("复制打开抖音 https://v.douyin.com/AbC123/ 看视频", "https://v.douyin.com/AbC123/"),
    ("https://www.douyin.com/user/MS4wLjAB", "https://www.douyin.com/user/MS4wLjAB"),
    ("https://www.iesdouyin.com/share/video/7123456789012345678/", "https://www.iesdouyin.com/share/video/7123456789012345678/"),
])
def test_input(value, expected):
    assert normalize_input(value) == expected


@pytest.mark.parametrize("value", ["https://douyin.com.evil.test/video/123", "http://localhost/video/123",
                                    "https://user:password@www.douyin.com/video/123", "", "猫咪"])
def test_bad_input(value):
    with pytest.raises(ValueError):
        normalize_input(value)


def test_keyword_encoding():
    assert normalize_input("猫/狗 &", keyword=True).endswith("%E7%8C%AB%2F%E7%8B%97%20%26?type=video")
    assert video_id("https://www.douyin.com/?modal_id=123") == "123"
    assert video_id("https://evil.test/video/123") == ""


def test_payload_variants_and_dedup(payload):
    data = {"data": [{"aweme_info": payload}, {"aweme_list": [payload]}, None, "noise"]}
    items = parse_payload(data)
    assert len(items) == 1
    assert items[0]["meta"]["duration"] == 12.34
    assert items[0]["meta"]["author"] == "作者"
    assert parse_payload({"item_list": [payload]}) == items
    assert parse_payload({**payload, "images": [{"url_list": []}]}) == []
    assert parse_payload({**payload, "video": None}) == []


def test_html_data_islands(payload):
    expected = parse_payload(payload)
    assert parse_page('<script id="RENDER_DATA">' + quote(json.dumps(payload)) + '</script>') == expected
    assert parse_page('<script>window._ROUTER_DATA = ' + json.dumps(payload) + ';</script>') == expected
    assert parse_page('<script>invalid javascript</script>') == []
    engine = get_engine("douyin")
    assert engine.extract_items(json.dumps({"douyin_items": expected})) == expected
    assert engine.extract_items(json.dumps({"item_list": [payload]})) == expected


def test_netscape_cookies(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text("# Netscape HTTP Cookie File\n"
                    "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret\n"
                    ".example.com\tTRUE\t/\tTRUE\t0\tother\thidden\n"
                    ".douyin.com\tTRUE\t/\tTRUE\t1\texpired\told\n")
    cookies = load_browser_cookies(str(path))
    assert len(cookies) == 1
    assert cookies[0]["name"] == "sessionid"
    assert cookies[0]["secure"] is True
    with pytest.raises(FileNotFoundError):
        load_browser_cookies(str(tmp_path / "missing"))


def test_browser_constraints():
    with pytest.raises(ValueError):
        DouyinCrawler(browser_channel="unknown")
    assert DouyinCrawler(browser_channel="chrome").browser_channel == "chrome"
    with pytest.raises(ValueError):
        DouyinCrawler(login_wait=10)
    with pytest.raises(ValueError):
        DouyinCrawler(timeout=0)


def mock_browser(monkeypatch, payloads):
    page = Mock()
    page.url = "https://www.douyin.com/user/test"
    page.content.return_value = "<html></html>"
    callbacks = {}
    page.on.side_effect = lambda event, callback: callbacks.update({event: callback})
    chunks = iter(payloads)
    def wait(_):
        data = next(chunks, None)
        if data:
            response = Mock(url="https://www.douyin.com/aweme/v1/web/aweme/post/", status=200,
                            headers={})
            response.json.return_value = {"aweme_list": data}
            callbacks["response"](response)
    page.wait_for_timeout.side_effect = wait
    browser = Mock()
    browser.new_context.return_value.new_page.return_value = page
    manager = Mock()
    manager.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=Mock(return_value=browser))))
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: manager)
    return browser, page


def test_browser_pagination_and_limit(monkeypatch, payload):
    second = {**payload, "aweme_id": "7123456789012345679"}
    third = {**payload, "aweme_id": "7123456789012345680"}
    browser, page = mock_browser(monkeypatch, [[payload], [payload, second, third]])
    crawler = DouyinCrawler(url_policy=Mock())
    items = crawler.discover("https://www.douyin.com/user/test", limit=2)
    assert [i["meta"]["aweme_id"] for i in items] == [payload["aweme_id"], second["aweme_id"]]
    page.mouse.wheel.assert_called_once()
    browser.close.assert_called_once()


def test_installed_chrome_launch(monkeypatch, payload):
    browser, page = mock_browser(monkeypatch, [[payload]])
    from playwright.sync_api import sync_playwright
    chromium = sync_playwright().__enter__().chromium
    DouyinCrawler(url_policy=Mock(), browser_channel="chrome").discover(
        "https://www.douyin.com/user/test", limit=1)
    chromium.launch.assert_called_once_with(headless=True, channel="chrome")


def test_browser_empty_is_error_and_closes(monkeypatch):
    browser, page = mock_browser(monkeypatch, [])
    with pytest.raises(RuntimeError, match="未发现"):
        DouyinCrawler(url_policy=Mock()).discover("https://www.douyin.com/user/test")
    assert page.wait_for_timeout.call_count == 4
    browser.close.assert_called_once()


def test_single_filters_recommendations(monkeypatch, payload):
    other = {**payload, "aweme_id": "7123456789012345679"}
    browser, page = mock_browser(monkeypatch, [[other, payload]])
    items = DouyinCrawler(url_policy=Mock()).discover("https://www.douyin.com/video/" + payload["aweme_id"])
    assert len(items) == 1
    assert items[0]["meta"]["aweme_id"] == payload["aweme_id"]


@pytest.fixture
def download_context(payload):
    item = parse_payload(payload)[0]
    peek = b"\x00\x00\x00\x18ftypisom"
    response = Mock(headers={"Content-Length": str(len(peek) + 4)})
    response.iter_content.return_value = iter([b"data"])
    http = Mock()
    http.get_stream.return_value = peek, response
    dl = Mock(max_filesize="500m")
    return item, peek, response, http, dl


def test_download_and_metadata(tmp_path, download_context):
    item, peek, response, http, dl = download_context
    assert download_item(item, str(tmp_path), downloader=dl, http_client=http)
    identifier = item["meta"]["aweme_id"]
    assert (tmp_path / identifier / (identifier + ".mp4")).read_bytes() == peek + b"data"
    meta = json.loads((tmp_path / identifier / (identifier + ".json")).read_text())
    assert meta["source_url"] == item["url"]
    assert "media_url" not in meta
    response.close.assert_called_once()
    dl.download.assert_not_called()


@pytest.mark.parametrize("failure", ["oversize", "truncated", "html", "connection"])
def test_download_failure_cleanup(tmp_path, download_context, failure):
    item, peek, response, http, dl = download_context
    if failure == "oversize":
        dl.max_filesize = str(len(peek) + 1)
    elif failure == "truncated":
        response.headers["Content-Length"] = "999"
    elif failure == "html":
        http.get_stream.return_value = b"<html>login</html>", response
    else:
        response.iter_content.side_effect = OSError("interrupted")
    assert not download_item(item, str(tmp_path), downloader=dl, http_client=http)
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.mp4"))
    assert not list(tmp_path.rglob("*.json"))
    response.close.assert_called_once()


def test_ytdlp_skip_not_success(tmp_path, download_context):
    item, _, _, http, dl = download_context
    item["meta"]["media_url"] = ""
    dl.download.return_value = True
    assert not download_item(item, str(tmp_path), downloader=dl, http_client=http)


def test_main_spider_routing(payload):
    from smart_spider.smart_spider import SmartSpider
    spider = object.__new__(SmartSpider)
    spider._renderer_cookies, spider._headless, spider._browser_proxy = [], True, None
    spider.max_items = 5
    items = parse_payload(payload)
    with patch("smart_spider.douyin.DouyinCrawler") as crawler:
        crawler.return_value.discover.return_value = items
        html = spider._fetch_page("douyin", get_engine("douyin").build_search_url("猫咪", 0))
        assert get_engine("douyin").extract_items(html) == items
        crawler.return_value.discover.assert_called_once_with("猫咪", keyword=True, limit=5)
    assert spider._count_pages("douyin", "猫咪") == 1


def test_cli_metadata_and_failed_exit(tmp_path, payload):
    from smart_spider.douyin_cli import main
    items = parse_payload(payload)
    with patch("smart_spider.douyin_cli.DouyinCrawler") as crawler, patch("smart_spider.douyin_cli.download_item", return_value=False) as download:
        crawler.return_value.discover.return_value = items
        args = ["--keyword", "猫咪", "--output", str(tmp_path)]
        assert main(args + ["--metadata-only"]) == 0
        download.assert_not_called()
        assert main(args) == 1
    report = json.loads((tmp_path / "douyin_results.json").read_text())
    assert report["failed"] == 1
    assert "media_url" not in report["items"][0]["meta"]


def test_plain_json_keeps_percent_description(payload):
    payload["desc"] = '优惠 %22 &quot;'
    items = parse_page('<script>window._ROUTER_DATA=' + json.dumps(payload) + ';</script>')
    assert items[0]["meta"]["title"] == payload["desc"]


def test_scroll_budget(monkeypatch):
    browser, page = mock_browser(monkeypatch, [])
    with pytest.raises(RuntimeError):
        DouyinCrawler(url_policy=Mock(), max_scrolls=1).discover("https://www.douyin.com/user/test")
    assert page.mouse.wheel.call_count == 1


def test_real_http_stream_and_referer(tmp_path, payload):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from smart_spider.http_client import SmartHttpClient
    content = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 100
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Referer"))
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        item = parse_payload(payload)[0]
        item["meta"]["media_url"] = f"http://127.0.0.1:{server.server_port}/video.mp4"
        with SmartHttpClient(use_curl_cffi=False, allow_private_hosts=True, timeout=2) as http:
            assert download_item(item, str(tmp_path), downloader=Mock(max_filesize="1m"), http_client=http)
        assert next(tmp_path.rglob("*.mp4")).read_bytes() == content
        assert seen == ["https://www.douyin.com/"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_captcha_has_actionable_error(monkeypatch):
    browser, page = mock_browser(monkeypatch, [])
    page.title.return_value = "验证码中间页"
    with pytest.raises(RuntimeError, match="验证码页面"):
        DouyinCrawler(url_policy=Mock(), max_scrolls=0).discover("https://www.douyin.com/user/test")
    browser.close.assert_called_once()


def test_browser_hydration_playaddr_src_and_cover():
    data = {"awemeId": "7655642289305292070", "desc": "真实页面结构回归",
            "authorInfo": {"nickname": "作者", "secUid": "author-id"}, "createTime": 123,
            "video": {"playAddr": [{"src": "https://video.example.com/sample.mp4"}],
                      "cover": "//img.example.com/cover.jpg", "duration": 352634}}
    meta = parse_payload(data)[0]["meta"]
    assert meta["media_url"] == "https://video.example.com/sample.mp4"
    assert meta["thumb"] == "https://img.example.com/cover.jpg"
    assert meta["author"] == "作者"
    assert meta["author_id"] == "author-id"
    assert meta["create_time"] == 123


def test_failed_discovery_records_safe_diagnostics(monkeypatch, tmp_path):
    from smart_spider.douyin_cli import main
    browser, page = mock_browser(monkeypatch, [])
    page.title.return_value = "验证码中间页"
    with pytest.raises(SystemExit) as error:
        main(["--keyword", "test", "--max-scrolls", "0", "--output", str(tmp_path)])
    assert error.value.code == 1
    report = json.loads((tmp_path / "douyin_diagnostics.json").read_text())
    assert report == {"page_title": "验证码中间页", "responses": [], "discovered": 0}


def test_manual_confirmation_preserves_live_page(monkeypatch, payload):
    browser, page = mock_browser(monkeypatch, [[payload]])
    confirm = Mock()
    DouyinCrawler(url_policy=Mock(), headless=False, login_confirmation=confirm).discover(
        "https://www.douyin.com/user/test", limit=1)
    confirm.assert_called_once_with(page)
    browser.close.assert_called_once()
    with pytest.raises(ValueError, match="人工确认"):
        DouyinCrawler(login_confirmation=confirm)


def test_confirmation_eof_is_not_approval(monkeypatch):
    from smart_spider.douyin_cli import _confirm_login
    def eof(*args):
        raise EOFError()
    monkeypatch.setattr("builtins.input", eof)
    page = Mock()
    page.is_closed.return_value = False
    with pytest.raises(RuntimeError, match="交互式终端"):
        _confirm_login(page)


def test_keyword_search_reissued_after_manual_login(monkeypatch, payload):
    browser, page = mock_browser(monkeypatch, [[payload]])
    events = []
    page.goto.side_effect = lambda *args, **kwargs: events.append("navigate")
    crawler = DouyinCrawler(url_policy=Mock(), headless=False,
                            login_confirmation=lambda page: events.append("confirmed"))
    assert crawler.discover("test", keyword=True, limit=1)
    assert events == ["navigate", "confirmed", "navigate"]
