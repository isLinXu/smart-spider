# coding=utf-8
"""数据集契约与多标签策略测试。"""
import json

from smart_spider.dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelMode,
    LabelPolicy,
    Modality,
    ModalityAsset,
    ModalityRelation,
    QualityMetrics,
    SampleRecord,
)


def test_candidate_id_ignores_fragment_and_normalizes_host():
    first = CandidateResource("HTTPS://EXAMPLE.COM/a.jpg#view", "bing")
    second = CandidateResource("https://example.com/a.jpg", "baidu")
    assert first.candidate_id == second.candidate_id


def test_hybrid_policy_keeps_multiple_fixed_labels_and_discovered_candidates():
    policy = LabelPolicy(
        mode=LabelMode.HYBRID,
        fixed_labels=["cat", "outdoor"],
        discovery_threshold=0.7,
    )
    result = policy.resolve([
        LabelDecision("cat", 0.93, "local_clip"),
        LabelDecision("outdoor", 0.81, "ocr"),
        LabelDecision("sunset", 0.88, "vision_model"),
    ])
    assert [item.name for item in result.labels] == ["cat", "outdoor"]
    assert [item.name for item in result.candidates] == ["sunset"]


def test_fixed_policy_rejects_unknown_labels():
    result = LabelPolicy(mode="fixed", fixed_labels=["cat"]).resolve([
        LabelDecision("cat", 0.9),
        LabelDecision("dog", 0.99),
    ])
    assert [item.name for item in result.labels] == ["cat"]
    assert [item.name for item in result.rejected] == ["dog"]


def test_discovery_policy_can_be_promoted_explicitly():
    result = LabelPolicy(
        mode="discovery",
        discovery_threshold=0.8,
        promote_discovered=True,
    ).resolve([LabelDecision("cat", 0.9)])
    assert [item.name for item in result.labels] == ["cat"]
    assert result.candidates == []


def test_sample_record_round_trip_preserves_multi_labels():
    sample = SampleRecord(
        sample_id="sha256:abc",
        file="images/000001.jpg",
        labels=[
            LabelDecision("cat", 0.91, "local_clip"),
            LabelDecision("outdoor", 0.78, "cloud_vision"),
        ],
        quality=QualityMetrics(width=640, height=480, validated=True),
    )
    restored = SampleRecord.from_dict(json.loads(sample.to_json()))
    assert [item.name for item in restored.labels] == ["cat", "outdoor"]
    assert restored.quality.width == 640


def test_multimodal_sample_round_trip_preserves_text_image_relation():
    sample = SampleRecord(
        sample_id="sample-multimodal-1",
        modalities=[
            ModalityAsset(
                modality=Modality.IMAGE,
                role="image",
                uri="images/000001.jpg",
                mime_type="image/jpeg",
                asset_id="img-1",
            ),
            ModalityAsset(
                modality=Modality.TEXT,
                role="caption",
                text="一只猫坐在窗边。",
                metadata={"language": "zh"},
                asset_id="txt-1",
            ),
        ],
        relations=[ModalityRelation("txt-1", "img-1", "caption-of", score=0.97)],
        task_type="image_text_alignment",
    )
    restored = SampleRecord.from_dict(json.loads(sample.to_json()))
    assert [asset.modality for asset in restored.modalities] == [Modality.IMAGE, Modality.TEXT]
    assert restored.modalities[1].text == "一只猫坐在窗边。"
    assert restored.relations[0].relation == "caption-of"
    assert restored.task_type == "image_text_alignment"
