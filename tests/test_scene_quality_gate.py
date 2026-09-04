# coding=utf-8
"""Regression tests for calibrated, detector-backed scene quality gates."""
import json

import pytest

from smart_spider.scene_quality import get_scene_quality_profile
from smart_spider.scene_quality_gate import JsonlSceneReviewQueue, SceneQualityGate


def test_gate_accepts_only_complete_detector_evidence():
    profile = get_scene_quality_profile("叉车司机未戴安全帽")
    decision = SceneQualityGate(profile).evaluate(
        0.72,
        signals={
            "forklift": 0.82,
            "forklift_operator": 0.77,
            "head": 0.73,
            "safety_helmet": 0.08,
        },
        model_versions={"ppe-detector": "2026.09.01"},
    )

    assert decision.action == "accept"
    assert decision.provenance["profile_fingerprint"] == profile.fingerprint
    assert decision.provenance["calibration"]["dataset_id"] == profile.calibration.dataset_id
    assert decision.provenance["model_versions"] == {"ppe-detector": "2026.09.01"}


def test_absence_based_violation_with_missing_signal_requires_review():
    profile = get_scene_quality_profile("未穿反光衣")
    decision = SceneQualityGate(profile).evaluate(
        0.68,
        signals={
            "person": 0.9,
            "upper_body": 0.8,
            "operational_area": 0.85,
        },
    )

    assert decision.action == "review"
    assert "missing_signal:reflective_vest" in decision.reasons


def test_gate_rejects_low_semantic_score_even_with_detector_evidence():
    profile = get_scene_quality_profile("smoking")
    decision = SceneQualityGate(profile).evaluate(
        profile.review_score - 0.01,
        signals={
            "person": 0.9,
            "cigarette_or_smoke": 0.8,
            "operational_area": 0.8,
        },
    )

    assert decision.action == "reject"
    assert decision.reasons == ("semantic_score_below_review_threshold",)


def test_review_queue_is_durable_and_contains_provenance(tmp_path):
    profile = get_scene_quality_profile("叉车司机未戴安全帽")
    decision = SceneQualityGate(profile).evaluate(0.7, signals={})
    assert decision.action == "review"

    queue = JsonlSceneReviewQueue(tmp_path / "review" / "pending.jsonl")
    queue.append("assets/example.jpg", decision)

    rows = [json.loads(line) for line in queue.path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["sample_ref"] == "assets/example.jpg"
    assert rows[0]["decision"]["provenance"]["profile"] == profile.name
    with pytest.raises(ValueError, match="only review decisions"):
        queue.append("assets/example.jpg", SceneQualityGate(profile).evaluate(
            0.7,
            signals={
                "forklift": 0.9,
                "forklift_operator": 0.9,
                "head": 0.9,
                "safety_helmet": 0.0,
            },
        ))
