# coding=utf-8
"""DatasetCrawlConfig 合规字段测试。"""
from __future__ import annotations

from smart_spider.dataset_config import DatasetCrawlConfig


def test_dataset_config_compliance_policy_and_crawler_kwargs_skip():
    cfg = DatasetCrawlConfig(
        keywords=["cat"],
        total_count=1,
        license="CC0-1.0",
        source_terms="public",
        redact_urls=True,
    )
    policy = cfg.compliance_policy()
    assert policy.license == "CC0-1.0"
    assert "license" not in cfg.crawler_kwargs()
    assert "source_terms" not in cfg.crawler_kwargs()
    assert cfg.to_dict()["license"] == "CC0-1.0"
