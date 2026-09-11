# coding=utf-8
"""Scene quality gate truth regression against fixtures (S5)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_spider.scene_quality import get_scene_quality_profile, list_scene_quality_profiles
from smart_spider.scene_quality_gate import SceneQualityGate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scene_gate" / "truth_cases.json"


def _load_cases() -> list[dict]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload.get("format_version") == 1
    cases = payload["cases"]
    assert cases, "truth fixture must not be empty"
    return cases


def test_fixture_covers_all_builtin_profiles():
    profiles = set(list_scene_quality_profiles())
    covered = {case["profile"] for case in _load_cases()}
    assert profiles <= covered, f"missing profiles in fixture: {profiles - covered}"


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_scene_gate_truth_case(case: dict):
    profile = get_scene_quality_profile(case["profile"])
    decision = SceneQualityGate(profile).evaluate(
        float(case["semantic_score"]),
        signals=case.get("signals") or {},
    )
    assert decision.action == case["expected_action"], (
        f"{case['id']}: expected {case['expected_action']}, "
        f"got {decision.action} reasons={decision.reasons}"
    )
