# coding=utf-8
"""CLIP 文本相似与以图搜图二次筛选（不依赖 torch 导入）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from loguru import logger


@dataclass
class SemanticGateResult:
    """Outcome of CLIP and/or image-query filters before scene admission."""

    accepted: bool = True
    reason: str = ""
    inc_filtered: bool = False
    sim: float = 0.0
    image_sim: Optional[float] = None
    event_detail: dict[str, Any] = field(default_factory=dict)


def evaluate_clip_similarity(
    *,
    enabled: bool,
    encode_image: Callable[[Any], Any],
    get_text_feature: Callable[[str], Any],
    cosine_similarity: Callable[[Any, Any], float],
    image: Any,
    keyword: str,
    threshold: float,
    scene_profile_name: Optional[str] = None,
) -> SemanticGateResult:
    """Compare one image to the keyword CLIP vector.

    Inference errors are swallowed (same as the legacy crawler): the candidate
    continues with ``sim=0.0`` instead of being rejected. A missing text
    feature also skips the gate.
    """
    result = SemanticGateResult()
    if not enabled:
        return result
    text_feat = get_text_feature(keyword)
    if text_feat is None:
        return result
    try:
        img_feat = encode_image(image)
        sim = float(cosine_similarity(img_feat, text_feat))
        result.sim = sim
        if sim < threshold:
            result.accepted = False
            result.reason = "clip_low_sim"
            result.inc_filtered = True
            result.event_detail = {
                "sim": round(sim, 4),
                "threshold": threshold,
                "reason": "clip_low_sim",
                "scene_profile": scene_profile_name,
            }
        return result
    except Exception as exc:
        logger.debug(f"CLIP inference error: {exc}")
        return result


def evaluate_image_query_similarity(
    *,
    enabled: bool,
    similarity: Callable[[Any], Optional[float]],
    image: Any,
    threshold: float,
) -> SemanticGateResult:
    """Second-pass visual filter against a query image embedding."""
    if not enabled:
        return SemanticGateResult()
    try:
        image_sim = similarity(image)
        if image_sim is None:
            return SemanticGateResult(
                accepted=False,
                reason="image_query_inference_error",
            )
        result = SemanticGateResult(image_sim=float(image_sim))
        if result.image_sim < threshold:
            result.accepted = False
            result.reason = "image_query_low_sim"
            result.inc_filtered = True
            result.event_detail = {
                "image_sim": round(result.image_sim, 4),
                "image_similarity_threshold": threshold,
                "reason": "image_query_low_sim",
            }
        return result
    except Exception as exc:
        logger.debug(f"Image query inference error: {exc}")
        return SemanticGateResult(
            accepted=False,
            reason="image_query_inference_error",
        )


def evaluate_semantic_filters(
    *,
    clip_enabled: bool,
    encode_image: Callable[[Any], Any],
    get_text_feature: Callable[[str], Any],
    cosine_similarity: Callable[[Any, Any], float],
    image: Any,
    keyword: str,
    clip_threshold: float,
    scene_profile_name: Optional[str] = None,
    image_query_enabled: bool = False,
    image_query_similarity: Optional[Callable[[Any], Optional[float]]] = None,
    image_query_threshold: float = 0.0,
) -> SemanticGateResult:
    """Run CLIP then optional image-query; CLIP score is kept for scene gates."""
    clip_result = evaluate_clip_similarity(
        enabled=clip_enabled,
        encode_image=encode_image,
        get_text_feature=get_text_feature,
        cosine_similarity=cosine_similarity,
        image=image,
        keyword=keyword,
        threshold=clip_threshold,
        scene_profile_name=scene_profile_name,
    )
    if not clip_result.accepted:
        return clip_result
    query_result = evaluate_image_query_similarity(
        enabled=image_query_enabled,
        similarity=image_query_similarity or (lambda _image: None),
        image=image,
        threshold=image_query_threshold,
    )
    query_result.sim = clip_result.sim
    return query_result
