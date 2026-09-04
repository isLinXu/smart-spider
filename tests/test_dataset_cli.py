# coding=utf-8
"""Dataset crawler CLI parameter contracts."""

import pytest

from smart_spider.dataset_cli import _parse_scene_targets


def test_parse_scene_targets_accumulates_repeated_aliases():
    assert _parse_scene_targets("打电话=2, 使用手机=3,打电话=4") == {
        "打电话": 6,
        "使用手机": 3,
    }


@pytest.mark.parametrize("raw", ["打电话", "打电话=0", "=2", "打电话=-1", "打电话=x"])
def test_parse_scene_targets_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        _parse_scene_targets(raw)
