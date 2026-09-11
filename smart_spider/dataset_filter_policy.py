# coding=utf-8
"""Keep / quarantine decision policy for offline dataset filtering."""
from __future__ import annotations

from typing import Optional

from .dataset_filter_types import (
    FilterDecision,
    FilterThresholds,
    SemanticScores,
    VisualSignals,
)


class DecisionPolicy:
    def __init__(self, thresholds: Optional[FilterThresholds] = None) -> None:
        self.thresholds = thresholds or FilterThresholds()

    def decide(
        self,
        *,
        record_position: int,
        path: str,
        scores: SemanticScores,
        signals: VisualSignals,
    ) -> FilterDecision:
        thresholds = self.thresholds
        reasons: list[str] = []
        ad_evidence = 0
        if signals.qr_detected:
            reasons.append("qr_code")
            ad_evidence += 4
        if signals.promotion_hits:
            reasons.append("promotion_terms")
            ad_evidence += 2
        if signals.contact_hits:
            reasons.append("contact_information")
            ad_evidence += min(signals.contact_hits, 2)
        if signals.synthetic_watermark_hits:
            reasons.append("synthetic_watermark")
            # An explicit generator disclosure is sufficient to keep the
            # image out of the accepted set, but remains auditable as a
            # content quarantine rather than an opaque model rejection.
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="synthetic_image",
                reasons=tuple(reasons),
                scores=scores,
                signals=signals,
                confidence="high",
            )
        if signals.text_area_ratio >= thresholds.text_area_ratio:
            reasons.append("text_heavy")
            ad_evidence += 1
        semantic_ad = (
            scores.advertisement >= thresholds.advertisement_score
            and scores.ad_margin >= thresholds.advertisement_margin
        )
        if semantic_ad:
            reasons.append("clip_advertisement")
            ad_evidence += 3
        supported_ad = (
            signals.text_area_ratio >= thresholds.text_area_ratio
            and scores.advertisement >= thresholds.advertisement_support_score
            and scores.ad_margin >= thresholds.advertisement_support_margin
        )
        if supported_ad and not semantic_ad:
            reasons.append("text_supported_advertisement")
            ad_evidence += 2
        if ad_evidence >= 3:
            confidence = "high" if (
                signals.qr_detected
                or bool(signals.promotion_hits)
                or signals.contact_hits > 0
                or scores.ad_margin >= 0.02
            ) else "medium"
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="advertisement",
                reasons=tuple(reasons),
                scores=scores,
                signals=signals,
                confidence=confidence,
            )

        # CLIP can still favor a depicted person/vehicle on book covers and
        # cartoons.  Require two independent cues (a document/illustration
        # negative prompt plus measurable text) before excluding it.
        mismatch_prompt = scores.mismatch_prompt.casefold()
        document_or_illustration = any(marker in mismatch_prompt for marker in (
            "book cover",
            "vocabulary card",
            "poster",
            "infographic",
            "screenshot",
            "cartoon",
            "anime",
            "comic",
            "illustration",
            "children's drawing",
            "3d render",
            "concept rendering",
            "miniature model",
            "toy vehicle",
            "video game screenshot",
            "vector illustration",
            "digital illustration",
            "traffic diagram",
        ))
        if (
            document_or_illustration
            and signals.text_area_ratio >= thresholds.text_area_ratio
            and scores.mismatch >= max(0.22, thresholds.min_relevance)
        ):
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="semantic_mismatch",
                reasons=("document_or_illustration_with_text",),
                scores=scores,
                signals=signals,
                confidence="medium",
            )

        mismatch_reasons: list[str] = []
        if scores.relevance < thresholds.min_relevance:
            mismatch_reasons.append("low_relevance")
        if scores.mismatch_margin >= thresholds.mismatch_margin:
            mismatch_reasons.append("negative_prompt_wins")
        if (
            thresholds.minimum_scene_evidence is not None
            and scores.scene_evidence < thresholds.minimum_scene_evidence
        ):
            mismatch_reasons.append("low_scene_evidence")
        if (
            thresholds.scene_evidence_margin is not None
            and scores.scene_evidence_margin >= thresholds.scene_evidence_margin
        ):
            mismatch_reasons.append("scene_negative_prompt_wins")
        if mismatch_reasons:
            confidence = "high" if (
                scores.relevance < thresholds.min_relevance - 0.03
                or scores.mismatch_margin >= thresholds.mismatch_margin + 0.03
                or (
                    thresholds.minimum_scene_evidence is not None
                    and scores.scene_evidence < thresholds.minimum_scene_evidence - 0.03
                )
                or (
                    thresholds.scene_evidence_margin is not None
                    and scores.scene_evidence_margin >= thresholds.scene_evidence_margin + 0.03
                )
            ) else "medium"
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="semantic_mismatch",
                reasons=tuple(mismatch_reasons),
                scores=scores,
                signals=signals,
                confidence=confidence,
            )
        confidence = "high" if (
            scores.relevance >= thresholds.min_relevance + 0.045
            and scores.ad_margin <= -0.02
            and scores.mismatch_margin <= -0.02
        ) else "medium"
        return FilterDecision(
            record_position=record_position,
            path=path,
            action="keep",
            category="relevant",
            reasons=(),
            scores=scores,
            signals=signals,
            confidence=confidence,
        )
