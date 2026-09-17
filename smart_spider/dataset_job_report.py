# coding=utf-8
"""数据集任务收尾报告：stats 合并、lineage、publish checklist、unified_report。"""
from __future__ import annotations

import json
import os
from typing import Any, Mapping, Optional, Sequence

from .compliance import build_publish_checklist, write_publish_checklist
from .dataset_lineage import build_lineage, publish_dataset_artifacts
from .report import UnifiedReport

DATASET_REPORT_FILENAME = "_dataset_report.json"
UNIFIED_REPORT_FILENAME = "unified_report.json"


def merge_dataset_crawl_stats(
    summary: Mapping[str, Any],
    *,
    total_target: int,
    keywords: Sequence[str],
    search_engines: Sequence[str],
    site_parsers: Sequence[str],
    use_clip: bool,
    image_output_format: Optional[str],
    jpeg_quality: Any,
    max_file_size: Any,
    scene_targets: Mapping[str, Any],
    scene_quality_gate: Mapping[str, Any],
    source_quotas: Mapping[str, Any],
    domain_quotas: Mapping[str, Any],
    batch_size: int,
    batches: Sequence[Any],
    shutdown: bool,
    http_metrics: Mapping[str, Any],
    lease_recoveries: int,
    repository_recovery: Optional[Mapping[str, Any]] = None,
    db_stats: Optional[Mapping[str, Any]] = None,
    db_stats_error: Optional[str] = None,
) -> dict[str, Any]:
    """Copy crawler ``stats.summary()`` and attach job-level fields."""
    report = dict(summary)
    report["total_target"] = total_target
    report["keywords"] = list(keywords)
    report["search_engines"] = list(search_engines)
    report["site_parsers"] = list(site_parsers)
    report["use_clip"] = use_clip
    report["image_output_format"] = image_output_format or "original"
    report["jpeg_quality"] = jpeg_quality
    report["max_file_size"] = max_file_size
    report["scene_targets"] = dict(scene_targets)
    report["scene_quality_gate"] = dict(scene_quality_gate)
    report["source_quotas"] = dict(source_quotas)
    report["domain_quotas"] = dict(domain_quotas)
    report["batch_size"] = batch_size
    report["batches"] = list(batches)
    report["shutdown"] = shutdown
    report["http_metrics"] = dict(http_metrics)
    if repository_recovery is not None:
        report["repository_recovery"] = dict(repository_recovery)
    report["lease_recoveries"] = int(lease_recoveries)
    if db_stats is not None:
        report["db_stats"] = dict(db_stats)
    if db_stats_error is not None:
        report["db_stats_error"] = db_stats_error
    return report


def attach_dataset_lineage(
    report: dict[str, Any],
    *,
    job_id: str,
    output_dir: str,
    config_snapshot: Mapping[str, Any],
    clip_model: Optional[str],
    extra_provenance: Mapping[str, Any],
) -> None:
    """Publish lineage artifacts onto ``report``; errors become ``lineage_error``."""
    lineage = build_lineage(
        job_id=job_id,
        output_dir=output_dir,
        config_snapshot=dict(config_snapshot),
        clip_model=clip_model,
        extra_provenance=dict(extra_provenance),
    )
    try:
        published = publish_dataset_artifacts(output_dir, lineage)
        report["lineage"] = published["lineage"]
        report["manifest_checksum"] = published["checksum"]
    except Exception as exc:
        report["lineage_error"] = str(exc)


def attach_dataset_publish_bundle(
    report: dict[str, Any],
    *,
    job_id: str,
    output_dir: str,
    policy: Any,
    config_snapshot: Mapping[str, Any],
):
    """Write checklist + ``unified_report.json``. Returns checklist after it is recorded."""
    report["compliance"] = policy.to_dict()
    committed = None
    try:
        checklist = build_publish_checklist(
            job_id=job_id,
            policy=policy,
            route_counts={},
            block_kinds={},
        )
        report["publish_checklist_path"] = write_publish_checklist(output_dir, checklist)
        report["publish_checklist"] = checklist.to_dict()
        committed = checklist
        unified = UnifiedReport.from_dataset_report(
            report,
            job_id=job_id,
            config_snapshot=dict(config_snapshot),
        )
        unified.extras["compliance"] = policy.to_dict()
        unified.extras["publish_checklist_path"] = report.get("publish_checklist_path")
        report["unified_report_path"] = unified.write(
            os.path.join(output_dir, UNIFIED_REPORT_FILENAME)
        )
    except Exception as exc:
        report["compliance_report_error"] = str(exc)
    return committed


def write_dataset_report_json(report: Mapping[str, Any], output_dir: str) -> str:
    """Write legacy ``_dataset_report.json`` (no trailing newline, same as before)."""
    path = os.path.join(output_dir, DATASET_REPORT_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dict(report), handle, ensure_ascii=False, indent=2)
    return path
