# coding=utf-8
"""Default SceneSignalDetector backends for safety-scene quality gates.

The gate protocol historically had no runnable default.  This module closes
that gap with:

* :class:`HeuristicSceneSignalDetector` — CI-safe PIL heuristics (no weights)
* :class:`YoloSceneSignalDetector` — optional ultralytics YOLO backend
* :func:`default_scene_signal_detector` — prefer YOLO when importable

Heuristic scores are intentionally conservative and fail-closed: they emit a
complete named signal vector so the gate can evaluate requirements, but they
are not a substitute for a calibrated detector in production.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np
from PIL import Image


# Signals declared across built-in SceneQualityProfile requirements.
_DEFAULT_SIGNAL_NAMES: tuple[str, ...] = (
    "person",
    "upper_body",
    "head",
    "mobile_phone",
    "operational_area",
    "cigarette_or_smoke",
    "reflective_vest",
    "safety_helmet",
    "forklift",
    "forklift_operator",
)


class HeuristicSceneSignalDetector:
    """Lightweight color / geometry heuristics for named scene signals."""

    def __init__(self, *, version: str = "heuristic-v1") -> None:
        self._versions = {"scene_signal_detector": version}

    @property
    def model_versions(self) -> Mapping[str, str]:
        return dict(self._versions)

    def detect(self, image: Any) -> Mapping[str, float]:
        rgb = self._as_rgb_array(image)
        height, width = rgb.shape[:2]
        if height < 8 or width < 8:
            return {name: 0.0 for name in _DEFAULT_SIGNAL_NAMES}

        # Downsample for cheap stats.
        step_y = max(1, height // 128)
        step_x = max(1, width // 128)
        sample = rgb[::step_y, ::step_x]
        r = sample[:, :, 0].astype(np.float32)
        g = sample[:, :, 1].astype(np.float32)
        b = sample[:, :, 2].astype(np.float32)
        mx = np.maximum(np.maximum(r, g), b)
        mn = np.minimum(np.minimum(r, g), b)
        sat = np.where(mx > 1e-3, (mx - mn) / mx, 0.0)
        val = mx / 255.0

        # Skin-like tones → weak person / upper_body / head proxies.
        skin = (
            (r > 95)
            & (g > 40)
            & (b > 20)
            & ((r - g) > 15)
            & (r > b)
            & (sat > 0.15)
            & (sat < 0.75)
            & (val > 0.25)
        )
        skin_ratio = float(np.mean(skin))
        person = _clamp(skin_ratio * 4.0)
        upper_body = _clamp(person * 0.9)
        head = _clamp(float(np.mean(skin[: max(1, sample.shape[0] // 3)])) * 3.5)

        # High-saturation yellow/orange → vest / helmet / forklift cues.
        yellow = (r > 150) & (g > 120) & (b < 110) & (sat > 0.35) & (val > 0.35)
        yellow_ratio = float(np.mean(yellow))
        top = yellow[: max(1, sample.shape[0] // 3)]
        helmet = _clamp(float(np.mean(top)) * 5.0)
        reflective_vest = _clamp(yellow_ratio * 4.5)

        # Dark compact regions → phone proxy (center band).
        cy0, cy1 = sample.shape[0] // 3, (2 * sample.shape[0]) // 3
        cx0, cx1 = sample.shape[1] // 3, (2 * sample.shape[1]) // 3
        center = val[cy0:cy1, cx0:cx1]
        dark = center < 0.22
        phone = _clamp(float(np.mean(dark)) * 2.2) if center.size else 0.0

        # Grayish mid tones in upper half → smoke proxy.
        upper = sample[: max(1, sample.shape[0] // 2)]
        ur, ug, ub = upper[:, :, 0], upper[:, :, 1], upper[:, :, 2]
        gray = (
            (np.abs(ur.astype(np.float32) - ug) < 18)
            & (np.abs(ug.astype(np.float32) - ub) < 18)
            & (ur > 80)
            & (ur < 200)
        )
        smoke = _clamp(float(np.mean(gray)) * 2.0)

        # Edge-ish industrial clutter via local contrast.
        gray_img = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        if gray_img.shape[0] > 1 and gray_img.shape[1] > 1:
            dx = np.abs(np.diff(gray_img, axis=1)).mean()
            dy = np.abs(np.diff(gray_img, axis=0)).mean()
            operational = _clamp((dx + dy) * 8.0)
        else:
            operational = 0.0

        forklift = _clamp(yellow_ratio * 3.0 + operational * 0.35)
        forklift_operator = _clamp(min(forklift, person) * 1.1)

        return {
            "person": person,
            "upper_body": upper_body,
            "head": head,
            "mobile_phone": phone,
            "operational_area": operational,
            "cigarette_or_smoke": smoke,
            "reflective_vest": reflective_vest,
            "safety_helmet": helmet,
            "forklift": forklift,
            "forklift_operator": forklift_operator,
        }

    @staticmethod
    def _as_rgb_array(image: Any) -> np.ndarray:
        if isinstance(image, np.ndarray):
            arr = image
            if arr.ndim == 2:
                arr = np.stack([arr, arr, arr], axis=-1)
            if arr.shape[-1] == 4:
                arr = arr[:, :, :3]
            return np.asarray(arr, dtype=np.uint8)
        if isinstance(image, Image.Image):
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
        if hasattr(image, "convert"):
            return np.asarray(image.convert("RGB"), dtype=np.uint8)
        raise TypeError(f"unsupported image type: {type(image)!r}")


class YoloSceneSignalDetector:
    """Optional ultralytics YOLO backend mapped onto scene signal names."""

    # COCO-ish class names → scene signals (best-effort).
    _CLASS_TO_SIGNAL = {
        "person": "person",
        "cell phone": "mobile_phone",
        "mobile phone": "mobile_phone",
        "truck": "forklift",
        "forklift": "forklift",
    }

    def __init__(
        self,
        model_name: str = "yolov8n.pt",
        *,
        confidence: float = 0.25,
        device: Optional[str] = None,
    ) -> None:
        try:
            from ultralytics import YOLO  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ImportError(
                "YoloSceneSignalDetector requires ultralytics; "
                "pip install ultralytics"
            ) from exc
        self._model = YOLO(model_name)
        self._confidence = float(confidence)
        self._device = device
        self._versions = {
            "scene_signal_detector": "yolo-v1",
            "yolo_model": model_name,
        }

    @property
    def model_versions(self) -> Mapping[str, str]:
        return dict(self._versions)

    def detect(self, image: Any) -> Mapping[str, float]:
        rgb = HeuristicSceneSignalDetector._as_rgb_array(image)
        # Start from heuristic priors so required keys always exist.
        signals = dict(HeuristicSceneSignalDetector().detect(rgb))
        results = self._model.predict(
            source=rgb,
            conf=self._confidence,
            device=self._device,
            verbose=False,
        )
        if not results:
            return signals
        result = results[0]
        names = getattr(result, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            return signals
        confs = getattr(boxes, "conf", None)
        clss = getattr(boxes, "cls", None)
        if confs is None or clss is None:
            return signals
        for conf, cls_id in zip(confs.tolist(), clss.tolist()):
            label = str(names.get(int(cls_id), "")).casefold()
            signal = self._CLASS_TO_SIGNAL.get(label)
            if signal is None:
                continue
            score = _clamp(float(conf))
            signals[signal] = max(signals.get(signal, 0.0), score)
            if signal == "person":
                signals["upper_body"] = max(signals.get("upper_body", 0.0), score * 0.9)
                signals["head"] = max(signals.get("head", 0.0), score * 0.85)
                signals["forklift_operator"] = max(
                    signals.get("forklift_operator", 0.0),
                    min(signals.get("forklift", 0.0), score),
                )
        return signals


def default_scene_signal_detector(
    *,
    prefer_yolo: bool = False,
    yolo_model: str = "yolov8n.pt",
) -> HeuristicSceneSignalDetector | YoloSceneSignalDetector:
    """Return a runnable default detector.

    Heuristics are the default so enabling the scene gate never downloads
    weights.  Pass ``prefer_yolo=True`` (or set env
    ``SMART_SPIDER_SCENE_DETECTOR=yolo``) when ultralytics is installed.
    """
    import os

    env_pref = os.environ.get("SMART_SPIDER_SCENE_DETECTOR", "").strip().casefold()
    use_yolo = prefer_yolo or env_pref == "yolo"
    if use_yolo:
        try:
            return YoloSceneSignalDetector(model_name=yolo_model)
        except Exception:
            pass
    return HeuristicSceneSignalDetector()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
