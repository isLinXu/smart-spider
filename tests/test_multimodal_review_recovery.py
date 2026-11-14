"""Offline review assets, cumulative quotas and explicit enrichment failures."""
import hashlib
import io
import json

import pytest
from PIL import Image

from smart_spider.dataset_contracts import CandidateResource, Modality, ModalityAsset, SampleRecord
from smart_spider.dataset_state import DatasetStateStore
from smart_spider.multimodal_job import AssetStore, MultimodalDatasetOrchestrator, MultimodalJobConfig
from smart_spider.multimodal_pipeline import AnnotationRouter, DiscoveryTask, SourceResponse
from smart_spider.multimodal_repository import MultimodalRepository
from smart_spider.multimodal_scale import ShardedManifestWriter


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (32, 32), (90, 120, 180)).save(output, "PNG")
    return output.getvalue()


def sample(identifier="sample", image=False):
    if image:
        return SampleRecord(
            identifier, file="https://example.com/photo.png",
            modalities=[ModalityAsset(Modality.IMAGE, uri="https://example.com/photo.png", asset_id="image")],
            provenance={"url": "https://example.com/page"},
        )
    return SampleRecord(identifier, modalities=[ModalityAsset(Modality.TEXT, text="context")])


class MissingDetector:
    model_versions = {"scene_signal_detector": "test-empty"}

    def detect(self, image):
        return {}


def test_cumulative_limit_survives_resume_and_allows_only_the_shortfall(tmp_path):
    fetched = []

    def source(task):
        fetched.append(task.start_url)
        return SourceResponse(sample=sample(task.start_url), html_length=1000)

    def run(limit, identifiers):
        job = MultimodalDatasetOrchestrator(MultimodalJobConfig(output_dir=str(tmp_path), max_samples=limit))
        return job.run([DiscoveryTask(start_url=f"https://example.com/{i}") for i in identifiers], source)

    first = run(1, [1, 2])
    resumed = run(1, [2, 3])
    grown = run(3, [2, 3, 4])
    assert (first.accepted, resumed.accepted, grown.accepted) == (1, 0, 2)
    assert (resumed.existing_accepted, resumed.total_accepted) == (1, 1)
    assert (grown.existing_accepted, grown.total_accepted) == (1, 3)
    assert len(fetched) == 3
    assert len((tmp_path / "manifest.jsonl").read_text().splitlines()) == 3


def test_missing_score_keeps_offline_review_asset_and_rebuilds_queue_without_fetch(tmp_path):
    calls = []
    content = image_bytes()
    config = MultimodalJobConfig(output_dir=str(tmp_path), max_samples=1,
                                 scene="no_reflective_vest", scene_quality_gate_enabled=True)
    job = MultimodalDatasetOrchestrator(config, image_fetcher=lambda url: calls.append(url) or content,
                                        scene_signal_detector=MissingDetector())
    report = job.run([DiscoveryTask(start_url="https://example.com/page")],
                     lambda task: SourceResponse(sample=sample(image=True), html_length=1000))
    assert (report.accepted, report.rejected, report.pending_review) == (0, 0, 1)
    queue = tmp_path / "scene_review_queue.jsonl"
    row = json.loads(queue.read_text())
    assert "semantic_score_unavailable" in row["decision"]["reasons"]
    assert row["decision"]["semantic_score"] is None
    assert row["sample"]["pipeline"]["status"] == "needs_review"
    assert "_annotation_image_paths" not in json.dumps(row)
    assert ".staging" not in json.dumps(row)
    asset = row["assets"][0]
    assert (tmp_path / asset["relative_path"]).read_bytes() == content
    assert asset["digest"] == hashlib.sha256(content).hexdigest()
    assert row["sample"]["modalities"][0]["metadata"]["content_hash"] == asset["digest"]
    assert not (tmp_path / "manifest.jsonl").read_text().strip()
    queue.unlink()

    def unexpected(*args):
        pytest.fail("pending review must not rediscover or re-download the page/media")

    job = MultimodalDatasetOrchestrator(config, image_fetcher=unexpected, scene_signal_detector=MissingDetector())
    resumed = job.run([DiscoveryTask(start_url="https://example.com/page")], unexpected)
    assert resumed.skipped_review == 1
    assert resumed.pending_review == 0
    assert len(calls) == 1
    assert len(queue.read_text().splitlines()) == 1
    with DatasetStateStore(str(tmp_path / ".dataset_state.sqlite3")) as state:
        assert not state.list_multimodal_items(config.job_id)
        assert len(state.list_multimodal_reviews(config.job_id)) == 1
        assert state.count_candidates(config.job_id, "needs_review") == 2


def test_review_search_candidate_is_skipped_before_downloading_on_resume(tmp_path):
    config = MultimodalJobConfig(output_dir=str(tmp_path), scene="no_reflective_vest", scene_quality_gate_enabled=True)
    candidate = CandidateResource("https://example.com/photo.png", "test", query="worker")
    calls = []
    for _ in range(2):
        job = MultimodalDatasetOrchestrator(config, image_fetcher=lambda url: calls.append(url) or image_bytes(),
                                            scene_signal_detector=MissingDetector())
        job.run([DiscoveryTask(query="worker")],
                lambda task: SourceResponse(candidates=[candidate], html_length=1000))
    assert calls == [candidate.url]
    assert len((tmp_path / "scene_review_queue.jsonl").read_text().splitlines()) == 1


