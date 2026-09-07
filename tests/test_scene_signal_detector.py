# coding=utf-8
from PIL import Image

from smart_spider.scene_quality import get_scene_quality_profile
from smart_spider.scene_quality_gate import SceneQualityGate
from smart_spider.scene_signal_detector import (
    HeuristicSceneSignalDetector,
    default_scene_signal_detector,
)


def test_heuristic_detector_returns_named_scores():
    image = Image.new("RGB", (160, 120), color=(180, 120, 90))
    detector = HeuristicSceneSignalDetector()
    signals = detector.detect(image)
    assert set(signals) >= {
        "person",
        "mobile_phone",
        "operational_area",
        "reflective_vest",
        "safety_helmet",
    }
    assert all(0.0 <= float(v) <= 1.0 for v in signals.values())
    assert "heuristic" in detector.model_versions["scene_signal_detector"]


def test_default_detector_is_runnable_without_yolo():
    detector = default_scene_signal_detector(prefer_yolo=False)
    image = Image.new("RGB", (64, 64), color=(40, 40, 40))
    signals = detector.detect(image)
    assert "person" in signals


def test_gate_with_default_detector_produces_decision():
    profile = get_scene_quality_profile("phone_call")
    gate = SceneQualityGate(profile, detector=default_scene_signal_detector(prefer_yolo=False))
    image = Image.new("RGB", (128, 128), color=(200, 140, 110))
    decision = gate.evaluate_image(image, semantic_score=0.30)
    assert decision.action in {"accept", "review", "reject"}
    assert decision.signals
    assert decision.provenance["model_versions"]
