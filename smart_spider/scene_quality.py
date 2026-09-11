# coding=utf-8
"""Declarative semantic quality profiles for operational safety datasets.

Profiles are intentionally versioned in source control rather than hidden in
ad-hoc command lines.  A custom JSON profile can be used for a calibrated
deployment without adding a YAML dependency to the crawler runtime.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .dataset_filter import FilterThresholds, PromptSet


@dataclass(frozen=True)
class SignalRequirement:
    """A detector score constraint required before an image can be accepted."""

    name: str
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def __post_init__(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError("signal requirement needs a minimum or maximum")
        for value in (self.minimum, self.maximum):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError("signal thresholds must be between 0 and 1")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("signal minimum cannot exceed maximum")


@dataclass(frozen=True)
class SceneCalibration:
    """Immutable identity for the held-out calibration set of one scene."""

    dataset_id: str
    version: str
    positive_count: int = 0
    negative_count: int = 0

    def __post_init__(self):
        if not self.dataset_id or not self.version:
            raise ValueError("calibration dataset_id and version are required")
        if self.positive_count < 0 or self.negative_count < 0:
            raise ValueError("calibration counts cannot be negative")


@dataclass(frozen=True)
class SceneQualityProfile:
    """Prompts and thresholds required to reproduce one safety scene filter."""

    name: str
    description: str
    prompts: PromptSet
    thresholds: FilterThresholds
    calibration: SceneCalibration
    signal_requirements: tuple[SignalRequirement, ...] = ()
    acceptance_score: float = 0.0
    review_score: float = 0.0
    version: str = "safety-gate-v1"
    review_required: bool = True

    def __post_init__(self):
        if not self.version:
            raise ValueError("profile version is required")
        if not -1.0 <= self.review_score <= self.acceptance_score <= 1.0:
            raise ValueError("review_score and acceptance_score must be in [-1, 1]")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self._fingerprint_payload(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompts": asdict(self.prompts),
            "thresholds": asdict(self.thresholds),
            "calibration": asdict(self.calibration),
            "signal_requirements": [asdict(item) for item in self.signal_requirements],
            "acceptance_score": self.acceptance_score,
            "review_score": self.review_score,
            "version": self.version,
            "review_required": self.review_required,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._fingerprint_payload()
        payload["fingerprint"] = self.fingerprint
        return payload


def _profile(
    name: str,
    description: str,
    positive: tuple[str, ...],
    mismatch: tuple[str, ...],
    evidence: tuple[str, ...],
    *,
    thresholds: Optional[FilterThresholds] = None,
    signals: tuple[SignalRequirement, ...] = (),
    acceptance_score: float = 0.26,
    review_score: float = 0.21,
) -> SceneQualityProfile:
    return SceneQualityProfile(
        name=name,
        description=description,
        prompts=PromptSet(
            positive=positive,
            advertisement=(
                "a commercial advertisement poster with prices, sales text, or phone numbers",
                "an e-commerce product listing with product specifications and marketing text",
                "a company promotional banner, brochure, logo, QR code, or website screenshot",
                "a collage of product photographs with captions or labels",
            ),
            mismatch=mismatch,
            scene_evidence=evidence,
        ),
        # Safety incident datasets favor precision.  A result is still a
        # screening candidate, especially for absence-based violations.
        thresholds=thresholds or FilterThresholds.from_profile("precision"),
        calibration=SceneCalibration(
            dataset_id=f"safety-scenes/{name}", version="calibration-v1"
        ),
        signal_requirements=signals,
        acceptance_score=acceptance_score,
        review_score=review_score,
    )


def _thresholds(**overrides: float) -> FilterThresholds:
    """Derive independent, reviewable scene thresholds from the precision base."""
    base = asdict(FilterThresholds.from_profile("precision"))
    base.update(overrides)
    return FilterThresholds(**base)


_BUILTIN_PROFILES: dict[str, SceneQualityProfile] = {
    "phone_call": _profile(
        "phone_call",
        "Worker making a phone call in an operational or industrial area.",
        (
            "a worker talking on a mobile phone at an industrial workplace",
            "a warehouse worker holding a smartphone to their ear",
            "a worker making a phone call near machinery, shelves, or a loading area",
        ),
        (
            "a person using a walkie talkie, radio microphone, or headset instead of a phone",
            "a person looking at a smartphone without making a phone call",
            "a phone product photograph, phone advertisement, or screen capture",
            "an office meeting, video call, selfie, or portrait unrelated to industrial work",
            "an empty warehouse, forklift, pallet, vehicle, or industrial equipment with no person",
        ),
        (
            "a worker visibly holding a mobile phone to their ear while working",
            "a warehouse or factory worker making a phone call in an operational area",
        ),
        thresholds=_thresholds(min_relevance=0.24, minimum_scene_evidence=0.24),
        signals=(
            SignalRequirement("person", minimum=0.55),
            SignalRequirement("mobile_phone", minimum=0.45),
            SignalRequirement("operational_area", minimum=0.45),
        ),
        acceptance_score=0.27,
        review_score=0.22,
    ),
    "mobile_phone_use": _profile(
        "mobile_phone_use",
        "Worker using or viewing a mobile phone in an operational area.",
        (
            "a worker using a mobile phone at a warehouse or factory",
            "an industrial worker looking at a smartphone while working",
            "a forklift operator or warehouse worker holding a smartphone",
        ),
        (
            "a worker using a handheld barcode scanner, radio, tablet, or control panel",
            "a phone product image, phone advertisement, mobile app screenshot, or QR code",
            "a person holding a phone in a home, cafe, office, or outdoor leisure setting",
            "an empty industrial workplace with no person using a device",
        ),
        (
            "a worker looking at or operating a mobile smartphone in a warehouse",
            "an industrial worker holding a smartphone during an operational task",
        ),
        thresholds=_thresholds(min_relevance=0.235, minimum_scene_evidence=0.235),
        signals=(
            SignalRequirement("person", minimum=0.55),
            SignalRequirement("mobile_phone", minimum=0.42),
            SignalRequirement("operational_area", minimum=0.42),
        ),
        acceptance_score=0.26,
        review_score=0.21,
    ),
    "smoking": _profile(
        "smoking",
        "Person smoking in a warehouse, factory, loading area, or other operational scene.",
        (
            "a worker smoking a cigarette in a warehouse or factory",
            "a person holding a lit cigarette in an industrial workplace",
            "visible cigarette smoke near a worker at a loading area",
        ),
        (
            "a person holding a pen, straw, tool, food, or vape product advertisement",
            "a cigarette package, tobacco advertisement, ashtray product image, or no smoking sign",
            "steam, dust, fog, exhaust, welding smoke, or factory vapor with no smoking person",
            "a worker wearing a mask or holding a radio near their mouth",
            "an empty warehouse or industrial equipment with no person",
        ),
        (
            "a worker visibly smoking a cigarette at an industrial workplace",
            "a person holding a cigarette with smoke visible in a warehouse or loading area",
        ),
        thresholds=_thresholds(min_relevance=0.245, minimum_scene_evidence=0.245),
        signals=(
            SignalRequirement("person", minimum=0.55),
            SignalRequirement("cigarette_or_smoke", minimum=0.42),
            SignalRequirement("operational_area", minimum=0.4),
        ),
        acceptance_score=0.28,
        review_score=0.23,
    ),
    "no_reflective_vest": _profile(
        "no_reflective_vest",
        "Worker in a traffic or material-handling area without a high-visibility vest.",
        (
            "a warehouse worker without a high visibility reflective safety vest",
            "a worker in a loading area wearing ordinary clothing and no reflective vest",
            "a pedestrian near forklifts or vehicles without a high visibility vest",
        ),
        (
            "a worker wearing a bright high visibility reflective vest",
            "a person outside an industrial, traffic, warehouse, or loading area",
            "an empty warehouse, road, forklift, vehicle, vest product, or safety poster",
            "a worker obscured by distance, darkness, motion blur, or heavy occlusion",
        ),
        (
            "a full or upper-body view of a worker in a vehicle or forklift operating area without a reflective vest",
            "a clearly visible worker near material handling equipment with ordinary non-high-visibility clothing",
        ),
        thresholds=_thresholds(min_relevance=0.25, minimum_scene_evidence=0.25),
        signals=(
            SignalRequirement("person", minimum=0.65),
            SignalRequirement("upper_body", minimum=0.6),
            SignalRequirement("operational_area", minimum=0.5),
            SignalRequirement("reflective_vest", maximum=0.2),
        ),
        acceptance_score=0.29,
        review_score=0.24,
    ),
    "forklift_driver_no_helmet": _profile(
        "forklift_driver_no_helmet",
        "Forklift driver without a safety helmet.",
        (
            "a forklift driver operating a forklift without a safety helmet",
            "a visible forklift operator with an uncovered head in a warehouse",
            "a warehouse forklift driver not wearing a hard hat",
        ),
        (
            "a forklift driver wearing a hard hat or safety helmet",
            "a forklift with no driver, a distant driver, or an occluded driver head",
            "a construction worker, truck driver, pallet jack user, or warehouse worker not operating a forklift",
            "a forklift product photograph, catalog image, safety poster, or illustration",
        ),
        (
            "a clearly visible forklift driver seated at the controls without a helmet",
            "an unobstructed view of a forklift operator head without a hard hat",
        ),
        thresholds=_thresholds(min_relevance=0.26, minimum_scene_evidence=0.26),
        signals=(
            SignalRequirement("forklift", minimum=0.65),
            SignalRequirement("forklift_operator", minimum=0.6),
            SignalRequirement("head", minimum=0.65),
            SignalRequirement("safety_helmet", maximum=0.2),
        ),
        acceptance_score=0.3,
        review_score=0.25,
    ),
    "material_stagnation": _profile(
        "material_stagnation",
        "Materials left in a passage, loading area, exit route, or other operational space.",
        (
            "goods, boxes, pallets, or materials left unattended in a warehouse passage",
            "obstructed warehouse aisle with stored materials blocking a route",
            "pallets or packages lingering in a loading area or emergency access path",
        ),
        (
            "neatly organized warehouse inventory stored on shelves or in designated racks",
            "active loading or unloading where a worker is moving goods",
            "a product catalog image of boxes, pallets, shelves, or storage equipment",
            "an empty aisle, office, retail store, home, or outdoor scene",
        ),
        (
            "materials visibly left on a warehouse floor or aisle obstructing access",
            "unattended boxes or pallets blocking a passage or loading route",
        ),
        thresholds=_thresholds(min_relevance=0.245, minimum_scene_evidence=0.245),
        signals=(
            SignalRequirement("materials", minimum=0.55),
            SignalRequirement("passage_or_exit", minimum=0.5),
            SignalRequirement("obstruction", minimum=0.45),
        ),
        acceptance_score=0.28,
        review_score=0.23,
    ),
    "pedestrian": _profile(
        "pedestrian",
        "Real-world photograph containing at least one visible pedestrian.",
        (
            "a real-world photograph of a pedestrian walking or standing in a street, crossing, sidewalk, station, or public place",
            "a surveillance or street-view photograph with one or more clearly visible people",
            "people walking naturally in an outdoor or public environment",
        ),
        (
            "a book cover, vocabulary card, poster, infographic, screenshot, or image dominated by text",
            "a cartoon, anime image, comic, vector illustration, icon, sketch, painting, or children's drawing",
            "a 3D render, toy, mannequin, miniature model, or abstract human symbol",
            "an empty road, empty sidewalk, landscape, building, or room with no visible person",
            "a cropped face, selfie, studio portrait, or fashion product photograph with no pedestrian scene",
        ),
        (
            "a natural camera photograph with at least one clearly visible person in a real environment",
            "a pedestrian visible from enough of the body to confirm a real person in a real-world scene",
        ),
        thresholds=_thresholds(
            min_relevance=0.215,
            mismatch_margin=0.03,
            text_area_ratio=0.08,
            minimum_scene_evidence=0.19,
            scene_evidence_margin=0.04,
        ),
        signals=(
            SignalRequirement("person_detector", minimum=0.2),
            SignalRequirement("scene_context", minimum=0.15),
            SignalRequirement("synthetic_image", maximum=0.98),
            SignalRequirement("non_photographic", maximum=0.92),
        ),
        acceptance_score=0.23,
        review_score=0.2,
    ),
    "road_vehicle": _profile(
        "road_vehicle",
        "Real-world photograph containing at least one complete road vehicle.",
        (
            "a real-world traffic photograph containing a car, bus, truck, van, motorcycle, or bicycle",
            "a natural road, parking, or work-zone photograph with a clearly visible road vehicle",
            "a complete road vehicle photographed in a real outdoor or operational environment",
        ),
        (
            "a book cover, vocabulary card, poster, infographic, screenshot, or image dominated by text",
            "a cartoon, anime image, comic, vector illustration, icon, sketch, painting, or children's drawing",
            "a 3D render, concept rendering, miniature model, toy vehicle, or video game screenshot",
            "a vehicle advertisement, catalog listing, studio product cutout, or promotional image",
            "a tire, engine, badge, dashboard, vehicle part, or charging equipment with no complete road vehicle",
            "an aircraft, train, ship, industrial machine, or empty road with no visible road vehicle",
        ),
        (
            "a natural camera photograph with a complete car, bus, truck, van, motorcycle, or bicycle in a real environment",
            "a clearly visible road vehicle with realistic lighting, texture, wheels, and surrounding scene",
        ),
        thresholds=_thresholds(
            min_relevance=0.215,
            mismatch_margin=0.03,
            text_area_ratio=0.08,
            minimum_scene_evidence=0.19,
            scene_evidence_margin=0.04,
        ),
        signals=(
            SignalRequirement("road_vehicle_detector", minimum=0.2),
            SignalRequirement("scene_context", minimum=0.15),
            SignalRequirement("synthetic_image", maximum=0.98),
            SignalRequirement("non_photographic", maximum=0.92),
        ),
        acceptance_score=0.23,
        review_score=0.2,
    ),
}

_ALIASES = {
    "打电话": "phone_call",
    "电话": "phone_call",
    "使用手机": "mobile_phone_use",
    "手机": "mobile_phone_use",
    "吸烟": "smoking",
    "未穿反光衣": "no_reflective_vest",
    "反光衣": "no_reflective_vest",
    "叉车司机未戴安全帽": "forklift_driver_no_helmet",
    "叉车未戴安全帽": "forklift_driver_no_helmet",
    "物品滞留": "material_stagnation",
    "滞留": "material_stagnation",
    "行人": "pedestrian",
    "城市道路行人": "pedestrian",
    "斑马线行人": "pedestrian",
    "pedestrians": "pedestrian",
    "车辆": "road_vehicle",
    "道路交通车辆": "road_vehicle",
    "停车场车辆": "road_vehicle",
    "vehicle": "road_vehicle",
    "vehicles": "road_vehicle",
}


def list_scene_quality_profiles() -> tuple[str, ...]:
    return tuple(sorted(_BUILTIN_PROFILES))


def get_scene_quality_profile(name: str) -> SceneQualityProfile:
    key = (name or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    try:
        return _BUILTIN_PROFILES[key]
    except KeyError as exc:
        available = ", ".join(list_scene_quality_profiles())
        raise ValueError(f"unknown scene profile: {name!r}; available: {available}") from exc


def _string_tuple(value: Any, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        if required:
            raise ValueError(f"profile prompts.{field_name} is required")
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"profile prompts.{field_name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _signal_requirements(value: Any) -> tuple[SignalRequirement, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("scene profile signal_requirements must be a list")
    requirements: list[SignalRequirement] = []
    for position, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"signal_requirements[{position}] must be an object")
        unknown = set(item) - {"name", "minimum", "maximum"}
        if unknown:
            raise ValueError(
                "unknown signal requirement fields: " + ", ".join(sorted(unknown))
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f"signal_requirements[{position}].name is required")
        try:
            minimum = None if item.get("minimum") is None else float(item["minimum"])
            maximum = None if item.get("maximum") is None else float(item["maximum"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"signal_requirements[{position}] thresholds must be numbers") from exc
        requirements.append(SignalRequirement(name, minimum=minimum, maximum=maximum))
    return tuple(requirements)


def _calibration(value: Any, name: str) -> SceneCalibration:
    if value is None:
        return SceneCalibration(
            dataset_id=f"custom/{name}",
            version="unverified-custom-v1",
        )
    if not isinstance(value, dict):
        raise ValueError("scene profile calibration must be an object")
    unknown = set(value) - {"dataset_id", "version", "positive_count", "negative_count"}
    if unknown:
        raise ValueError("unknown calibration fields: " + ", ".join(sorted(unknown)))
    try:
        return SceneCalibration(
            dataset_id=str(value.get("dataset_id") or "").strip(),
            version=str(value.get("version") or "").strip(),
            positive_count=int(value.get("positive_count", 0)),
            negative_count=int(value.get("negative_count", 0)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid scene profile calibration") from exc


def load_scene_quality_profile(path: str | Path) -> SceneQualityProfile:
    """Load a custom profile from strict JSON for calibrated deployments."""
    profile_path = Path(path).expanduser()
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read scene profile {profile_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid scene profile JSON {profile_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("scene profile must be a JSON object")
    prompts = payload.get("prompts")
    thresholds = payload.get("thresholds", {})
    if not isinstance(prompts, dict):
        raise ValueError("scene profile prompts must be an object")
    if not isinstance(thresholds, dict):
        raise ValueError("scene profile thresholds must be an object")
    known_thresholds = set(FilterThresholds.__dataclass_fields__)
    unknown = set(thresholds) - known_thresholds
    if unknown:
        raise ValueError(f"unknown threshold fields: {', '.join(sorted(unknown))}")
    name = str(payload.get("name") or profile_path.stem).strip()
    if not name:
        raise ValueError("scene profile name cannot be empty")
    try:
        acceptance_score = float(payload.get("acceptance_score", thresholds.get("min_relevance", 0.0)))
        review_score = float(payload.get("review_score", acceptance_score))
    except (TypeError, ValueError) as exc:
        raise ValueError("scene profile acceptance_score and review_score must be numbers") from exc
    return SceneQualityProfile(
        name=name,
        description=str(payload.get("description") or "Custom scene quality profile").strip(),
        prompts=PromptSet(
            positive=_string_tuple(prompts.get("positive"), "positive", required=True),
            advertisement=_string_tuple(
                prompts.get("advertisement"), "advertisement", required=True
            ),
            mismatch=_string_tuple(prompts.get("mismatch"), "mismatch", required=True),
            scene_evidence=_string_tuple(prompts.get("scene_evidence"), "scene_evidence"),
        ),
        thresholds=FilterThresholds(**thresholds),
        calibration=_calibration(payload.get("calibration"), name),
        signal_requirements=_signal_requirements(payload.get("signal_requirements")),
        acceptance_score=acceptance_score,
        review_score=review_score,
        version=str(payload.get("version") or "custom-safety-gate-v1").strip(),
        review_required=bool(payload.get("review_required", True)),
    )


def resolve_scene_quality_profile(
    *,
    scene: Optional[str] = None,
    config_path: Optional[str | Path] = None,
    query: str = "",
) -> SceneQualityProfile:
    """Resolve a built-in or custom profile; omit both for legacy truck loading."""
    if scene and config_path:
        raise ValueError("scene and config_path cannot be used together")
    if config_path:
        return load_scene_quality_profile(config_path)
    if scene:
        return get_scene_quality_profile(scene)
    return SceneQualityProfile(
        name="truck_loading",
        description="Legacy cargo truck loading and door operation profile.",
        prompts=PromptSet.truck_loading(query),
        thresholds=FilterThresholds.from_profile("balanced"),
        calibration=SceneCalibration("legacy/truck_loading", "legacy-v1"),
        review_required=True,
    )
