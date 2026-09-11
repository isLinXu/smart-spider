# coding=utf-8
"""config_io / dump-config 测试（S2）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_spider.config_io import dump_mapping, load_dataclass, load_mapping
from smart_spider.dataset_cli import main
from smart_spider.dataset_config import DatasetCrawlConfig


def test_load_json_config_and_override(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(
        json.dumps(
            {
                "keywords": ["cat"],
                "total_count": 10,
                "output_dir": str(tmp_path / "out"),
                "use_clip": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("SMART_SPIDER_TOTAL_COUNT", raising=False)
    cfg = load_dataclass(
        DatasetCrawlConfig,
        path,
        overrides={"total_count": 3},
        use_env=False,
    )
    assert cfg.keywords == ["cat"]
    assert cfg.total_count == 3
    assert cfg.use_clip is False


def test_env_override(tmp_path, monkeypatch):
    path = tmp_path / "job.json"
    path.write_text(
        json.dumps({"keywords": ["dog"], "total_count": 5, "use_clip": False}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SMART_SPIDER_TOTAL_COUNT", "8")
    mapping = load_mapping(path, use_env=True)
    assert mapping["total_count"] == 8


def test_dump_config_cli(tmp_path):
    out = tmp_path / "dump.json"
    main(
        [
            "--keywords",
            "bird",
            "--total",
            "2",
            "--output",
            str(tmp_path / "dataset"),
            "--no-clip",
            "--dump-config",
            str(out),
        ]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["keywords"] == ["bird"]
    assert payload["total_count"] == 2
    assert payload["use_clip"] is False


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("yaml") is None,
    reason="PyYAML not installed",
)
def test_yaml_roundtrip(tmp_path):
    cfg = DatasetCrawlConfig(
        keywords=["x"],
        total_count=1,
        output_dir=str(tmp_path / "o"),
        use_clip=False,
    )
    path = tmp_path / "cfg.yaml"
    dump_mapping(cfg.to_dict(), path)
    loaded = load_dataclass(DatasetCrawlConfig, path, use_env=False)
    assert loaded.keywords == ["x"]
    assert loaded.total_count == 1
