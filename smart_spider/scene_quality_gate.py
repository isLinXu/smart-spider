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
import math
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
    semantic_score: Optional[float]
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
        semantic_score: Optional[float],
        *,
        signals: Optional[Mapping[str, float]] = None,
        model_versions: Optional[Mapping[str, str]] = None,
        signal_sources: Optional[Mapping[str, str]] = None,
    ) -> GateDecision:
        if semantic_score is not None and not math.isfinite(semantic_score):
            semantic_score = None
        if semantic_score is not None and not -1.0 <= semantic_score <= 1.0:
            raise ValueError("semantic_score must be between -1 and 1")
        normalized = self._normalize_signals(signals or {})
        versions = dict(model_versions or {})
        if self.detector is not None:
            versions = {**dict(self.detector.model_versions), **versions}
        sources = {name: str((signal_sources or {}).get(name, "unknown")) for name in normalized}
        reasons: list[str] = []
        if semantic_score is None:
            reasons.append("semantic_score_unavailable")
        elif semantic_score < self.profile.review_score:
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

        calibration = self.profile.calibration
        calibration_ready = (
            calibration.approved
            and calibration.positive_count > 0 and calibration.negative_count > 0
            and len(calibration.dataset_digest) == 64
            and all(char in "0123456789abcdef" for char in calibration.dataset_digest)
            and calibration.policy_fingerprint == self.profile.policy_fingerprint
            and bool(calibration.model_versions)
            and calibration.model_versions == versions
        )
        untrusted = [requirement.name for requirement in self.profile.signal_requirements
                     if requirement.name in normalized and sources[requirement.name] not in {"detector", "manual"}]
        if semantic_score is not None and semantic_score < self.profile.review_score:
            action = "reject"
        elif semantic_score is None:
            action = "review"
        elif (semantic_score >= self.profile.acceptance_score and requirements_satisfied
              and calibration_ready and not untrusted):
            action = "accept"
        elif self.profile.review_required:
            if semantic_score < self.profile.acceptance_score:
                reasons.append("semantic_score_below_acceptance_threshold")
            action = "review"
        else:
            if semantic_score < self.profile.acceptance_score:
                reasons.append("semantic_score_below_acceptance_threshold")
            action = "reject"
        if action != "reject" or semantic_score is None or semantic_score >= self.profile.review_score:
            if not calibration_ready:
                reasons.append("calibration_unverified")
            reasons.extend(f"untrusted_signal:{name}" for name in untrusted)
        provenance = {
            "profile": self.profile.name,
            "profile_version": self.profile.version,
            "profile_fingerprint": self.profile.fingerprint,
            "policy_fingerprint": self.profile.policy_fingerprint,
            "calibration": asdict(self.profile.calibration),
            "calibration_verified": calibration_ready,
            "acceptance_score": self.profile.acceptance_score,
            "review_score": self.profile.review_score,
            "signal_requirements": [asdict(item) for item in self.profile.signal_requirements],
            "model_versions": dict(sorted(versions.items())),
            "signal_sources": dict(sorted(sources.items())),
        }
        return GateDecision(
            action=action,
            semantic_score=semantic_score,
            reasons=tuple(reasons),
            signals=normalized,
            provenance=provenance,
        )

    def evaluate_image(self, image: Any, semantic_score: Optional[float]) -> GateDecision:
        if self.detector is None:
            return self.evaluate(semantic_score)
        try:
            signals = self.detector.detect(image)
            return self.evaluate(
                semantic_score,
                signals=signals,
                model_versions=self.detector.model_versions,
                signal_sources=getattr(self.detector, "signal_sources", {}),
            )
        except Exception as exc:
            decision = self.evaluate(semantic_score)
            return GateDecision(
                action="review", semantic_score=decision.semantic_score,
                reasons=decision.reasons + ("detector_error",), signals={},
                provenance={**decision.provenance, "detector_error_type": type(exc).__name__},
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
