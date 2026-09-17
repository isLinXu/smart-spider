# coding=utf-8
"""场景配额键与质量门禁求值（与下载/提交解耦）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from .dataset_semantic_gate import SemanticGateResult
from .scene_quality import get_scene_quality_profile
from .scene_quality_gate import GateDecision, SceneQualityGate


def normalize_scene_targets(
    scene_targets: Optional[Mapping[str, int]],
) -> dict[str, int]:
    if scene_targets is None:
        return {}
    if not isinstance(scene_targets, Mapping):
        raise ValueError("scene_targets must be a mapping of scene names to counts")
    result: dict[str, int] = {}
    for raw_name, raw_target in scene_targets.items():
        try:
            name = get_scene_quality_profile(str(raw_name)).name
        except ValueError:
            name = str(raw_name).strip()
        try:
            target = int(raw_target)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid scene target for {raw_name!r}") from exc
        if not name or target <= 0:
            raise ValueError("scene targets need non-empty names and positive counts")
        result[name] = result.get(name, 0) + target
    return result


def scene_key(keyword: str) -> str:
    try:
        return get_scene_quality_profile(keyword).name
    except ValueError:
        return str(keyword or "").strip()


def scene_semantic_threshold(
    *,
    gate_enabled: bool,
    gate: Any,
    scene_profile: Any,
    default_threshold: float,
) -> float:
    """CLIP cosine cutoff used before the optional detector-backed gate."""
    if gate_enabled:
        gate_profile = gate.profile if gate is not None else scene_profile
        return (
            gate_profile.review_score
            if gate_profile is not None
            else default_threshold
        )
    return (
        scene_profile.acceptance_score
        if scene_profile is not None
        else default_threshold
    )


def evaluate_scene_quality(
    *,
    enabled: bool,
    gate: Optional[SceneQualityGate],
    scene_profile: Any,
    detector: Any,
    image: Any,
    semantic_score: float,
) -> Optional[GateDecision]:
    """Fail-closed gate evaluation used before repository commit.

    When the gate is disabled this returns None so ordinary datasets skip it.
    A configured scene without a profile or detector evidence cannot be
    accepted silently.
    """
    if not enabled:
        return None
    active = gate
    if active is None:
        if scene_profile is None:
            raise ValueError(
                "scene quality gate requires a built-in/custom scene profile "
                "or an injected SceneQualityGate"
            )
        active = SceneQualityGate(scene_profile, detector=detector)
    if active.detector is not None:
        return active.evaluate_image(image, semantic_score)
    return active.evaluate(semantic_score, signals={})


@dataclass
class AdmissionResult:
    """Combined CLIP / image-query / scene-quality decision."""

    accepted: bool = True
    reason: str = ""
    inc_filtered: bool = False
    sim: float = 0.0
    image_sim: Optional[float] = None
    event_detail: dict[str, Any] = field(default_factory=dict)
    scene_decision: Optional[GateDecision] = None


def admit_ingested_image(
    semantic: SemanticGateResult,
    *,
    scene_evaluator: Optional[Callable[[float], Optional[GateDecision]]] = None,
) -> AdmissionResult:
    """Apply scene quality only after semantic filters have accepted.

    Scene evaluation is skipped on CLIP / image-query rejection so those
    reasons stay unchanged. A disabled scene gate (evaluator returns None)
    is treated as accept.
    """
    if not semantic.accepted:
        return AdmissionResult(
            accepted=False,
            reason=semantic.reason,
            inc_filtered=semantic.inc_filtered,
            sim=semantic.sim,
            image_sim=semantic.image_sim,
            event_detail=dict(semantic.event_detail),
        )
    scene_decision = None
    if scene_evaluator is not None:
        scene_decision = scene_evaluator(semantic.sim)
    result = AdmissionResult(
        sim=semantic.sim,
        image_sim=semantic.image_sim,
        scene_decision=scene_decision,
    )
    if scene_decision is not None and scene_decision.action != "accept":
        result.accepted = False
        result.reason = (
            "scene_quality_review"
            if scene_decision.action == "review"
            else "scene_quality_reject"
        )
    return result
