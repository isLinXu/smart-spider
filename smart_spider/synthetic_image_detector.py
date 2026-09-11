# coding=utf-8
"""Optional whole-image synthetic-content screening for scene quality gates."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

import numpy as np
from PIL import Image

from .dataset_filter_types import PromptSet


DEFAULT_MODEL_ID = (
    "Thermostatic/community-forensics-low-quality-detector-2026-08"
)
DEFAULT_MODEL_REVISION = "6fca3e7f4365363ee5c0fdb1a17d73917d54413d"
DEFAULT_ONNX_FILENAME = "community_forensics_low_quality_fp16.onnx"

_PHOTOGRAPHIC_STYLE_PROMPTS = PromptSet(
    positive=(
        "a photograph",
        "a real photo",
    ),
    advertisement=("text",),
    mismatch=(
        "a drawing",
        "a digital illustration",
        "a 3D render",
        "computer graphics",
        "a screenshot",
        "a collage",
        "an infographic",
    ),
    scene_evidence=("a natural photo",),
)


class OnnxSyntheticImageDetector:
    """Return a calibrated AI-generated probability from a pinned ONNX model.

    The output is a screening signal, not provenance proof.  The scene gate
    deliberately uses it to abstain for human review rather than auto-reject.
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        config_path: Optional[str | Path] = None,
        provider: Optional[str] = None,
        model_id: str = DEFAULT_MODEL_ID,
        revision: str = DEFAULT_MODEL_REVISION,
    ) -> None:
        try:
            import onnxruntime as ort
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "OnnxSyntheticImageDetector requires onnxruntime; "
                "install smart-spider[filter]"
            ) from exc

        model = Path(model_path).expanduser().resolve()
        config = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else model.with_name("config.json")
        )
        if not model.is_file():
            raise FileNotFoundError(model)
        if not config.is_file():
            raise FileNotFoundError(config)
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid synthetic detector config {config}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("synthetic detector config must be a JSON object")

        self._input_size = _positive_int(payload.get("input_size"), "input_size")
        self._resize_short_edge = _positive_int(
            payload.get("resize_short_edge"), "resize_short_edge"
        )
        self._mean = _channel_vector(payload.get("image_mean"), "image_mean")
        self._std = _channel_vector(payload.get("image_std"), "image_std")
        if np.any(self._std <= 0):
            raise ValueError("image_std entries must be positive")
        calibration = payload.get("calibration") or {}
        if not isinstance(calibration, dict):
            raise ValueError("synthetic detector calibration must be an object")
        self._slope = float(calibration.get("slope"))
        self._intercept = float(calibration.get("intercept"))
        expected_sha256 = str(payload.get("onnx_sha256") or "").strip().casefold()
        actual_sha256 = _sha256(model)
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(
                "synthetic detector model checksum mismatch: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )

        providers = [provider] if provider else ["CPUExecutionProvider"]
        self._session = ort.InferenceSession(str(model), providers=providers)
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if len(inputs) != 1 or len(outputs) != 1:
            raise ValueError("synthetic detector must have one input and one output")
        self._input_name = inputs[0].name
        self._versions = {
            "synthetic_image_detector": str(payload.get("candidate_id") or "onnx"),
            "synthetic_model_id": model_id,
            "synthetic_model_revision": revision,
            "synthetic_model_sha256": actual_sha256,
        }

    @property
    def model_versions(self) -> Mapping[str, str]:
        return dict(self._versions)

    def detect(self, image: Any) -> Mapping[str, float]:
        array = self._preprocess(image)[None, ...]
        output = self._session.run(None, {self._input_name: array})[0]
        values = np.asarray(output, dtype=np.float32).reshape(-1)
        if values.size != 1 or not np.isfinite(values[0]):
            raise ValueError("synthetic detector returned an invalid logit")
        calibrated_logit = self._slope * float(values[0]) + self._intercept
        probability = _sigmoid(calibrated_logit)
        return {"synthetic_image": probability}

    def _preprocess(self, image: Any) -> np.ndarray:
        if isinstance(image, Image.Image):
            opened = image.convert("RGB")
        elif isinstance(image, np.ndarray):
            opened = Image.fromarray(np.asarray(image, dtype=np.uint8)).convert("RGB")
        elif hasattr(image, "convert"):
            opened = image.convert("RGB")
        else:
            raise TypeError(f"unsupported image type: {type(image)!r}")
        width, height = opened.size
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        scale = self._resize_short_edge / min(width, height)
        resized = opened.resize(
            (round(width * scale), round(height * scale)),
            Image.Resampling.BICUBIC,
        )
        left = (resized.width - self._input_size) // 2
        top = (resized.height - self._input_size) // 2
        cropped = resized.crop((
            left,
            top,
            left + self._input_size,
            top + self._input_size,
        ))
        pixels = np.asarray(cropped, dtype=np.float32) / 255.0
        pixels = (pixels - self._mean) / self._std
        return np.ascontiguousarray(np.transpose(pixels, (2, 0, 1)), dtype=np.float32)


class CompositeSceneSignalDetector:
    """Merge independent detector outputs without hiding missing evidence."""

    def __init__(self, *detectors: Any) -> None:
        self._detectors = tuple(detector for detector in detectors if detector is not None)
        if not self._detectors:
            raise ValueError("at least one scene signal detector is required")

    @property
    def model_versions(self) -> Mapping[str, str]:
        versions: dict[str, str] = {}
        for detector in self._detectors:
            for name, version in detector.model_versions.items():
                value = str(version)
                if name in versions and versions[name] != value:
                    raise ValueError(f"conflicting detector model version: {name}")
                versions[str(name)] = value
        return versions

    def detect(self, image: Any) -> Mapping[str, float]:
        signals: dict[str, float] = {}
        for detector in self._detectors:
            for raw_name, raw_score in detector.detect(image).items():
                name = str(raw_name).strip()
                score = float(raw_score)
                if not name or not 0.0 <= score <= 1.0:
                    raise ValueError(f"invalid detector signal {raw_name!r}: {raw_score!r}")
                signals[name] = max(signals.get(name, 0.0), score)
        return signals


class CLIPPhotographicStyleDetector:
    """Compare photographic and non-photographic style with topic-free prompts."""

    def __init__(
        self,
        *,
        model_name: str = "ViT-B/32",
        device: str = "auto",
        cache_dir: Optional[str | Path] = None,
        logit_scale: float = 100.0,
    ) -> None:
        from .dataset_filter_scorers import CLIPPromptScorer

        if logit_scale <= 0:
            raise ValueError("style detector logit_scale must be positive")
        self._scorer = CLIPPromptScorer(
            _PHOTOGRAPHIC_STYLE_PROMPTS,
            model_name=model_name,
            device=device,
            cache_dir=cache_dir,
        )
        self._logit_scale = float(logit_scale)
        self._versions = {
            "photographic_style_detector": "clip-topic-free-v1",
            "photographic_style_model": model_name,
            "photographic_style_logit_scale": str(float(logit_scale)),
        }

    @property
    def model_versions(self) -> Mapping[str, str]:
        return dict(self._versions)

    def detect(self, image: Any) -> Mapping[str, float]:
        if not isinstance(image, Image.Image):
            if isinstance(image, np.ndarray):
                image = Image.fromarray(np.asarray(image, dtype=np.uint8))
            elif hasattr(image, "convert"):
                image = image.convert("RGB")
            else:
                raise TypeError(f"unsupported image type: {type(image)!r}")
        scores = self._scorer.score_images([image.convert("RGB")])[0]
        margin = scores.mismatch - scores.relevance
        return {"non_photographic": _sigmoid(self._logit_scale * margin)}


def download_synthetic_image_detector(
    *,
    model_id: str = DEFAULT_MODEL_ID,
    revision: str = DEFAULT_MODEL_REVISION,
    cache_dir: Optional[str | Path] = None,
) -> tuple[Path, Path]:
    """Download only pinned, checksum-verified inference artifacts."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ImportError("synthetic detector download requires huggingface_hub") from exc
    snapshot = Path(snapshot_download(
        repo_id=model_id,
        revision=revision,
        cache_dir=str(cache_dir) if cache_dir is not None else None,
        allow_patterns=["config.json", DEFAULT_ONNX_FILENAME],
    ))
    return snapshot / DEFAULT_ONNX_FILENAME, snapshot / "config.json"


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"synthetic detector {name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"synthetic detector {name} must be positive")
    return parsed


def _channel_vector(value: Any, name: str) -> np.ndarray:
    try:
        vector = np.asarray(value, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"synthetic detector {name} must be numeric") from exc
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"synthetic detector {name} must contain three finite numbers")
    return vector.reshape((1, 1, 3))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)
