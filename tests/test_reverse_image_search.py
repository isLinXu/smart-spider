"""远程网页以图搜图的离线契约和解析测试。"""
from __future__ import annotations

import html
from pathlib import Path

import pytest

from smart_spider.reverse_image_search import (
    BaiduReverseImageProvider,
    BingVisualSearchProvider,
    GoogleLensProvider,
    ProviderSearchResponse,
    RemoteImageSearchResult,
    ReverseImageSearcher,
    _blocked_page_reason,
    _parse_baidu_results,
    _parse_bing_results,
    _parse_google_results,
    resolve_providers,
)
from smart_spider.reverse_image_search_cli import _build_parser


def test_resolve_providers_supports_all_and_aliases():
    assert resolve_providers(["google", "bing", "google_lens"]) == (
        "google_lens",
        "bing",
    )
    assert resolve_providers(["all"]) == ("baidu", "bing", "google_lens")


def test_resolve_providers_rejects_unknown_provider():
    with pytest.raises(ValueError, match="unknown reverse image provider"):
        resolve_providers(["yandex"])


def test_parse_bing_iousc_results():
    payload = {
        "murl": "https://cdn.example.com/cat.jpg",
        "turl": "https://cdn.example.com/cat-thumb.jpg",
        "purl": "https://example.com/cat",
        "t": "Example cat",
        "desc": "A cat result",
    }
    source = (
        '<a class="iusc" m="%s"></a>'
        % html.escape(__import__("json").dumps(payload), quote=True)
    )
    results = _parse_bing_results(
        source,
        page_url="https://www.bing.com/images/search",
        top_k=5,
    )
    assert len(results) == 1
    assert results[0].provider == "bing"
    assert results[0].source_url == "https://example.com/cat"
    assert results[0].image_url == "https://cdn.example.com/cat.jpg"
    assert results[0].thumbnail_url.endswith("cat-thumb.jpg")
    assert results[0].metadata["source_domain"] == "example.com"


def test_parse_google_lens_external_links_and_filters_provider_links():
    source = """
    <a href="https://lens.google.com/about">About</a>
    <a href="https://example.com/page"><img src="https://cdn.example.com/a.jpg" alt="Example result"></a>
    <a href="https://example.org/second">Second result</a>
    """
    results = _parse_google_results(
        source,
        page_url="https://lens.google.com/search?p=1",
        top_k=5,
    )
    assert [result.source_url for result in results] == [
        "https://example.com/page",
        "https://example.org/second",
    ]
    assert results[0].image_url == "https://cdn.example.com/a.jpg"
    assert results[0].title == "Example result"


def test_parse_baidu_external_links():
    source = """
    <a href="https://graph.baidu.com/pcpage/index">Baidu navigation</a>
    <a href="https://example.com/product"><img data-src="//cdn.example.com/p.png" alt="Product"></a>
    """
    results = _parse_baidu_results(
        source,
        page_url="https://graph.baidu.com/pcpage/index",
        top_k=5,
    )
    assert len(results) == 1
    assert results[0].source_url == "https://example.com/product"
    assert results[0].image_url == "https://cdn.example.com/p.png"
    assert results[0].metadata["source_domain"] == "example.com"


def test_parse_baidu_keeps_content_subdomains_as_sources():
    source = '<a href="https://baijiahao.baidu.com/s?id=1">Baidu article</a>'
    results = _parse_baidu_results(
        source,
        page_url="https://graph.baidu.com/s",
        top_k=5,
    )
    assert results[0].source_url.startswith("https://baijiahao.baidu.com/")


def test_blocked_page_detection_is_explicit():
    assert _blocked_page_reason("Please verify you are human")
    assert _blocked_page_reason("normal image results") is None


def test_response_and_result_json_contract():
    result = RemoteImageSearchResult(
        provider="bing",
        title="Example",
        source_url="https://example.com",
    )
    response = ProviderSearchResponse(
        provider="bing",
        query_image="/tmp/query.jpg",
        results=(result,),
    )
    assert response.to_dict()["results"][0]["rank"] == 1
    assert response.to_dict()["results"][0]["source_url"] == "https://example.com"
    assert response.to_dict()["attempts"] == 1


def test_searcher_validates_image_and_top_k(tmp_path):
    searcher = ReverseImageSearcher(providers=["bing"])
    with pytest.raises(FileNotFoundError):
        searcher.search(str(tmp_path / "missing.jpg"))
    with pytest.raises(ValueError, match="top_k"):
        searcher.search(__file__, top_k=0)
    with pytest.raises(ValueError, match="max_attempts"):
        ReverseImageSearcher(providers=["bing"], max_attempts=0)


def test_provider_retry_uses_fallback_entry_and_records_attempts(monkeypatch):
    searcher = ReverseImageSearcher(providers=["bing"], max_attempts=2)
    provider = BingVisualSearchProvider()
    calls: list[str] = []

    def fake_search_one(context, current_provider, image_path, top_k, *, start_url, attempt):
        assert current_provider is provider
        assert image_path == str(Path("/tmp/query.jpg"))
        assert top_k == 3
        calls.append(start_url)
        return ProviderSearchResponse(
            provider=current_provider.name,
            query_image=image_path,
            result_page_url=start_url,
            error="no upload input",
        )

    monkeypatch.setattr(searcher, "_search_one", fake_search_one)
    response = searcher._search_provider(object(), provider, "/tmp/query.jpg", 3)

    assert calls == list(provider.start_urls)
    assert response.attempts == 2
    assert response.error == "no upload input"


def test_provider_waits_until_dynamic_result_selector_appears():
    class FakeLocator:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1 if self.page.polls >= 2 else 0

    class FakePage:
        url = "https://graph.baidu.com/initial"

        def __init__(self):
            self.polls = 0
            self.settled_for = 0
            self.load_state_timeout = None

        def locator(self, selector):
            return FakeLocator(self)

        def wait_for_timeout(self, delay_ms):
            if delay_ms == 250:
                self.polls += 1
            else:
                self.settled_for = delay_ms

        def wait_for_load_state(self, state, timeout):
            self.load_state_timeout = timeout

    page = FakePage()
    BaiduReverseImageProvider().wait_for_results(
        page,
        timeout_ms=1_000,
        settle_ms=75,
        initial_url=page.url,
    )

    assert page.polls == 2
    assert page.settled_for == 75
    assert page.load_state_timeout is not None


def test_cli_parser_accepts_remote_search_options():
    args = _build_parser().parse_args([
        "--image", "query.jpg",
        "--providers", "baidu", "google",
        "--no-headless",
        "--user-data-dir", "/tmp/smart-spider-browser",
        "--top-k", "7",
        "--attempts", "3",
    ])
    assert args.image == "query.jpg"
    assert args.providers == ["baidu", "google"]
    assert args.no_headless is True
    assert args.top_k == 7
    assert args.attempts == 3


def test_provider_contract_exposes_upload_and_parser():
    assert BingVisualSearchProvider.name == "bing"
    assert GoogleLensProvider.name == "google_lens"
    assert callable(BingVisualSearchProvider().upload)
    assert callable(GoogleLensProvider().parse_results)