def test_pending_reviews_have_a_per_run_budget_even_when_nothing_is_accepted(tmp_path):
    config = MultimodalJobConfig(output_dir=str(tmp_path), max_samples=2,
                                 scene="no_reflective_vest", scene_quality_gate_enabled=True)
    calls = []

    def source(task):
        calls.append(task.start_url)
        record = sample(task.start_url)
        record.pipeline["semantic_score"] = float("nan")
        return SourceResponse(sample=record, html_length=1000)

    job = MultimodalDatasetOrchestrator(config, scene_signal_detector=MissingDetector())
    report = job.run([DiscoveryTask(start_url=f"https://example.com/{i}") for i in range(10)], source)
    assert (report.accepted, report.pending_review, len(calls)) == (0, 2, 2)
    assert report.review_limit_reached
    rows = [json.loads(line) for line in (tmp_path / "scene_review_queue.jsonl").read_text().splitlines()]
    assert all(row["sample"]["pipeline"]["semantic_score"] is None for row in rows)


def test_review_prepare_crash_is_recovered_without_promoting_a_training_sample(tmp_path):
    state = DatasetStateStore(str(tmp_path / "state.sqlite3"))
    state.create_job("job", {})
    store = AssetStore(str(tmp_path))
    staged = store.stage_image(image_bytes())
    candidate_id = state.add_candidate("job", CandidateResource("https://example.com/photo.png", "test"))
    state.claim_candidate(candidate_id)
    record = SampleRecord("pending", file=staged.relative_path, pipeline={"status": "needs_review"})
    state.prepare_multimodal_review("job", record, candidate_ids=[candidate_id],
                                    assets=[store.staged_payload(staged)],
                                    decision={"action": "review", "reasons": ["manual_check"]})
    writer = ShardedManifestWriter(str(tmp_path))
    repo = MultimodalRepository(str(tmp_path), state, "job", writer, store)
    try:
        repo.recover()
        assert (tmp_path / staged.relative_path).exists()
        assert state.get_candidate(candidate_id)["state"] == "needs_review"
        assert not state.list_multimodal_items("job")
        assert repo.accepted_count == 0
        assert json.loads((tmp_path / "scene_review_queue.jsonl").read_text())["sample_ref"] == "pending"
    finally:
        writer.close()
        state.close()


def test_missing_prepared_review_asset_releases_candidate_for_retry(tmp_path):
    state = DatasetStateStore(str(tmp_path / "state.sqlite3"))
    state.create_job("job", {})
    store = AssetStore(str(tmp_path))
    staged = store.stage_image(image_bytes())
    candidate_id = state.add_candidate("job", CandidateResource("https://example.com/photo.png", "test"))
    state.claim_candidate(candidate_id)
    record = SampleRecord("pending", file=staged.relative_path)
    state.prepare_multimodal_review("job", record, candidate_ids=[candidate_id],
                                    assets=[store.staged_payload(staged)], decision={"action": "review"})
    store.discard(staged)
    writer = ShardedManifestWriter(str(tmp_path))
    try:
        repo = MultimodalRepository(str(tmp_path), state, "job", writer, store)
        repo.recover()
        assert not state.list_multimodal_reviews("job", ("prepared", "needs_review"))
        assert state.get_candidate(candidate_id)["state"] == "retry_wait"
        assert not repo.has_review("pending")
    finally:
        writer.close()
        state.close()


def test_durable_review_survives_queue_write_failure(tmp_path):
    state = DatasetStateStore(str(tmp_path / "state.sqlite3"))
    state.create_job("job", {})
    store = AssetStore(str(tmp_path))
    staged = store.stage_image(image_bytes())
    writer = ShardedManifestWriter(str(tmp_path))
    repo = MultimodalRepository(str(tmp_path), state, "job", writer, store)
    record = SampleRecord("review", file=staged.relative_path)
    # A directory at the queue path simulates a filesystem publication failure.
    queue = tmp_path / "scene_review_queue.jsonl"
    queue.mkdir()
    try:
        assert repo.retain_review(record, candidate_ids=[], staged_assets=[staged], decision={"action": "review"})
        assert len(state.list_multimodal_reviews("job")) == 1
        assert (tmp_path / staged.relative_path).exists()
        queue.rmdir()
        repo.recover()
        assert json.loads(queue.read_text())["sample_ref"] == "review"
        assert not (tmp_path / "manifest.jsonl").read_text().strip()
    finally:
        writer.close()
        state.close()


@pytest.mark.parametrize("required", [False, True])
def test_annotation_failures_distinguish_advisory_from_required(tmp_path, required):
    class Broken:
        def annotate(self, record):
            raise RuntimeError("test model unavailable")

    config = MultimodalJobConfig(output_dir=str(tmp_path), license="fixture", source_terms="fixture",
                                 required_annotation_backends=("model",) if required else ())
    job = MultimodalDatasetOrchestrator(config, annotation_router=AnnotationRouter(backends={"model": Broken()}))
    report = job.run([DiscoveryTask(start_url="https://example.com/page")],
                     lambda task: SourceResponse(sample=sample(), html_length=1000))
    assert report.degraded_annotations == 1
    if required:
        assert (report.accepted, report.pending_review) == (0, 1)
        assert not (tmp_path / "manifest.jsonl").read_text().strip()
        row = json.loads((tmp_path / "scene_review_queue.jsonl").read_text())
        assert row["sample"]["pipeline"]["annotation"]["status"] == "required_failed"
        assert not json.loads((tmp_path / ".publish_checklist.json").read_text())["ready"]
    else:
        assert (report.accepted, report.pending_review) == (1, 0)
        row = json.loads((tmp_path / "manifest.jsonl").read_text())
        assert row["pipeline"]["annotation"]["status"] == "degraded"


@pytest.mark.parametrize("value", [None, "invalid", float("nan"), float("inf"), 2])
def test_unavailable_semantic_score_is_not_zero(value):
    record = sample()
    record.pipeline["semantic_score"] = value
    assert MultimodalDatasetOrchestrator._sample_semantic_score(record) is None
