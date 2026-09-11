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


def test_from_multimodal_report_includes_routes_and_blocks():
    report = UnifiedReport.from_multimodal_report(
        {
            "job_id": "mm1",
            "tasks": 2,
            "accepted": 1,
            "rejected": 0,
            "route_counts": {"static": 1, "browser": 1},
            "route_reasons": {"insufficient_static_content": 1},
            "block_kinds": {"forbidden": 1},
            "compliance": {"license": "CC0"},
        },
        config_snapshot={"output_dir": "./out"},
        quality_stats={"accepted": 1},
    )
    assert report.track == "multimodal"
    assert report.source_stats["route_reasons"]["insufficient_static_content"] == 1
    assert report.source_stats["block_kinds"]["forbidden"] == 1
    assert report.extras["compliance"]["license"] == "CC0"
