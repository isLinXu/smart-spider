# coding=utf-8
"""Regression tests for split DatasetCrawler internals."""
import json

from smart_spider.dataset_discovery import (
    bound_page_items,
    collection_limit_reached,
    discover_page_images,
    drain_inflight_window,
    estimate_pages_needed,
    items_to_discovered,
    order_search_engines,
    page_offsets,
    urls_to_discovered,
)
from smart_spider.dataset_download_window import DownloadWindow
from smart_spider.dataset_scene_admission import (
    admit_ingested_image,
    evaluate_scene_quality,
    normalize_scene_targets,
    scene_key,
    scene_semantic_threshold,
)
from smart_spider.dataset_image_ingest import (
    ingest_remote_image,
    prepare_output_image,
)
from smart_spider.dataset_image_commit import (
    materialize_accepted_image,
    reserve_ingest_quotas,
)
from smart_spider.dataset_governance import QuotaLedger, SceneQuotaLedger
from smart_spider.dataset_repository import DatasetCommit
from smart_spider.dataset_semantic_gate import (
    SemanticGateResult,
    evaluate_clip_similarity,
    evaluate_image_query_similarity,
    evaluate_semantic_filters,
)
from smart_spider.scene_quality import get_scene_quality_profile
from PIL import Image


def test_order_search_engines_prefers_successful_yield():
    names = ["slow", "fast"]
    metrics = {
        "slow": {"attempts": 10, "candidates": 2, "success_rate": 0.2},
        "fast": {"attempts": 10, "candidates": 20, "success_rate": 0.9},
    }
    assert order_search_engines(names, metrics)[0] == "fast"


def test_bound_page_items_truncates():
    items = [{"url": f"https://x/{i}"} for i in range(10)]
    assert len(bound_page_items(items, 3)) == 3


def test_page_offsets_and_discovered():
    assert list(page_offsets(20, 2)) == [0, 20]
    found = items_to_discovered(
        [{"url": "https://img.example/a.jpg", "meta": {"title": "a"}}, {"url": ""}],
        keyword="cat",
        source="bing",
    )
    assert len(found) == 1
    assert found[0].source == "bing"


def test_collection_limit_and_page_planning():
    assert collection_limit_reached(
        stopped=False, saved_count=0, total_count=10
    ) is False
    assert collection_limit_reached(
        stopped=True, saved_count=0, total_count=10
    ) is True
    assert collection_limit_reached(
        stopped=False, saved_count=10, total_count=10
    ) is True
    assert collection_limit_reached(
        stopped=False,
        saved_count=1,
        total_count=10,
        scene_target_reached=True,
    ) is True
    assert estimate_pages_needed(100, 20) == 7
    assert estimate_pages_needed(0, 10) == 2
    assert estimate_pages_needed(10_000, 1) == 50
    assert estimate_pages_needed(5, 0) == 7
    wrapped = urls_to_discovered(
        ["https://img.example/a.jpg", "", "https://img.example/b.jpg"],
        keyword="cat",
        source="site:wiki",
    )
    assert [item.url for item in wrapped] == [
        "https://img.example/a.jpg",
        "https://img.example/b.jpg",
    ]
    raw_count, found = discover_page_images(
        [{"url": f"https://x/{i}"} for i in range(5)] + [{"url": ""}],
        keyword="cat",
        source="bing",
        max_pending=3,
    )
    assert raw_count == 6
    assert len(found) == 3
    assert found[0].source == "bing"


