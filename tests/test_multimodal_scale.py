# coding=utf-8
"""规模化输出、质量报告和批量标注测试。"""
import json
import os
import tempfile

from smart_spider.dataset_contracts import (
    LabelDecision,
    LabelPolicy,
    Modality,
    ModalityAsset,
    SampleRecord,
)
from smart_spider.multimodal_pipeline import AnnotationRouter
from smart_spider.multimodal_scale import QualityReport, ShardedManifestWriter


def _sample(index: int, labels=None):
    return SampleRecord(
        sample_id=f"sample-{index}",
        task_type="image_text_alignment",
        labels=labels or [],
        modalities=[
            ModalityAsset(
                modality=Modality.TEXT,
                role="context",
                text=f"context {index}",
            ),
            ModalityAsset(
                modality=Modality.IMAGE,
                role="image",
                uri=f"assets/{index}.jpg",
            ),
        ],
    )


def test_sharded_manifest_writer_rotates_and_resumes():
    with tempfile.TemporaryDirectory() as tmp:
        writer = ShardedManifestWriter(tmp, shard_size=2)
        for index in range(5):
            writer.write(_sample(index))
        writer.close()

        paths = sorted(path for path in os.listdir(tmp) if path.startswith("manifest-"))
        assert paths == ["manifest-00000.jsonl", "manifest-00001.jsonl", "manifest-00002.jsonl"]
        total = 0
        for path in paths:
            with open(os.path.join(tmp, path), encoding="utf-8") as handle:
                total += sum(1 for line in handle if line.strip())
        assert total == 5

        resumed = ShardedManifestWriter(tmp, shard_size=2)
        resumed.write(_sample(5))
        resumed.close()
        assert os.path.exists(os.path.join(tmp, "manifest-00002.jsonl"))
        with open(os.path.join(tmp, "manifest-00002.jsonl"), encoding="utf-8") as handle:
            assert [json.loads(line)["id"] for line in handle] == ["sample-4", "sample-5"]


def test_quality_report_counts_modalities_tasks_labels_and_rejections():
    report = QualityReport()
    report.observe_route("static")
    report.observe_sample(_sample(1, [LabelDecision("cat"), LabelDecision("outdoor")]))
    report.observe_materialized_image()
    report.observe_rejection("invalid_image")
    report.observe_error("invalid_image")

    data = report.to_dict()
    assert data["accepted"] == 1
    assert data["rejected"] == 1
    assert data["materialized_images"] == 1
    assert data["modality_counts"] == {"image": 1, "text": 1}
    assert data["label_counts"] == {"cat": 1, "outdoor": 1}
    assert data["rejection_reasons"] == {"invalid_image": 1}


class _BatchBackend:
    def __init__(self):
        self.calls = 0

    def annotate_batch(self, samples):
        self.calls += 1
        return {
            sample.sample_id: [LabelDecision("cat", 0.9)]
            for sample in samples
        }


def test_annotation_router_uses_batch_backend_and_preserves_multiple_samples():
    backend = _BatchBackend()
    router = AnnotationRouter(
        policy=LabelPolicy(fixed_labels=["cat"]),
        backends={"batch": backend},
    )
    samples = [_sample(1), _sample(2)]
    results = router.annotate_batch(samples)

    assert backend.calls == 1
    assert set(results) == {"sample-1", "sample-2"}
    assert all([label.name for label in result.resolution.labels] == ["cat"] for result in results.values())
