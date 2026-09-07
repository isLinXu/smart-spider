# coding=utf-8
"""DatasetCrawlConfig 与 Facade 接线测试。"""
from __future__ import annotations

import pytest

from smart_spider.dataset_config import DatasetCrawlConfig
from smart_spider.dataset_crawler import DatasetCrawler


def test_dataset_crawl_config_validates_keywords():
    with pytest.raises(ValueError):
        DatasetCrawlConfig(keywords=[])


def test_dataset_crawl_config_scene_targets_bound_total():
    with pytest.raises(ValueError):
        DatasetCrawlConfig(
            keywords=["a"],
            total_count=10,
            scene_targets={"x": 20},
        )


def test_dataset_crawler_from_config_sets_config(tmp_path):
    config = DatasetCrawlConfig(
        keywords=["cat"],
        total_count=1,
        output_dir=str(tmp_path / "out"),
        use_clip=False,
        state_db="",
    )
    crawler = DatasetCrawler.from_config(config)
    assert crawler.config.keywords == ["cat"]
    assert crawler.keywords == ["cat"]
    assert crawler.total_count == 1


def test_dataset_crawler_kwargs_still_work(tmp_path):
    crawler = DatasetCrawler(
        keywords=["dog"],
        total_count=2,
        output_dir=str(tmp_path / "out2"),
        use_clip=False,
        state_db="",
    )
    assert crawler.config.keywords == ["dog"]
    assert isinstance(crawler.config, DatasetCrawlConfig)


def test_dataset_crawler_rejects_mixed_config_and_keywords(tmp_path):
    config = DatasetCrawlConfig(
        keywords=["cat"],
        output_dir=str(tmp_path / "out3"),
        use_clip=False,
        state_db="",
    )
    with pytest.raises(TypeError):
        DatasetCrawler(keywords=["x"], config=config)


def test_dataset_crawler_quality_gate_rejects_unknown_scene(tmp_path):
    with pytest.raises(ValueError, match="unknown scenes"):
        DatasetCrawler(
            keywords=["custom scene"],
            total_count=1,
            output_dir=str(tmp_path / "unknown-scene"),
            use_clip=False,
            state_db="",
            scene_quality_gate_enabled=True,
            scene_targets={"custom scene": 1},
        )
