# coding=utf-8
"""UnifiedReport schema tests（S3）。"""
from __future__ import annotations

import json

from smart_spider.report import UnifiedReport, format_browse_summary, summarize_browse_results


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


def test_from_browse_plan_and_write(tmp_path):
    report = UnifiedReport.from_browse_plan(
        {
            "profile": "demo",
            "mode": "once",
            "urls": ["https://example.com/"],
            "policy": {"allow_hosts": ["example.com"]},
            "steps": [],
            "block_kinds": {"ok": 1},
            "results": [
                {
                    "seed_url": "https://example.com/",
                    "link_count": 2,
                    "links": [{}, {}],
                    "block": {"kind": "ok"},
                    "skipped_by_policy": 0,
                    "skipped_by_robots": 1,
                }
            ],
        }
    )
    assert report.track == "browse"
    assert report.stage_stats["pages"] == 1
    assert report.stage_stats["links"] == 2
    assert report.stage_stats["skipped_by_robots"] == 1
    path = report.write(str(tmp_path / "unified_report.json"))
    restored = UnifiedReport.from_mapping(
        json.loads(tmp_path.joinpath("unified_report.json").read_text(encoding="utf-8"))
    )
    assert restored.track == "browse"
    assert path.endswith("unified_report.json")
    assert restored.extras["summary"]["ok"] == 1
    assert restored.extras["summary"]["routes"][0]["seed"] == "https://example.com/"


def test_summarize_browse_results_seed_to_block():
    summary = summarize_browse_results(
        [
            {
                "seed_url": "https://example.com/",
                "final_url": "https://example.com/home",
                "link_count": 3,
                "block": {"kind": "ok", "reason": "ok"},
            },
            {
                "seed_url": "https://example.com/login",
                "link_count": 0,
                "block": {"kind": "challenge", "reason": "challenge_page"},
                "failed_step": "wait_for:css=#ok",
            },
        ]
    )
    assert summary["pages"] == 2
    assert summary["ok"] == 1
    assert summary["blocked"] == 1
    assert summary["links"] == 3
    text = format_browse_summary(summary)
    assert "1 ok, 1 blocked" in text
    assert "https://example.com/login -> challenge (challenge_page)" in text
    assert "step=wait_for:css=#ok" in text
