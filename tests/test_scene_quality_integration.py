# coding=utf-8
"""Integration-level checks for detector-backed gate wiring."""

import json

from smart_spider.dataset_contracts import (
    CandidateResource,
    Modality,
    ModalityAsset,
    SampleRecord,
)
from smart_spider.multimodal_job import MultimodalDatasetOrchestrator, MultimodalJobConfig
from smart_spider.multimodal_pipeline import DiscoveryTask, SourceResponse


def test_multimodal_gate_rejects_missing_evidence_and_writes_review(tmp_path):
    sample = SampleRecord(
        sample_id="sample-review",
        pipeline={"semantic_score": 0.7},
        modalities=[ModalityAsset(Modality.TEXT, role="context", text="worker")],
    )
    candidate = CandidateResource("https://example.com/page", "test", Modality.WEBPAGE)

    def source(task):
        return SourceResponse(candidates=[candidate], sample=sample, html_length=100)

    job = MultimodalDatasetOrchestrator(
        MultimodalJobConfig(
            output_dir=str(tmp_path),
            scene="未穿反光衣",
            scene_quality_gate_enabled=True,
            materialize_images=False,
        )
    )
    report = job.run([DiscoveryTask(start_url=candidate.url)], source)
    assert report.accepted == 0
    assert report.rejected == 1
    assert report.scene_decisions == {"review": 1}
    queue = tmp_path / "scene_review_queue.jsonl"
    assert queue.exists()
    row = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
    assert row["decision"]["action"] == "review"
    quality = json.loads((tmp_path / "quality_report.json").read_text(encoding="utf-8"))
    assert quality["scene_decisions"] == {"review": 1}
