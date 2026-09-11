# coding=utf-8
"""离线过滤的提示词、阈值与决策类型（从 dataset_filter 拆出）。"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from PIL import Image

PROMOTION_TERMS = (
    "促销", "优惠", "特价", "厂家直销", "招商", "加盟", "批发", "报价",
    "咨询", "联系", "扫码", "微信", "电话", "广告", "限时", "sale",
    "discount", "promotion", "wholesale", "contact", "call now", "official",
)
CONTACT_PATTERNS = (
    re.compile(r"(?:https?://|www\.)", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)(?:400|800)[- ]?\d{3}[- ]?\d{4}(?!\d)"),
)


@dataclass(frozen=True)
class PromptSet:
    positive: tuple[str, ...]
    advertisement: tuple[str, ...]
    mismatch: tuple[str, ...]
    scene_evidence: tuple[str, ...] = ()

    @classmethod
    def truck_loading(cls, query: str = "货车装卸区 开关门") -> "PromptSet":
        query = query.strip()
        custom_prompts = (query,) if query and query.isascii() else ()
        return cls(
            positive=custom_prompts + (
                "a cargo truck backed into a warehouse loading dock",
                "workers opening or closing the rear doors of a cargo truck at a loading area",
                "workers loading or unloading goods through the open rear doors of a truck",
                "a box truck with rear cargo doors open at a warehouse",
                "a loading bay occupied by a truck or trailer",
                "a delivery truck parked at a loading dock",
                "inside the cargo area of a truck with its rear doors open",
                "a freight trailer being loaded at a warehouse",
            ),
            advertisement=(
                "a commercial advertisement poster with sales text and phone numbers",
                "a company promotional banner or product brochure",
                "an e-commerce product listing with price and marketing text",
                "an industrial product sales poster with specifications",
                "a promotional image with QR code, phone number, or watermark",
                "a collage of product photos with labels",
                "a product cutout on a white background with marketing text",
                "a company logo graphic or website screenshot",
            ),
            mismatch=(
                "an empty warehouse loading dock with no truck",
                "a loading dock leveler, ramp, lift table, or platform shown as a product",
                "a forklift or construction vehicle with no cargo truck",
                "a close-up of a car door handle, hinge, latch, remote control, switch, or spare part",
                "a passenger car, SUV, pickup truck, bus, motorcycle, or vehicle interior",
                "an ambulance, fire truck, emergency vehicle, or rescue scene",
                "a cargo truck driving or parked on a road with no warehouse loading area",
                "a closed cargo truck photographed by itself in a parking lot or vehicle yard",
                "a front, side, or rear exterior view of a truck with its cargo doors closed",
                "a truck accident, breakdown, repair, roadside inspection, or towing scene",
                "a passenger van, car trunk, tailgate, or side door open",
                "a shipping port, container yard, crane, train, or ship with no loading dock",
                "an aerial photograph of a warehouse, port, or industrial park",
                "a warehouse interior with pallets or conveyor belts but no truck",
                "people carrying or sorting packages indoors with no cargo truck visible",
                "a warehouse rolling shutter or loading bay door with no truck present",
                "a close-up of a truck engine, wheel, chassis, suspension, or mechanical part",
                "workers repairing, washing, painting, inspecting, or changing the tire of a truck",
                "a person opening a truck cab door, hood, engine bay, or maintenance panel",
                "a dump truck, garbage truck, crane truck, tanker, or construction truck",
                "a crane lifting a container, machine, or cargo with no warehouse loading dock",
                "cargo being loaded onto or unloaded from an airplane, train, rail wagon, or ship",
                "an airport ground handling or air freight loading scene",
                "people loading construction debris, soil, logs, pipes, or scrap into a dump truck or flatbed",
                "a flatbed truck transporting cargo or machinery away from a warehouse dock",
                "cargo tied down on an open flatbed truck or trailer",
                "a truck carrying cargo on a road or in a vehicle yard",
                "people moving furniture through a house, building doorway, or moving van",
                "a shipping container being inspected, repaired, or handled away from a warehouse dock",
                "a truck tail lift or hydraulic loading platform demonstrated as equipment",
                "a close-up inside a passenger cab or non-cargo compartment",
                "a miniature model, toy, or 3D rendering of a truck loading mechanism",
                "a technical drawing, diagram, floor plan, infographic, or screenshot",
                "a product photograph on a plain white background",
                "a vehicle advertisement or product showcase photographed in a studio",
                "a portrait or fashion photograph",
                "an animal, flower, plant, food, landscape, tourist attraction, or artwork",
                "a computer, household object, tool, machinery part, or unrelated product",
                "a logo or QR code on a plain background",
            ),
            scene_evidence=(
                "workers loading or unloading boxes through open rear cargo doors at a warehouse",
                "a worker opening or closing the rear cargo doors of a truck",
                "a truck with its rear cargo doors visibly open at a loading area",
                "a forklift moving goods through open rear cargo doors at a warehouse loading dock",
                "goods being moved through the open rear doorway of a truck",
                "an open truck cargo compartment visibly framed by its rear doors",
            ),
        )


@dataclass(frozen=True)
class SemanticScores:
    relevance: float
    advertisement: float
    mismatch: float
    relevance_prompt: str = ""
    advertisement_prompt: str = ""
    mismatch_prompt: str = ""
    scene_evidence: float = 0.0
    scene_evidence_prompt: str = ""

    @property
    def ad_margin(self) -> float:
        return self.advertisement - self.relevance

    @property
    def mismatch_margin(self) -> float:
        return self.mismatch - self.relevance

    @property
    def scene_evidence_margin(self) -> float:
        return self.mismatch - self.scene_evidence


@dataclass(frozen=True)
class VisualSignals:
    text_area_ratio: float = 0.0
    text_box_count: int = 0
    qr_detected: bool = False
    ocr_text: str = ""
    promotion_hits: tuple[str, ...] = ()
    contact_hits: int = 0
    # Explicit disclosure terms such as "AI generated" or "AI生成" are
    # stronger evidence than a generic OCR/text-density hit.  They are kept
    # separate so downstream policy can audit why an item was held.
    synthetic_watermark_hits: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilterThresholds:
    min_relevance: float = 0.225
    mismatch_margin: float = 0.015
    advertisement_score: float = 0.235
    advertisement_margin: float = 0.005
    advertisement_support_score: float = 0.215
    advertisement_support_margin: float = -0.04
    text_area_ratio: float = 0.14
    minimum_scene_evidence: Optional[float] = None
    scene_evidence_margin: Optional[float] = None

    @classmethod
    def from_profile(cls, profile: str) -> "FilterThresholds":
        profiles = {
            "conservative": cls(
                min_relevance=0.205,
                mismatch_margin=0.025,
                advertisement_score=0.245,
                advertisement_margin=0.01,
                advertisement_support_score=0.225,
                advertisement_support_margin=-0.025,
                text_area_ratio=0.18,
            ),
            "balanced": cls(),
            "strict": cls(
                min_relevance=0.225,
                mismatch_margin=0.0,
                advertisement_score=0.23,
                advertisement_margin=0.0,
                advertisement_support_score=0.21,
                advertisement_support_margin=-0.05,
                text_area_ratio=0.12,
            ),
            "precision": cls(
                min_relevance=0.225,
                mismatch_margin=0.015,
                advertisement_score=0.23,
                advertisement_margin=0.0,
                advertisement_support_score=0.21,
                advertisement_support_margin=-0.05,
                text_area_ratio=0.12,
                minimum_scene_evidence=0.225,
                scene_evidence_margin=0.005,
            ),
        }
        try:
            return profiles[profile]
        except KeyError as exc:
            raise ValueError(f"unknown filter profile: {profile}") from exc


@dataclass(frozen=True)
class FilterDecision:
    record_position: int
    path: str
    action: str
    category: str
    reasons: tuple[str, ...]
    scores: Optional[SemanticScores] = None
    signals: VisualSignals = field(default_factory=VisualSignals)
    confidence: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["signals"]["promotion_hits"] = list(self.signals.promotion_hits)
        payload["signals"]["synthetic_watermark_hits"] = list(
            self.signals.synthetic_watermark_hits
        )
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FilterDecision":
        raw_scores = payload.get("scores")
        scores = None
        if isinstance(raw_scores, dict) and raw_scores:
            scores = SemanticScores(
                relevance=float(raw_scores.get("relevance") or 0.0),
                advertisement=float(raw_scores.get("advertisement") or 0.0),
                mismatch=float(raw_scores.get("mismatch") or 0.0),
                relevance_prompt=str(raw_scores.get("relevance_prompt") or ""),
                advertisement_prompt=str(raw_scores.get("advertisement_prompt") or ""),
                mismatch_prompt=str(raw_scores.get("mismatch_prompt") or ""),
                scene_evidence=float(raw_scores.get("scene_evidence") or 0.0),
                scene_evidence_prompt=str(raw_scores.get("scene_evidence_prompt") or ""),
            )
        raw_signals = payload.get("signals") or {}
        if not isinstance(raw_signals, dict):
            raw_signals = {}
        promotion = raw_signals.get("promotion_hits") or ()
        signals = VisualSignals(
            text_area_ratio=float(raw_signals.get("text_area_ratio") or 0.0),
            text_box_count=int(raw_signals.get("text_box_count") or 0),
            qr_detected=bool(raw_signals.get("qr_detected")),
            ocr_text=str(raw_signals.get("ocr_text") or ""),
            promotion_hits=tuple(str(item) for item in promotion),
            contact_hits=int(raw_signals.get("contact_hits") or 0),
            synthetic_watermark_hits=tuple(
                str(item) for item in (raw_signals.get("synthetic_watermark_hits") or ())
            ),
        )
        return cls(
            record_position=int(payload.get("record_position") or 0),
            path=str(payload.get("path") or ""),
            action=str(payload.get("action") or "keep"),
            category=str(payload.get("category") or ""),
            reasons=tuple(str(item) for item in (payload.get("reasons") or ())),
            scores=scores,
            signals=signals,
            confidence=str(payload.get("confidence") or ""),
            error=str(payload.get("error") or ""),
        )


class PromptScorer(Protocol):
    def score_images(self, images: Sequence[Image.Image]) -> list[SemanticScores]:
        ...


class SignalAnalyzer(Protocol):
    def analyze(
        self,
        image: Image.Image,
        scores: Optional[SemanticScores] = None,
    ) -> VisualSignals:
        ...
