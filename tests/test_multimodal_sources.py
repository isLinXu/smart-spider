# coding=utf-8
"""搜索和站点来源适配器测试。"""
from smart_spider.dataset_contracts import Modality
from smart_spider.engines import MediaType, RenderMode
from smart_spider.multimodal_pipeline import DiscoveryTask
from smart_spider.multimodal_sources import SearchDiscoverySource, SiteDiscoverySource


class _FakeHttp:
    def get_text(self, url, engine=None):
        return "fake html"


class _FakeEngine:
    media_type = MediaType.IMAGE
    render_mode = RenderMode.STATIC
    page_step = 20

    def build_search_url(self, query, page):
        return f"https://search.example/?q={query}&page={page}"

    def extract_items(self, html):
        return [{"url": "https://cdn.example.com/a.jpg", "meta": {"title": "A"}}]


def test_search_discovery_source_converts_engine_items(monkeypatch):
    monkeypatch.setattr("smart_spider.multimodal_sources.get_engine", lambda name: _FakeEngine())
    source = SearchDiscoverySource(_FakeHttp(), engines=["fake"])
    response = source(DiscoveryTask(
        query="cat",
        modalities=(Modality.IMAGE,),
        metadata={"pages": 2},
    ))
    assert len(response.candidates) == 2
    assert response.candidates[0].modality == Modality.IMAGE
    assert response.candidates[0].title == "A"
    assert response.html_length == len("fake html")
    assert response.metadata["item_counts"] == {"fake": 2}


def test_search_discovery_source_exposes_empty_parse_diagnostic(monkeypatch):
    class EmptyEngine(_FakeEngine):
        def extract_items(self, html):
            return []

    monkeypatch.setattr("smart_spider.multimodal_sources.get_engine", lambda name: EmptyEngine())
    response = SearchDiscoverySource(_FakeHttp(), engines=["fake"])(DiscoveryTask(
        query="cat",
        modalities=(Modality.IMAGE,),
    ))
    assert response.candidates == []
    assert response.error == "no_parseable_candidates"
    assert response.html_length == len("fake html")
    assert response.metadata["item_counts"] == {"fake": 0}


class _FakeParser:
    name = "fake-site"

    def build_listing_url(self, page):
        return f"https://site.example/page/{page}"

    def collect_pages(self, html):
        return [{"url": "https://site.example/article/1", "title": "Article", "id": "1"}]


def test_site_discovery_source_emits_webpage_candidates(monkeypatch):
    monkeypatch.setattr("smart_spider.multimodal_sources.get_site_parser", lambda name: _FakeParser())
    source = SiteDiscoverySource(_FakeHttp(), "fake-site", start_page=1, end_page=2)
    response = source(DiscoveryTask(modalities=(Modality.WEBPAGE,)))
    assert len(response.candidates) == 2
    assert response.candidates[0].modality == Modality.WEBPAGE
    assert response.candidates[0].source == "site:fake-site"
