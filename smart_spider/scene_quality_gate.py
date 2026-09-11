# coding=utf-8
"""Auditable quality gates for operational safety scene datasets.

Semantic similarity is useful for candidate retrieval but is not evidence that
an absence-based violation is visible.  This module combines that candidate
score with named signals from a dedicated detector.  An unavailable detector
or an incomplete signal vector always produces a review decision, never an
automatic acceptance.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from .scene_quality import SceneQualityProfile


class SceneSignalDetector(Protocol):
    """Adapter boundary for detectors such as person/PPE/forklift models."""

    @property
    def model_versions(self) -> Mapping[str, str]:
        ...

    def detect(self, image: Any) -> Mapping[str, float]:
        ...


@dataclass(frozen=True)
class GateDecision:
    action: str
    semantic_score: float
    reasons: tuple[str, ...]
    signals: dict[str, float]
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "semantic_score": self.semantic_score,
            "reasons": list(self.reasons),
            "signals": dict(sorted(self.signals.items())),
            "provenance": self.provenance,
        }


class SceneQualityGate:
    """Accept only complete, calibrated evidence; queue uncertainty for review."""

    def __init__(
        self,
        profile: SceneQualityProfile,
        detector: Optional[SceneSignalDetector] = None,
    ) -> None:
        self.profile = profile
        self.detector = detector

    def evaluate(
        self,
        semantic_score: float,
        *,
        signals: Optional[Mapping[str, float]] = None,
        model_versions: Optional[Mapping[str, str]] = None,
    ) -> GateDecision:
        if not -1.0 <= semantic_score <= 1.0:
            raise ValueError("semantic_score must be between -1 and 1")
        normalized = self._normalize_signals(signals or {})
        versions = dict(model_versions or {})
        if self.detector is not None:
            versions = {**dict(self.detector.model_versions), **versions}
        reasons: list[str] = []
        if semantic_score < self.profile.review_score:
            reasons.append("semantic_score_below_review_threshold")

        requirements_satisfied = True
        for requirement in self.profile.signal_requirements:
            score = normalized.get(requirement.name)
            if score is None:
                requirements_satisfied = False
                reasons.append(f"missing_signal:{requirement.name}")
            elif requirement.minimum is not None and score < requirement.minimum:
                requirements_satisfied = False
                reasons.append(f"signal_below_minimum:{requirement.name}")
            elif requirement.maximum is not None and score > requirement.maximum:
                requirements_satisfied = False
                reasons.append(f"signal_above_maximum:{requirement.name}")

        if semantic_score < self.profile.review_score:
            action = "reject"
        elif semantic_score >= self.profile.acceptance_score and requirements_satisfied:
            action = "accept"
        elif self.profile.review_required:
            if semantic_score < self.profile.acceptance_score:
                reasons.append("semantic_score_below_acceptance_threshold")
            action = "review"
        else:
            action = "reject"
        provenance = {
            "profile": self.profile.name,
            "profile_version": self.profile.version,
            "profile_fingerprint": self.profile.fingerprint,
            "calibration": asdict(self.profile.calibration),
            "acceptance_score": self.profile.acceptance_score,
            "review_score": self.profile.review_score,
            "signal_requirements": [asdict(item) for item in self.profile.signal_requirements],
            "model_versions": dict(sorted(versions.items())),
        }
        return GateDecision(
            action=action,
            semantic_score=semantic_score,
            reasons=tuple(reasons),
            signals=normalized,
            provenance=provenance,
        )

    def evaluate_image(self, image: Any, semantic_score: float) -> GateDecision:
        if self.detector is None:
            return self.evaluate(semantic_score)
        return self.evaluate(
            semantic_score,
            signals=self.detector.detect(image),
            model_versions=self.detector.model_versions,
        )

    @staticmethod
    def _normalize_signals(signals: Mapping[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for name, raw_score in signals.items():
            key = str(name).strip()
            if not key:
                raise ValueError("signal names cannot be empty")
            try:
                score = float(raw_score)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"signal {key!r} must be numeric") from exc
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"signal {key!r} must be between 0 and 1")
            normalized[key] = score
        return normalized


class JsonlSceneReviewQueue:
    """Durable append-only journal for low-confidence scene decisions."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def append(self, sample_ref: str, decision: GateDecision) -> None:
        if decision.action != "review":
            raise ValueError("only review decisions may be enqueued")
        record = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sample_ref": str(sample_ref),
            "decision": decision.to_dict(),
        }
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
