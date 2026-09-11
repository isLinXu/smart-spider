# coding=utf-8
"""Declarative safety scene quality profile tests."""
import json

import pytest

from smart_spider.scene_quality import (
    get_scene_quality_profile,
    list_scene_quality_profiles,
    load_scene_quality_profile,
    resolve_scene_quality_profile,
)
from smart_spider.dataset_filter_cli import _build_parser


def test_builtin_profiles_cover_six_requested_scenes():
    names = set(list_scene_quality_profiles())
    assert {
        "phone_call",
        "mobile_phone_use",
        "smoking",
        "no_reflective_vest",
        "forklift_driver_no_helmet",
        "material_stagnation",
    } <= names
    assert get_scene_quality_profile("未穿反光衣").name == "no_reflective_vest"
    assert get_scene_quality_profile("叉车司机未戴安全帽").prompts.scene_evidence
    assert get_scene_quality_profile("行人").name == "pedestrian"
    assert get_scene_quality_profile("斑马线行人").name == "pedestrian"
    assert get_scene_quality_profile("车辆").name == "road_vehicle"
    assert get_scene_quality_profile("停车场车辆").name == "road_vehicle"
    assert {"pedestrian", "road_vehicle"} <= names


def test_custom_json_profile_is_strict(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({
        "name": "custom",
        "prompts": {
            "positive": ["a worker using a phone"],
            "advertisement": ["a commercial advertisement"],
            "mismatch": ["a product photo"],
            "scene_evidence": ["a worker visibly holding a phone"],
        },
        "thresholds": {"min_relevance": 0.3},
    }), encoding="utf-8")
    profile = load_scene_quality_profile(path)
    assert profile.name == "custom"
    assert profile.thresholds.min_relevance == 0.3
    assert profile.calibration.dataset_id == "custom/custom"
    assert len(profile.fingerprint) == 16
    assert resolve_scene_quality_profile(config_path=path).name == "custom"


def test_builtin_profiles_have_independent_calibration_and_signal_requirements():
    phone = get_scene_quality_profile("phone_call")
    no_vest = get_scene_quality_profile("no_reflective_vest")
    assert phone.thresholds != no_vest.thresholds
    assert phone.calibration.dataset_id != no_vest.calibration.dataset_id
    assert {item.name for item in no_vest.signal_requirements} >= {
        "person", "reflective_vest",
    }


def test_custom_json_profile_rejects_unknown_threshold(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({
        "prompts": {"positive": ["x"], "mismatch": ["y"]},
        "thresholds": {"unknown": 1},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown threshold"):
        load_scene_quality_profile(path)


def test_filter_cli_exposes_scene_profile_selection():
    args = _build_parser().parse_args([
        "--dataset", "dataset",
        "--scene", "未穿反光衣",
    ])
    assert args.scene == "未穿反光衣"
    assert args.quality_config is None
