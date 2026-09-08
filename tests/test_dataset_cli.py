# coding=utf-8
"""Dataset crawler CLI parameter contracts."""

import pytest

from smart_spider.dataset_cli import _parse_scene_targets
from smart_spider.dataset_cli import main as dataset_cli_main


def test_parse_scene_targets_accumulates_repeated_aliases():
    assert _parse_scene_targets("打电话=2, 使用手机=3,打电话=4") == {
        "打电话": 6,
        "使用手机": 3,
    }


@pytest.mark.parametrize("raw", ["打电话", "打电话=0", "=2", "打电话=-1", "打电话=x"])
def test_parse_scene_targets_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        _parse_scene_targets(raw)


def test_dataset_cli_requires_scene_targets_for_quality_gate():
    with pytest.raises(SystemExit):
        dataset_cli_main([
            "--keywords", "smoking",
            "--total", "1",
            "--no-clip",
            "--scene-quality-gate",
        ])


def test_dataset_cli_accepts_compliance_flags_in_help():
    with pytest.raises(SystemExit) as exc:
        dataset_cli_main(["--help"])
    assert exc.value.code == 0
