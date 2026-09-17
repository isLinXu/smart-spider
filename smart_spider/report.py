# coding=utf-8
"""统一任务报告 schema（S3）。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .dataset_contracts import CONTRACT_FORMAT_VERSION, utc_now

ALLOWED_TRACKS = {"crawl", "dataset", "multimodal", "filter", "browse"}


def summarize_browse_results(
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Short seed → block/reason routes for browse --once and UnifiedReport."""
    routes: list[dict[str, Any]] = []
    kinds: dict[str, int] = {}
    links = 0
    for item in results:
        block = item.get("block") or {}
        kind = str(block.get("kind") or "") or "unknown"
        reason = str(block.get("reason") or "")
        link_count = int(item.get("link_count") or len(item.get("links") or []) or 0)
        links += link_count
        kinds[kind] = kinds.get(kind, 0) + 1
        routes.append(
            {
                "seed": str(item.get("seed_url") or ""),
                "final_url": str(item.get("final_url") or ""),
                "block": kind,
                "reason": reason,
                "links": link_count,
                "failed_step": str(item.get("failed_step") or ""),
            }
        )
    return {
        "pages": len(routes),
        "ok": int(kinds.get("ok", 0)),
        "blocked": sum(count for kind, count in kinds.items() if kind != "ok"),
        "links": links,
        "block_kinds": kinds,
        "routes": routes,
    }


def format_browse_summary(summary: Mapping[str, Any]) -> str:
    lines = [
        "browse: {pages} pages, {ok} ok, {blocked} blocked, {links} links".format(
            pages=summary.get("pages", 0),
            ok=summary.get("ok", 0),
            blocked=summary.get("blocked", 0),
            links=summary.get("links", 0),
        )
    ]
    for route in summary.get("routes") or []:
        reason = str(route.get("reason") or "")
        kind = str(route.get("block") or "")
        extra = f" ({reason})" if reason and reason != kind else ""
        step = route.get("failed_step") or ""
        step_bit = f" step={step}" if step else ""
        lines.append(f"  {route.get('seed') or ''} -> {kind}{extra}{step_bit}")
    return "\n".join(lines)


