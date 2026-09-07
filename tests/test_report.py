# coding=utf-8
"""UnifiedReport schema tests（S3）。"""
from __future__ import annotations

from smart_spider.report import UnifiedReport


def test_unified_report_roundtrip():
    report = UnifiedReport(
        job_id="abc",
        track="dataset",
        stage_stats={"saved": 10},
        source_stats={"bing": {"success_rate": 0.9}},
    )
    payload = report.to_dict()
    restored = UnifiedReport.from_mapping(payload)
    assert restored.job_id == "abc"
    assert restored.track == "dataset"
    assert restored.stage_stats["saved"] == 10


def test_from_dataset_report_adapter():
    report = UnifiedReport.from_dataset_report(
        {"saved": 3, "total_target": 10, "filtered": 1, "source_metrics": {"a": 1}},
        job_id="j1",
        config_snapshot={"keywords": ["cat"]},
    )
    assert report.track == "dataset"
    assert report.config_snapshot["keywords"] == ["cat"]
    assert report.stage_stats["saved"] == 3
