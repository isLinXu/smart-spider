"""Synthetic policy fixture only: does not certify production model accuracy."""
from dataclasses import replace
from smart_spider.scene_quality import SceneCalibration


def calibrated_profile(profile, versions=None):
    return replace(profile, calibration=SceneCalibration(
        dataset_id="synthetic-policy-test", version="fixture-v1",
        positive_count=1, negative_count=1, approved=True,
        dataset_digest="a" * 64, policy_fingerprint=profile.policy_fingerprint,
        model_versions=versions or {"stub": "ci"},
    ))


def detector_sources(profile):
    return {item.name: "detector" for item in profile.signal_requirements}
