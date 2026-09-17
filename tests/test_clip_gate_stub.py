# coding=utf-8
"""CLIP-free scene gate path for CI (semantic score is injected; no torch)."""
from PIL import Image

from smart_spider.dataset_crawler import DatasetCrawler
from smart_spider.scene_quality import get_scene_quality_profile
from smart_spider.scene_quality_gate import SceneQualityGate


class StubDetector:
    """Deterministic detector used when CLIP/YOLO extras are absent."""

    def __init__(self, signals):
        self.signals = dict(signals)
        self.model_versions = {"stub": "ci"}

    def detect(self, image):
        return dict(self.signals)


def test_stub_gate_accepts_complete_signals_without_clip(tmp_path):
    profile = get_scene_quality_profile("smoking")
    detector = StubDetector(
        {
            "person": 0.9,
            "cigarette_or_smoke": 0.8,
            "operational_area": 0.8,
        }
    )
    gate = SceneQualityGate(profile, detector=detector)
    crawler = DatasetCrawler(
        ["smoking"],
        total_count=1,
        output_dir=str(tmp_path),
        use_clip=False,
        state_db="",
        scene_targets={"smoking": 1},
        scene_quality_gate_enabled=True,
        scene_quality_gate=gate,
        scene_signal_detector=detector,
    )
    try:
        image = Image.new("RGB", (256, 256), (40, 40, 40))
        decision = crawler._evaluate_scene_quality(profile, image, 0.72)
        assert decision is not None
        assert decision.action == "accept"
        assert crawler.use_clip is False
        assert crawler.model is None
    finally:
        crawler.close()


def test_semantic_filters_skip_without_clip(tmp_path):
    crawler = DatasetCrawler(
        ["cat"],
        total_count=1,
        output_dir=str(tmp_path),
        use_clip=False,
        state_db="",
    )
    try:
        image = Image.new("RGB", (32, 32), (8, 8, 8))
        result = crawler._evaluate_semantic_filters(
            image, "cat", scene_profile=None, threshold=0.5
        )
        assert result.accepted is True
        assert result.sim == 0.0
        assert result.image_sim is None
        assert crawler.model is None
    finally:
        crawler.close()


def test_stub_gate_reviews_when_semantic_score_is_borderline(tmp_path):
    profile = get_scene_quality_profile("smoking")
    detector = StubDetector(
        {
            "person": 0.9,
            "cigarette_or_smoke": 0.8,
            "operational_area": 0.8,
        }
    )
    crawler = DatasetCrawler(
        ["smoking"],
        total_count=1,
        output_dir=str(tmp_path),
        use_clip=False,
        state_db="",
        scene_targets={"smoking": 1},
        scene_quality_gate_enabled=True,
        scene_quality_gate=SceneQualityGate(profile, detector=detector),
        scene_signal_detector=detector,
    )
    try:
        image = Image.new("RGB", (128, 128), (8, 8, 8))
        decision = crawler._evaluate_scene_quality(
            profile, image, profile.review_score - 0.01
        )
        assert decision.action == "reject"
    finally:
        crawler.close()


def test_admit_ingested_image_without_clip_skips_scene_when_disabled(tmp_path):
    crawler = DatasetCrawler(
        ["cat"],
        total_count=1,
        output_dir=str(tmp_path),
        use_clip=False,
        state_db="",
        scene_quality_gate_enabled=False,
    )
    try:
        image = Image.new("RGB", (32, 32), (8, 8, 8))
        result = crawler._admit_ingested_image(
            image, "cat", scene_profile=None, threshold=0.5
        )
        assert result.accepted is True
        assert result.scene_decision is None
        assert result.sim == 0.0
    finally:
        crawler.close()