def test_drain_inflight_window_runs_and_reports_errors():
    from concurrent.futures import ThreadPoolExecutor

    seen: list[int] = []
    errors: list[BaseException] = []
    inflight: list[int] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        work = iter([1, 2, "boom", 3])

        def spawn():
            try:
                item = next(work)
            except StopIteration:
                return None
            if item == "boom":
                def fail():
                    raise RuntimeError("page")
                return executor.submit(fail)
            return executor.submit(seen.append, item)

        drain_inflight_window(
            spawn,
            window=2,
            on_error=errors.append,
            on_inflight=inflight.append,
        )
    assert sorted(seen) == [1, 2, 3]
    assert any(str(exc) == "page" for exc in errors)
    assert inflight
    try:
        drain_inflight_window(
            lambda: None, window=0, on_error=lambda _exc: None, on_inflight=lambda _n: None
        )
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_crawl_loops_delegate_to_discovery_helpers():
    import inspect

    from smart_spider.dataset_crawler import DatasetCrawler

    search = inspect.getsource(DatasetCrawler._crawl_search_engines)
    fetch = inspect.getsource(DatasetCrawler._fetch_and_process_page)
    site = inspect.getsource(DatasetCrawler._crawl_site)
    tools = inspect.getsource(DatasetCrawler._crawl_spider_tools)
    crawl = inspect.getsource(DatasetCrawler.crawl)
    keywords = inspect.getsource(DatasetCrawler._run_keyword_sources)
    assert "estimate_pages_needed" in search
    assert "drain_inflight_window" in search
    assert "_collection_limit_reached" in search
    assert "discover_page_images" in fetch
    assert "_save_discovered_images" in fetch
    assert "urls_to_discovered" in site
    assert "_build_site_crawler" in site
    assert "spider_tools_to_discovered" in tools
    assert "_run_keyword_sources" in crawl
    assert "_run_site_sources" in crawl
    assert "_run_spider_tools_source" in crawl
    assert "_finish_job" in crawl
    assert "remaining_quota" in crawl
    assert "_collection_limit_reached" in keywords
    assert "bound_page_items(" not in fetch


def test_crawl_plan_remaining_and_progress():
    from smart_spider.dataset_crawl_plan import (
        keyword_progress_snapshot,
        remaining_keywords,
        remaining_quota,
        site_crawler_kwargs,
        spider_tools_to_discovered,
    )

    assert remaining_quota(3, 10) == 7
    assert remaining_quota(10, 10) == 0
    assert remaining_keywords(["cat", "dog", "bird"], ["cat"]) == ["dog", "bird"]
    snapshot = keyword_progress_snapshot(
        keywords=["cat", "dog", "bird"],
        done=["cat", "dog"],
        saved_count=4,
        total_target=10,
    )
    assert snapshot["keywords_done"] == ["cat", "dog"]
    assert snapshot["keywords_remaining"] == ["bird"]
    assert snapshot["saved_count"] == 4
    kwargs = site_crawler_kwargs(
        parser_name="wiki",
        output_dir="/tmp/ds",
        start_page=1,
        end_page=3,
        max_workers=4,
        min_width=200,
        min_height=200,
        timeout=10,
        connect_timeout=None,
        read_timeout=2.5,
        max_image_bytes=1024,
        max_image_pixels=1000,
    )
    assert kwargs["output_dir"].endswith("_site_tmp_wiki")
    assert kwargs["end_page"] == 3
    assert kwargs["read_timeout"] == 2.5

    class Row:
        def __init__(self, url, site, tags):
            self.url = url
            self.site = site
            self.tags = tags

    found = spider_tools_to_discovered(
        [
            Row("https://img.example/a.jpg", "pix", ["cat"]),
            Row("https://img.example/b.jpg", "pix", []),
        ],
        default_keyword="fallback",
    )
    assert found[0].keyword == "cat"
    assert found[0].source == "st:pix"
    assert found[1].keyword == "fallback"


def test_download_window_slots():
    window = DownloadWindow(
        max_inflight_downloads=1,
        max_pending_candidates=1,
        per_domain_concurrency=1,
    )
    assert window.acquire_candidate_slot() is True
    assert window.inflight_candidates == 1
    slot = window.acquire_download_slot("https://img.example/a.jpg")
    assert slot is not None
    assert window.inflight_downloads == 1
    window.release_download_slot(slot)
    window.release_candidate_slot()
    assert window.inflight_downloads == 0
    assert window.inflight_candidates == 0


