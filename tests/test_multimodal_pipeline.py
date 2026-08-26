# coding=utf-8
"""第二阶段多模态抽取、来源路由和标注路由测试。"""
from dataclasses import dataclass

from smart_spider.dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelMode,
    LabelPolicy,
    Modality,
)
from smart_spider.multimodal_pipeline import (
    AdaptiveSourceRouter,
    AnnotationRouter,
    BrowserPageSource,
    DiscoveryTask,
    PageSampleExtractor,
    RouteAction,
    SourceResponse,
    StaticPageSource,
)


HTML = """
<html>
  <head><title>猫咪观察</title><meta name="content-language" content="zh-CN"></head>
  <body>
    <script>不要进入训练文本</script>
    <article>
      <h1>猫咪观察</h1>
      <p>这是一段用于图文数据集的正文，描述一只猫坐在窗边。</p>
      <img src="/images/cat.jpg" alt="窗边的猫">
      <img data-src="https://cdn.example.com/cat-2.webp">
    </article>
  </body>
</html>
"""


def test_page_sample_extractor_builds_text_image_and_page_context():
    sample = PageSampleExtractor(min_text_chars=10).extract_page(
        "https://example.com/article/1",
        HTML,
        source="test",
        query="cat",
    )
    modalities = {asset.modality for asset in sample.modalities}
    assert modalities == {Modality.WEBPAGE, Modality.TEXT, Modality.IMAGE}
    text_asset = next(asset for asset in sample.modalities if asset.modality == Modality.TEXT)
    assert "不要进入训练文本" not in text_asset.text
    assert "猫坐在窗边" in text_asset.text
    image_assets = [asset for asset in sample.modalities if asset.modality == Modality.IMAGE]
    assert image_assets[0].uri == "https://example.com/images/cat.jpg"
    assert any(relation.relation == "caption-of" for relation in sample.relations)
    assert sample.task_type == "image_text_alignment"
    assert sample.quality.attributes["image_count"] == 2


def test_page_sample_extractor_discovers_absolute_image_candidates():
    candidates = PageSampleExtractor(min_text_chars=10).discover_candidates(
        "https://example.com/article/1", HTML, source="test", query="cat"
    )
    assert len(candidates) == 2
    assert all(candidate.modality == Modality.IMAGE for candidate in candidates)
    assert candidates[0].source_meta["role"] == "image"


def test_adaptive_router_switches_to_browser_when_static_is_insufficient():
    browser_called = []

    def static_source(task):
        return SourceResponse(html_length=20, candidates=[])

    def browser_source(task):
        browser_called.append(task.start_url)
        return SourceResponse(candidates=[
            # Browser Use often returns a rendered result URL rather than page HTML.
            CandidateResource(
                "https://cdn.example.com/cat.jpg", "browser", modality=Modality.IMAGE
            )
        ], html_length=5000, dynamic=True)

    result = AdaptiveSourceRouter(min_html_length=100).discover(
        DiscoveryTask(start_url="https://example.com/search", min_candidates=1),
        static_source,
        browser_source,
    )
    assert result.decision.action == RouteAction.BROWSER
    assert browser_called == ["https://example.com/search"]
    assert len(result.candidates) == 1


def test_adaptive_router_keeps_static_when_quality_is_sufficient():
    browser_called = []

    def static_source(task):
        return SourceResponse(
            candidates=[CandidateResource(
                "https://example.com/cat.jpg", "static", modality=Modality.IMAGE
            )],
            html_length=2000,
        )

    def browser_source(task):
        browser_called.append(True)
        return SourceResponse()

    result = AdaptiveSourceRouter().discover(
        DiscoveryTask(start_url="https://example.com/search", min_candidates=1),
        static_source,
        browser_source,
    )
    assert result.decision.action == RouteAction.STATIC
    assert browser_called == []


@dataclass
class _FakeResponse:
    text: str
    status_code: int = 200
    closed: bool = False

    def close(self):
        self.closed = True


class _FakeHttpClient:
    def __init__(self, response):
        self.response = response

    def get(self, url):
        return self.response


def test_static_and_browser_page_sources_are_injectable():
    response = _FakeResponse(HTML)
    static = StaticPageSource(_FakeHttpClient(response), PageSampleExtractor(min_text_chars=10))
    result = static(DiscoveryTask(start_url="https://example.com/a", source="static"))
    assert result.status_code == 200
    assert result.candidates
    assert response.closed is True

    browser = BrowserPageSource(
        lambda url: HTML,
        PageSampleExtractor(min_text_chars=10),
    )
    browser_result = browser(DiscoveryTask(start_url="https://example.com/a", source="browser"))
    assert browser_result.dynamic is True
    assert browser_result.candidates


class _LocalBackend:
    def annotate(self, sample):
        return [
            LabelDecision("cat", 0.93),
            LabelDecision("outdoor", 0.81),
        ]


class _BrokenBackend:
    def annotate(self, sample):
        raise RuntimeError("provider unavailable")


def test_annotation_router_combines_backends_and_keeps_discovered_labels_pending():
    router = AnnotationRouter(
        policy=LabelPolicy(
            mode=LabelMode.HYBRID,
            fixed_labels=["cat"],
            discovery_threshold=0.7,
        ),
        backends={"local": _LocalBackend(), "cloud": _BrokenBackend()},
    )
    sample = PageSampleExtractor(min_text_chars=10).extract_page("https://example.com/a", HTML)
    result = router.annotate(sample)
    assert [label.name for label in result.resolution.labels] == ["cat"]
    assert [label.name for label in result.resolution.candidates] == ["outdoor"]
    assert result.used_backends == ["local"]
    assert "cloud" in result.errors
