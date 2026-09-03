# coding=utf-8
"""Declarative semantic quality profiles for operational safety datasets.

Profiles are intentionally versioned in source control rather than hidden in
ad-hoc command lines.  A custom JSON profile can be used for a calibrated
deployment without adding a YAML dependency to the crawler runtime.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from .dataset_filter import FilterThresholds, PromptSet


@dataclass(frozen=True)
class SceneQualityProfile:
    """Prompts and thresholds required to reproduce one safety scene filter."""

    name: str
    description: str
    prompts: PromptSet
    thresholds: FilterThresholds
    review_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "prompts": asdict(self.prompts),
            "thresholds": asdict(self.thresholds),
            "review_required": self.review_required,
        }


def _profile(
    name: str,
    description: str,
    positive: tuple[str, ...],
    mismatch: tuple[str, ...],
    evidence: tuple[str, ...],
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
        thresholds=FilterThresholds.from_profile("precision"),
    )


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
        review_required=True,
    )
