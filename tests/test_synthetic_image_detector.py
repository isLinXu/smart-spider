# coding=utf-8
import math

import numpy as np
import pytest
from PIL import Image

from smart_spider.synthetic_image_detector import (
    CLIPPhotographicStyleDetector,
    CompositeSceneSignalDetector,
    OnnxSyntheticImageDetector,
)
from smart_spider.dataset_filter_types import SemanticScores


class _Session:
    def run(self, outputs, inputs):
        assert outputs is None
        assert inputs["images"].shape == (1, 3, 4, 4)
        assert inputs["images"].dtype == np.float32
        return [np.asarray([[2.0]], dtype=np.float32)]


def test_onnx_detector_calibrates_single_logit():
    detector = object.__new__(OnnxSyntheticImageDetector)
    detector._input_size = 4
    detector._resize_short_edge = 5
    detector._mean = np.asarray([0.5, 0.5, 0.5], dtype=np.float32).reshape(1, 1, 3)
    detector._std = np.asarray([0.25, 0.25, 0.25], dtype=np.float32).reshape(1, 1, 3)
    detector._slope = 0.5
    detector._intercept = -0.25
    detector._session = _Session()
    detector._input_name = "images"
    detector._versions = {"synthetic_image_detector": "fixture"}

    score = detector.detect(Image.new("RGB", (8, 6), (128, 128, 128)))[
        "synthetic_image"
    ]

    assert score == math.exp(0.75) / (1.0 + math.exp(0.75))


def test_composite_scene_detector_merges_signals_and_provenance():
    class Detector:
        def __init__(self, signals, versions):
            self.signals = signals
            self._versions = versions

        @property
        def model_versions(self):
            return self._versions

        def detect(self, image):
            return self.signals

    detector = CompositeSceneSignalDetector(
        Detector({"road_vehicle_detector": 0.8}, {"yolo_model": "fixture.pt"}),
        Detector({"synthetic_image": 0.03}, {"synthetic_image_detector": "fixture"}),
    )

    assert detector.detect(Image.new("RGB", (8, 8))) == {
        "road_vehicle_detector": 0.8,
        "synthetic_image": 0.03,
    }
    assert detector.model_versions == {
        "yolo_model": "fixture.pt",
        "synthetic_image_detector": "fixture",
    }


def test_clip_style_detector_converts_margin_to_probability():
    class Scorer:
        def score_images(self, images):
            assert len(images) == 1
            return [SemanticScores(0.20, 0.0, 0.23)]

    detector = object.__new__(CLIPPhotographicStyleDetector)
    detector._scorer = Scorer()
    detector._logit_scale = 100.0
    detector._versions = {"photographic_style_detector": "fixture"}

    score = detector.detect(Image.new("RGB", (8, 8)))["non_photographic"]

    assert score == pytest.approx(math.exp(3.0) / (1.0 + math.exp(3.0)))
