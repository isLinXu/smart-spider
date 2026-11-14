# coding=utf-8
"""Regression tests for calibrated, detector-backed scene quality gates."""
import json
from dataclasses import replace

import pytest

from tests.scene_calibration_fixture import calibrated_profile, detector_sources

from smart_spider.scene_quality import get_scene_quality_profile
from smart_spider.scene_quality_gate import JsonlSceneReviewQueue, SceneQualityGate


def test_gate_accepts_only_complete_detector_evidence():
    profile = calibrated_profile(get_scene_quality_profile("叉车司机未戴安全帽"), {"ppe-detector": "2026.09.01"})
    decision = SceneQualityGate(profile).evaluate(
        0.72,
        signal_sources=detector_sources(profile),
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


def test_complete_but_uncalibrated_or_heuristic_evidence_stays_in_review():
    profile = get_scene_quality_profile("smoking")
    signals = {"person": 0.9, "cigarette_or_smoke": 0.8, "operational_area": 0.8}
    uncalibrated = SceneQualityGate(profile).evaluate(
        0.72, signals=signals,
        signal_sources={name: "detector" for name in signals},
        model_versions={"stub": "ci"},
    )
    heuristic = SceneQualityGate(profile).evaluate(
        0.72, signals=signals,
        signal_sources={name: "heuristic" for name in signals},
        model_versions={"stub": "ci"},
    )
    assert uncalibrated.action == heuristic.action == "review"
    assert "calibration_unverified" in uncalibrated.reasons
    assert "untrusted_signal:person" in heuristic.reasons


def test_missing_semantic_score_and_detector_errors_go_to_review():
    profile = get_scene_quality_profile("smoking")
    missing = SceneQualityGate(profile).evaluate(None)
    assert missing.action == "review"
    assert "semantic_score_unavailable" in missing.reasons

    class BrokenDetector:
        model_versions = {"broken": "fixture"}

        def detect(self, _image):
            raise RuntimeError("fixture failure")

    failed = SceneQualityGate(profile, BrokenDetector()).evaluate_image(object(), 0.8)
    assert failed.action == "review"
    assert "detector_error" in failed.reasons
    assert failed.provenance["detector_error_type"] == "RuntimeError"


def test_reject_without_review_policy_still_explains_unverified_calibration():
    profile = replace(get_scene_quality_profile("smoking"), review_required=False)
    decision = SceneQualityGate(profile).evaluate(
        0.8,
        signals={"person": 0.9, "cigarette_or_smoke": 0.8, "operational_area": 0.8},
    )
    assert decision.action == "reject"
    assert "calibration_unverified" in decision.reasons


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
            0.01,
            signals={
                "forklift": 0.9,
                "forklift_operator": 0.9,
                "head": 0.9,
                "safety_helmet": 0.0,
            },
        ))


def test_generic_scene_profiles_require_detector_evidence():
    pedestrian = calibrated_profile(get_scene_quality_profile("行人"), {"yolo_model": "fixture.pt"})
    accepted = SceneQualityGate(pedestrian).evaluate(
        0.3,
        signal_sources=detector_sources(pedestrian),
        signals={
            "person_detector": 0.88,
            "scene_context": 0.9,
            "synthetic_image": 0.02,
            "non_photographic": 0.02,
        },
        model_versions={"yolo_model": "fixture.pt"},
    )
    uncertain = SceneQualityGate(pedestrian).evaluate(
        0.3,
        signals={
            "person_detector": 0.0,
            "scene_context": 0.9,
            "synthetic_image": 0.02,
            "non_photographic": 0.02,
        },
    )

    assert accepted.action == "accept"
    assert uncertain.action == "review"
    assert "signal_below_minimum:person_detector" in uncertain.reasons


def test_generic_scene_profile_routes_synthetic_image_to_review():
    profile = get_scene_quality_profile("车辆")
    decision = SceneQualityGate(profile).evaluate(
        0.3,
        signals={
            "road_vehicle_detector": 0.9,
            "scene_context": 0.9,
            "synthetic_image": 0.999,
            "non_photographic": 0.02,
        },
    )

    assert decision.action == "review"
    assert "signal_above_maximum:synthetic_image" in decision.reasons


def test_generic_scene_profile_routes_non_photographic_style_to_review():
    profile = get_scene_quality_profile("车辆")
    decision = SceneQualityGate(profile).evaluate(
        0.3,
        signals={
            "road_vehicle_detector": 0.9,
            "scene_context": 0.9,
            "synthetic_image": 0.01,
            "non_photographic": 0.95,
        },
    )

    assert decision.action == "review"
    assert "signal_above_maximum:non_photographic" in decision.reasons