def test_scene_admission_without_detector_reviews():
    profile = get_scene_quality_profile("未穿反光衣")
    image = Image.new("RGB", (64, 64), (10, 20, 30))
    decision = evaluate_scene_quality(
        enabled=True,
        gate=None,
        scene_profile=profile,
        detector=None,
        image=image,
        semantic_score=0.7,
    )
    assert decision is not None
    assert decision.action == "review"
    assert scene_key("打电话") == "phone_call"
    assert normalize_scene_targets({"吸烟": 2, "smoking": 3})["smoking"] == 5
    profile = get_scene_quality_profile("打电话")
    assert scene_semantic_threshold(
        gate_enabled=False,
        gate=None,
        scene_profile=profile,
        default_threshold=0.3,
    ) == profile.acceptance_score
    assert scene_semantic_threshold(
        gate_enabled=True,
        gate=None,
        scene_profile=profile,
        default_threshold=0.3,
    ) == profile.review_score


def _png_bytes(size: int = 64, color=(10, 80, 160)) -> bytes:
    import io

    image = Image.new("RGB", (size, size), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeStream:
    def __init__(self, remainder: bytes, headers=None):
        self.remainder = remainder
        self.headers = headers or {}

    def iter_content(self, chunk_size=65536):
        yield self.remainder

    def close(self):
        return None


class _FakeHttp:
    def __init__(self, payload: bytes, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def get_stream(self, url, peek_bytes=8192):
        return self.payload[:peek_bytes], _FakeStream(
            self.payload[peek_bytes:], self.headers
        )


def test_ingest_remote_image_accepts_png():
    payload = _png_bytes(80)
    result = ingest_remote_image(
        _FakeHttp(payload),
        "https://img.example/a.png",
        max_file_size=1_000_000,
        max_image_pixels=10_000_000,
        min_file_size=10,
        min_width=32,
        min_height=32,
        min_variance=0.0,
    )
    assert result.ok
    assert result.ext == ".png"
    assert result.image is not None
    assert result.image.size == (80, 80)


def test_ingest_remote_image_rejects_small_and_unknown_ext():
    tiny = _png_bytes(8)
    small = ingest_remote_image(
        _FakeHttp(tiny),
        "https://img.example/tiny.png",
        max_file_size=1_000_000,
        max_image_pixels=10_000_000,
        min_file_size=1,
        min_width=32,
        min_height=32,
        min_variance=0.0,
    )
    assert small.status == "reject"
    assert small.reason == "image_too_small"
    assert small.inc_filtered is True

    unknown = ingest_remote_image(
        _FakeHttp(b"not-an-image-at-all-xxxxx"),
        "https://img.example/x.bin",
        max_file_size=1_000_000,
        max_image_pixels=10_000_000,
        min_file_size=1,
        min_width=1,
        min_height=1,
        min_variance=0.0,
        detect_ext=lambda _peek: ".bin",
    )
    assert unknown.status == "reject"
    assert unknown.reason == "unsupported_image_format"


def test_prepare_output_image_converts_to_jpeg():
    image = Image.new("RGB", (32, 32), (12, 34, 56))
    source = _png_bytes(32)
    content, ext, fmt, conversion = prepare_output_image(
        image,
        source,
        ".png",
        output_format="jpg",
        jpeg_quality=90,
    )
    assert ext == ".jpg"
    assert fmt == "jpeg"
    assert conversion["enabled"] is True
    assert content != source
    kept, kept_ext, kept_fmt, skipped = prepare_output_image(
        image,
        source,
        ".png",
        output_format=None,
        jpeg_quality=90,
    )
    assert kept == source
    assert kept_ext == ".png"
    assert skipped["enabled"] is False
    assert kept_fmt in {"png", "rgb"}


def test_reserve_ingest_quotas_releases_scene_when_source_full():
    scenes = SceneQuotaLedger({"phone_call": 2})
    sources = QuotaLedger(10, max_share=0.1, limits={"bing": 0})
    domains = QuotaLedger(10, max_share=1.0)
    hold, reason = reserve_ingest_quotas(
        scene_quotas=scenes,
        source_quotas=sources,
        domain_quotas=domains,
        scene_key="phone_call",
        source_key="bing",
        domain_key="img.example",
        track_scene=True,
    )
    assert hold is None
    assert reason == "source_quota_reached"
    assert scenes.snapshot()["phone_call"]["pending"] == 0
    assert scenes.snapshot()["phone_call"]["accepted"] == 0


def test_reserve_ingest_quotas_commit_then_release_is_noop():
    scenes = SceneQuotaLedger({"phone_call": 2})
    sources = QuotaLedger(10, max_share=1.0)
    domains = QuotaLedger(10, max_share=1.0)
    hold, reason = reserve_ingest_quotas(
        scene_quotas=scenes,
        source_quotas=sources,
        domain_quotas=domains,
        scene_key="phone_call",
        source_key="bing",
        domain_key="img.example",
        track_scene=True,
    )
    assert reason == ""
    assert hold is not None
    hold.commit()
    assert scenes.snapshot()["phone_call"]["accepted"] == 1
    hold.release()
    assert scenes.snapshot()["phone_call"]["accepted"] == 1
    assert scenes.snapshot()["phone_call"]["pending"] == 0


class _Sample:
    def to_dict(self):
        return {"file": "x.jpg"}


def test_materialize_repository_and_legacy(tmp_path):
    recorded = []

    class FakeRepo:
        def commit_image(self, content, **kwargs):
            sample, _meta = kwargs["build_records"](3, "/out/x.jpg", "x.jpg", "hash")
            return DatasetCommit(
                status="committed", index=3, path="/out/x.jpg", sample=sample
            )

    class FakeDir:
        def record_repository_commit(self, index):
            recorded.append(index)

        def save_content(self, url, ext, content, max_count=None):
            path = str(tmp_path / "legacy.jpg")
            path_obj = tmp_path / "legacy.jpg"
            path_obj.write_bytes(content)
            return 0, path

    def build_records(idx, save_path, relative_path, final_hash):
        return _Sample(), {"index": idx, "file": save_path}

    repo_result = materialize_accepted_image(
        repository=FakeRepo(),
        dir_manager=FakeDir(),
        state_store=None,
        job_id="j",
        output_dir=str(tmp_path),
        manifest_writer=None,
        metadata_writer=None,
        output_content=b"jpeg-bytes",
        source_content=b"png-bytes",
        url="https://img.example/a.jpg",
        output_ext=".jpg",
        candidate_id=None,
        max_count=10,
        build_records=build_records,
        source_hash="unused",
        content_hash="abc",
    )
    assert repo_result.ok
    assert repo_result.index == 3
    assert recorded == [3]

    class Writers:
        def __init__(self):
            self.rows = []

        def write(self, item):
            self.rows.append(item)

    writers = Writers()
    legacy = materialize_accepted_image(
        repository=None,
        dir_manager=FakeDir(),
        state_store=None,
        job_id="j",
        output_dir=str(tmp_path),
        manifest_writer=writers,
        metadata_writer=writers,
        output_content=b"jpeg-bytes",
        source_content=b"png-bytes",
        url="https://img.example/a.jpg",
        output_ext=".jpg",
        candidate_id=None,
        max_count=10,
        build_records=build_records,
        source_hash="unused",
        content_hash="abc",
    )
    assert legacy.ok
    assert legacy.index == 0
    assert len(writers.rows) == 2


def test_materialize_repository_duplicate_is_reject():
    class FakeRepo:
        def commit_image(self, content, **kwargs):
            return DatasetCommit(status="duplicate")

    class FakeDir:
        def record_repository_commit(self, index):
            raise AssertionError("should not record failed commit")

    result = materialize_accepted_image(
        repository=FakeRepo(),
        dir_manager=FakeDir(),
        state_store=None,
        job_id="j",
        output_dir="/tmp",
        manifest_writer=None,
        metadata_writer=None,
        output_content=b"x",
        source_content=b"x",
        url="https://img.example/a.jpg",
        output_ext=".jpg",
        candidate_id=None,
        max_count=1,
        build_records=lambda *args: (_Sample(), {}),
        source_hash="h",
        content_hash="h",
    )
    assert result.reject
    assert result.reason == "duplicate_content"


def test_clip_gate_rejects_below_threshold():
    result = evaluate_clip_similarity(
        enabled=True,
        encode_image=lambda _img: [1.0, 0.0],
        get_text_feature=lambda _kw: [0.0, 1.0],
        cosine_similarity=lambda _left, _right: 0.1,
        image=object(),
        keyword="cat",
        threshold=0.5,
        scene_profile_name="phone_call",
    )
    assert result.accepted is False
    assert result.reason == "clip_low_sim"
    assert result.inc_filtered is True
    assert result.sim == 0.1
    assert result.event_detail["scene_profile"] == "phone_call"


def test_clip_gate_skips_disabled_missing_text_and_errors():
    disabled = evaluate_clip_similarity(
        enabled=False,
        encode_image=lambda _img: [1.0],
        get_text_feature=lambda _kw: [1.0],
        cosine_similarity=lambda _left, _right: 0.99,
        image=object(),
        keyword="cat",
        threshold=0.5,
    )
    assert disabled.accepted is True
    assert disabled.sim == 0.0

    no_text = evaluate_clip_similarity(
        enabled=True,
        encode_image=lambda _img: [1.0],
        get_text_feature=lambda _kw: None,
        cosine_similarity=lambda _left, _right: 0.99,
        image=object(),
        keyword="cat",
        threshold=0.5,
    )
    assert no_text.accepted is True
    assert no_text.sim == 0.0

    def boom(_left, _right):
        raise RuntimeError("cuda")

    errored = evaluate_clip_similarity(
        enabled=True,
        encode_image=lambda _img: [1.0],
        get_text_feature=lambda _kw: [1.0],
        cosine_similarity=boom,
        image=object(),
        keyword="cat",
        threshold=0.5,
    )
    assert errored.accepted is True
    assert errored.sim == 0.0


def test_image_query_gate_none_low_sim_and_exception():
    missing = evaluate_image_query_similarity(
        enabled=True,
        similarity=lambda _image: None,
        image=object(),
        threshold=0.75,
    )
    assert missing.accepted is False
    assert missing.reason == "image_query_inference_error"
    assert missing.inc_filtered is False

    low = evaluate_image_query_similarity(
        enabled=True,
        similarity=lambda _image: 0.2,
        image=object(),
        threshold=0.75,
    )
    assert low.reason == "image_query_low_sim"
    assert low.inc_filtered is True
    assert low.image_sim == 0.2

    def boom(_image):
        raise RuntimeError("encode")

    failed = evaluate_image_query_similarity(
        enabled=True,
        similarity=boom,
        image=object(),
        threshold=0.75,
    )
    assert failed.reason == "image_query_inference_error"


def test_semantic_filters_keep_clip_score_and_short_circuit():
    passed = evaluate_semantic_filters(
        clip_enabled=True,
        encode_image=lambda _image: [1.0],
        get_text_feature=lambda _keyword: [1.0],
        cosine_similarity=lambda _left, _right: 0.88,
        image=object(),
        keyword="cat",
        clip_threshold=0.2,
        image_query_enabled=True,
        image_query_similarity=lambda _image: 0.9,
        image_query_threshold=0.5,
    )
    assert passed.accepted is True
    assert passed.sim == 0.88
    assert passed.image_sim == 0.9

    query_calls = []

    def query(_image):
        query_calls.append(1)
        return 0.99

    blocked = evaluate_semantic_filters(
        clip_enabled=True,
        encode_image=lambda _image: [1.0],
        get_text_feature=lambda _keyword: [1.0],
        cosine_similarity=lambda _left, _right: 0.1,
        image=object(),
        keyword="cat",
        clip_threshold=0.5,
        image_query_enabled=True,
        image_query_similarity=query,
        image_query_threshold=0.5,
    )
    assert blocked.reason == "clip_low_sim"
    assert query_calls == []


def test_admit_ingested_image_short_circuits_scene_on_clip_reject():
    scene_calls = []
    semantic = SemanticGateResult(
        accepted=False,
        reason="clip_low_sim",
        inc_filtered=True,
        sim=0.1,
        event_detail={"reason": "clip_low_sim"},
    )
    result = admit_ingested_image(
        semantic,
        scene_evaluator=lambda score: scene_calls.append(score) or None,
    )
    assert result.accepted is False
    assert result.reason == "clip_low_sim"
    assert result.inc_filtered is True
    assert scene_calls == []
    assert result.scene_decision is None


def test_admit_ingested_image_maps_scene_review():
    class Decision:
        action = "review"

    result = admit_ingested_image(
        SemanticGateResult(accepted=True, sim=0.82, image_sim=0.9),
        scene_evaluator=lambda score: Decision(),
    )
    assert result.accepted is False
    assert result.reason == "scene_quality_review"
    assert result.inc_filtered is False
    assert result.sim == 0.82
    assert result.image_sim == 0.9
    assert result.scene_decision.action == "review"


def test_build_accepted_image_records_and_quality():
    from smart_spider.compliance import CompliancePolicy
    from smart_spider.dataset_contracts import LabelDecision, LabelPolicy, LabelResolution
    from smart_spider.dataset_sample_builder import (
        AcceptedImageFacts,
        bind_record_builder,
        build_accepted_image_records,
        build_image_quality_metrics,
    )

    image = Image.new("RGB", (48, 32), (12, 80, 160))
    thumbnail = __import__("numpy").array(image.resize((32, 32)))
    quality = build_image_quality_metrics(
        image=image,
        thumbnail=thumbnail,
        output_content=b"jpeg-bytes",
        output_format="jpeg",
        scene_profile=None,
        semantic_threshold=0.26,
        scene_decision=None,
    )
    assert quality.width == 48
    assert quality.height == 32
    assert quality.format == "jpeg"
    assert quality.validated is True
    assert quality.phash

    facts = AcceptedImageFacts(
        url="https://img.example/a.png",
        keyword="cat",
        source="bing",
        job_id="job-1",
        sim=0.44,
        output_ext=".jpg",
        source_ext=".png",
        width=48,
        height=32,
        format_conversion={"enabled": True, "to_format": "jpeg"},
    )
    labels = LabelResolution(labels=[LabelDecision("cat", 1.0, "query")])
    sample, metadata = build_accepted_image_records(
        7,
        "/out/batch_0000/0007_abcd.jpg",
        "batch_0000/0007_abcd.jpg",
        "deadbeef",
        facts=facts,
        quality=quality,
        label_resolution=labels,
        label_policy=LabelPolicy(),
        compliance_policy=CompliancePolicy(license="CC0", redact_urls=False),
    )
    assert sample.sample_id == "sha256:deadbeef"
    assert sample.file == "batch_0000/0007_abcd.jpg"
    assert sample.modalities[0].mime_type == "image/jpeg"
    assert metadata["index"] == 7
    assert metadata["batch"] == "batch_0000"
    assert metadata["ext"] == ".jpg"
    assert metadata["source_ext"] == ".png"
    assert metadata["labels"][0]["name"] == "cat"
    assert sample.provenance["license"] == "CC0"

    bound = bind_record_builder(
        facts=facts,
        quality=quality,
        label_resolution=labels,
        label_policy=LabelPolicy(),
        compliance_policy=CompliancePolicy(license="CC0", redact_urls=False),
    )
    sample2, metadata2 = bound(
        7,
        "/out/batch_0000/0007_abcd.jpg",
        "batch_0000/0007_abcd.jpg",
        "deadbeef",
    )
    assert sample2.sample_id == sample.sample_id
    assert sample2.file == sample.file
    assert metadata2 == metadata


def test_download_and_save_is_session_orchestrator():
    import inspect

    from smart_spider.dataset_crawler import DatasetCrawler

    source = inspect.getsource(DatasetCrawler._download_and_save)
    for name in (
        "_begin_image_session",
        "_scene_context",
        "_accepted_ingest",
        "_admit_ingested_image",
        "_commit_admitted_image",
        "_record_saved",
    ):
        assert name in source
    assert "ingest_remote_image(" not in source
    assert "reserve_ingest_quotas(" not in source
    assert "materialize_accepted_image(" not in source
    assert "bind_record_builder(" not in source


def test_download_session_releases_slots_and_url_on_close():
    from smart_spider.dataset_download_session import ImageDownloadSession
    from smart_spider.dedup import UrlDeduplicator

    dedup = UrlDeduplicator()
    window = DownloadWindow(
        max_inflight_downloads=1,
        max_pending_candidates=1,
        per_domain_concurrency=1,
    )
    session = ImageDownloadSession(
        url="https://img.example/a.jpg",
        keyword="cat",
        source="bing",
        job_id="job-1",
        dedup=dedup,
        window=window,
    )
    assert session.claim_url() is True
    assert session.acquire_slots() is None
    assert window.inflight_candidates == 1
    assert window.inflight_downloads == 1
    other = ImageDownloadSession(
        url="https://img.example/a.jpg",
        keyword="cat",
        source="bing",
        job_id="job-1",
        dedup=dedup,
        window=window,
    )
    assert other.claim_url() is False
    session.close()
    assert window.inflight_candidates == 0
    assert window.inflight_downloads == 0
    assert other.claim_url() is True
    other.close()


def test_download_session_commit_then_close_keeps_url_deduped():
    from smart_spider.dataset_download_session import ImageDownloadSession
    from smart_spider.dedup import UrlDeduplicator

    dedup = UrlDeduplicator()
    window = DownloadWindow(
        max_inflight_downloads=1,
        max_pending_candidates=1,
        per_domain_concurrency=1,
    )
    session = ImageDownloadSession(
        url="https://img.example/b.jpg",
        keyword="cat",
        source="bing",
        job_id="job-1",
        dedup=dedup,
        window=window,
    )
    assert session.claim_url() is True
    assert session.claim_content(b"unique-bytes") is True
    session.commit_success()
    session.close()
    again = ImageDownloadSession(
        url="https://img.example/b.jpg",
        keyword="cat",
        source="bing",
        job_id="job-1",
        dedup=dedup,
        window=window,
    )
    assert again.claim_url() is False
    assert again.claim_content(b"unique-bytes") is False


def test_download_session_reject_notifies_state_store():
    from smart_spider.dataset_download_session import ImageDownloadSession
    from smart_spider.dedup import UrlDeduplicator

    class Store:
        def __init__(self):
            self.rejected = None

        def add_candidate(self, job_id, candidate):
            self.job_id = job_id
            self.url = candidate.url
            return "cid-1"

        def reject_candidate(self, candidate_id, reason):
            self.rejected = (candidate_id, reason)

        def fail_candidate(self, candidate_id, error):
            self.failed = (candidate_id, error)

    store = Store()
    session = ImageDownloadSession(
        url="https://img.example/c.jpg",
        keyword="cat",
        source="bing",
        job_id="job-1",
        dedup=UrlDeduplicator(),
        window=DownloadWindow(
            max_inflight_downloads=1,
            max_pending_candidates=1,
            per_domain_concurrency=1,
        ),
        state_store=store,
    )
    session.register_candidate()
    assert session.reject("duplicate_content") is False
    assert store.rejected == ("cid-1", "duplicate_content")
    session.close()


def test_merge_dataset_crawl_stats_optional_fields():
    from smart_spider.dataset_job_report import merge_dataset_crawl_stats

    report = merge_dataset_crawl_stats(
        {"saved": 2, "filtered": 1},
        total_target=10,
        keywords=["cat"],
        search_engines=["bing"],
        site_parsers=["wiki"],
        use_clip=False,
        image_output_format=None,
        jpeg_quality=95,
        max_file_size=1024,
        scene_targets={"phone_call": 1},
        scene_quality_gate={"enabled": False},
        source_quotas={"bing": 1},
        domain_quotas={"example.com": 1},
        batch_size=100,
        batches=["batch_0000"],
        shutdown=False,
        http_metrics={"requests": 3},
        lease_recoveries=4,
        db_stats_error="locked",
    )
    assert report["saved"] == 2
    assert report["image_output_format"] == "original"
    assert report["keywords"] == ["cat"]
    assert report["lease_recoveries"] == 4
    assert report["db_stats_error"] == "locked"
    assert "repository_recovery" not in report
    assert "db_stats" not in report

    with_repo = merge_dataset_crawl_stats(
        {},
        total_target=1,
        keywords=[],
        search_engines=[],
        site_parsers=[],
        use_clip=True,
        image_output_format="jpg",
        jpeg_quality=90,
        max_file_size=1,
        scene_targets={},
        scene_quality_gate={},
        source_quotas={},
        domain_quotas={},
        batch_size=100,
        batches=[],
        shutdown=True,
        http_metrics={},
        lease_recoveries=0,
        repository_recovery={"recovered": 1},
        db_stats={"jobs": 1},
    )
    assert with_repo["repository_recovery"] == {"recovered": 1}
    assert with_repo["db_stats"] == {"jobs": 1}
    assert with_repo["use_clip"] is True
    assert with_repo["shutdown"] is True


def test_attach_dataset_lineage_and_publish_bundle(tmp_path):
    from smart_spider.compliance import CompliancePolicy
    from smart_spider.dataset_job_report import (
        DATASET_REPORT_FILENAME,
        UNIFIED_REPORT_FILENAME,
        attach_dataset_lineage,
        attach_dataset_publish_bundle,
        write_dataset_report_json,
    )

    report = {"saved": 0, "total_target": 1}
    attach_dataset_lineage(
        report,
        job_id="job-report",
        output_dir=str(tmp_path),
        config_snapshot={"keywords": ["cat"]},
        clip_model=None,
        extra_provenance={"saved_count": 0, "compliance": {"license": "CC0"}},
    )
    assert report["lineage"]["dataset_id"].startswith("ds_")
    assert report["manifest_checksum"]["sha256"]
    assert (tmp_path / ".dataset_lineage.json").is_file()

    policy = CompliancePolicy(license="CC0", source_terms="internal-test")
    checklist = attach_dataset_publish_bundle(
        report,
        job_id="job-report",
        output_dir=str(tmp_path),
        policy=policy,
        config_snapshot={"keywords": ["cat"]},
    )
    assert checklist is not None
    assert checklist.ready is True
    assert report["compliance"]["license"] == "CC0"
    unified_path = tmp_path / UNIFIED_REPORT_FILENAME
    assert unified_path.is_file()
    payload = json.loads(unified_path.read_text(encoding="utf-8"))
    assert payload["track"] == "dataset"
    assert payload["job_id"] == "job-report"
    assert payload["extras"]["compliance"]["license"] == "CC0"

    path = write_dataset_report_json(report, str(tmp_path))
    assert path.endswith(DATASET_REPORT_FILENAME)
    saved = json.loads((tmp_path / DATASET_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert saved["unified_report_path"] == str(unified_path)
    assert saved["publish_checklist"]["ready"] is True


def test_attach_dataset_lineage_records_error(monkeypatch, tmp_path):
    from smart_spider import dataset_job_report as mod
    from smart_spider.dataset_job_report import attach_dataset_lineage

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mod, "publish_dataset_artifacts", boom)
    report = {}
    attach_dataset_lineage(
        report,
        job_id="job-err",
        output_dir=str(tmp_path),
        config_snapshot={},
        clip_model=None,
        extra_provenance={},
    )
    assert report["lineage_error"] == "disk full"
    assert "lineage" not in report


def test_generate_report_is_job_report_orchestrator():
    import inspect

    from smart_spider.dataset_crawler import DatasetCrawler

    source = inspect.getsource(DatasetCrawler._generate_report)
    for name in (
        "_collect_job_report_stats",
        "attach_dataset_lineage",
        "attach_dataset_publish_bundle",
        "_persist_job_report",
    ):
        assert name in source
    assert "build_publish_checklist(" not in source
    assert "UnifiedReport.from_dataset_report" not in source
    assert "publish_dataset_artifacts(" not in source


def test_generate_report_writes_artifacts(tmp_path):
    from smart_spider.dataset_crawler import DatasetCrawler

    crawler = DatasetCrawler(
        keywords=["cat"],
        total_count=1,
        output_dir=str(tmp_path),
        use_clip=False,
    )
    events = []
    crawler._callbacks.append(lambda event: events.append(event))
    try:
        report = crawler._generate_report()
        assert report["total_target"] == 1
        assert report["keywords"] == ["cat"]
        assert report["use_clip"] is False
        assert "lineage" in report
        assert crawler.last_publish_checklist is not None
        assert (tmp_path / "_dataset_report.json").is_file()
        assert (tmp_path / "unified_report.json").is_file()
        assert (tmp_path / ".publish_checklist.json").is_file()
        assert any(event.event_type == "crawl_done" for event in events)
        assert events[-1].detail["lease_recoveries"] == 0
    finally:
        crawler.close()