@dataclass
class UnifiedReport:
    """采集轨道共用的报告事实源。"""

    job_id: str
    track: str  # crawl | dataset | multimodal | filter | browse
    created_at: str = field(default_factory=utc_now)
    format_version: int = CONTRACT_FORMAT_VERSION
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    stage_stats: dict[str, Any] = field(default_factory=dict)
    source_stats: dict[str, Any] = field(default_factory=dict)
    quality_stats: dict[str, Any] = field(default_factory=dict)
    resource_stats: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.job_id:
            raise ValueError("job_id is required")
        if self.track not in ALLOWED_TRACKS:
            raise ValueError(f"unknown track: {self.track!r}")
        if self.format_version != CONTRACT_FORMAT_VERSION:
            raise ValueError(
                f"unsupported report format_version={self.format_version}; "
                f"expected {CONTRACT_FORMAT_VERSION}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def write(self, path: str) -> str:
        """Serialize to JSON and return the written path."""
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "UnifiedReport":
        known = {
            "job_id",
            "track",
            "created_at",
            "format_version",
            "config_snapshot",
            "stage_stats",
            "source_stats",
            "quality_stats",
            "resource_stats",
            "recovery",
            "errors",
            "extras",
        }
        payload = {k: v for k, v in raw.items() if k in known}
        report = cls(**payload)  # type: ignore[arg-type]
        report.validate()
        return report

    @classmethod
    def from_dataset_report(
        cls,
        raw: dict[str, Any],
        *,
        job_id: str = "",
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> "UnifiedReport":
        """Map legacy ``_dataset_report.json`` shape into UnifiedReport."""
        return cls(
            job_id=str(job_id or raw.get("job_id") or "unknown"),
            track="dataset",
            config_snapshot=dict(config_snapshot or raw.get("config") or {}),
            stage_stats={
                "saved": raw.get("saved"),
                "total_target": raw.get("total_target"),
                "elapsed_seconds": raw.get("elapsed_seconds"),
            },
            source_stats=dict(raw.get("source_metrics") or raw.get("sources") or {}),
            quality_stats={
                "filtered": raw.get("filtered"),
                "scene_quality": raw.get("scene_quality"),
                "scene_quality_gate": raw.get("scene_quality_gate"),
            },
            resource_stats=dict(raw.get("resource_metrics") or raw.get("http_metrics") or {}),
            recovery={
                **dict(raw.get("recovery") or {}),
                "lease_recoveries": raw.get("lease_recoveries"),
                "repository_recovery": raw.get("repository_recovery"),
            },
            errors=list(raw.get("errors") or []),
            extras={
                **{
                    k: v
                    for k, v in raw.items()
                    if k
                    not in {
                        "job_id",
                        "config",
                        "saved",
                        "total_target",
                        "elapsed_seconds",
                        "source_metrics",
                        "sources",
                        "filtered",
                        "scene_quality",
                        "scene_quality_gate",
                        "resource_metrics",
                        "http_metrics",
                        "recovery",
                        "errors",
                        "lease_recoveries",
                        "repository_recovery",
                    }
                },
                "compliance": dict(raw.get("compliance") or {}),
                "unified_report_path": raw.get("unified_report_path"),
                "publish_checklist_path": raw.get("publish_checklist_path"),
            },
        )

    @classmethod
    def from_multimodal_report(
        cls,
        raw: dict[str, Any],
        *,
        job_id: str = "",
        config_snapshot: Optional[dict[str, Any]] = None,
        quality_stats: Optional[dict[str, Any]] = None,
    ) -> "UnifiedReport":
        """Map multimodal job report (+ optional quality dict) into UnifiedReport."""
        return cls(
            job_id=str(job_id or raw.get("job_id") or "unknown"),
            track="multimodal",
            config_snapshot=dict(config_snapshot or {}),
            stage_stats={
                "tasks": raw.get("tasks"),
                "discovered": raw.get("discovered"),
                "accepted": raw.get("accepted"),
                "rejected": raw.get("rejected"),
                "materialized_images": raw.get("materialized_images"),
            },
            source_stats={
                "route_counts": dict(raw.get("route_counts") or {}),
                "route_reasons": dict(raw.get("route_reasons") or {}),
                "block_kinds": dict(raw.get("block_kinds") or {}),
                "source_metrics": dict(raw.get("source_metrics") or {}),
            },
            quality_stats=dict(quality_stats or {}),
            recovery={
                "lease_recoveries": (raw.get("route_counts") or {}).get("lease_recoveries"),
            },
            errors=list(raw.get("errors") or []),
            extras={
                "quality_report_path": raw.get("quality_report_path"),
                "unified_report_path": raw.get("unified_report_path"),
                "publish_checklist_path": raw.get("publish_checklist_path"),
                "compliance": dict(raw.get("compliance") or {}),
                "scene_decisions": dict(raw.get("scene_decisions") or {}),
            },
        )

    @classmethod
    def from_crawl_stats(
        cls,
        stats: dict[str, Any],
        *,
        job_id: str,
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> "UnifiedReport":
        return cls(
            job_id=job_id,
            track="crawl",
            config_snapshot=dict(config_snapshot or {}),
            stage_stats={
                "saved": stats.get("saved"),
                "filtered": stats.get("filtered"),
                "failed": stats.get("failed"),
                "pages_fetched": stats.get("pages_fetched"),
                "elapsed_seconds": stats.get("elapsed_seconds"),
            },
            source_stats=dict(stats.get("source_metrics") or {}),
            resource_stats=dict(stats.get("resource_metrics") or {}),
        )

    @classmethod
    def from_browse_plan(
        cls,
        plan: dict[str, Any],
        *,
        job_id: str = "",
        config_snapshot: Optional[dict[str, Any]] = None,
    ) -> "UnifiedReport":
        """Map browse CLI plan + page results into UnifiedReport."""
        results = list(plan.get("results") or [])
        link_count = 0
        blocked_pages = 0
        skipped_by_policy = 0
        skipped_by_robots = 0
        for item in results:
            links = item.get("links") or []
            link_count += int(item.get("link_count") or len(links) or 0)
            block = item.get("block") or {}
            kind = str(block.get("kind") or "")
            if kind and kind != "ok":
                blocked_pages += 1
            skipped_by_policy += int(item.get("skipped_by_policy") or 0)
            skipped_by_robots += int(item.get("skipped_by_robots") or 0)
        snapshot = dict(config_snapshot or {})
        if not snapshot:
            snapshot = {
                "profile": plan.get("profile"),
                "urls": list(plan.get("urls") or []),
                "policy": dict(plan.get("policy") or {}),
                "steps": list(plan.get("steps") or []),
            }
        summary = dict(plan.get("summary") or summarize_browse_results(results))
        return cls(
            job_id=str(job_id or plan.get("profile") or "browse"),
            track="browse",
            config_snapshot=snapshot,
            stage_stats={
                "seeds": len(plan.get("urls") or []),
                "pages": len(results),
                "links": link_count,
                "blocked_pages": blocked_pages,
                "skipped_by_policy": skipped_by_policy,
                "skipped_by_robots": skipped_by_robots,
            },
            source_stats={"block_kinds": dict(plan.get("block_kinds") or {})},
            extras={
                "mode": plan.get("mode"),
                "summary": summary,
                "publish_checklist_path": plan.get("publish_checklist_path"),
                "unified_report_path": plan.get("unified_report_path"),
                "results": results,
            },
        )
