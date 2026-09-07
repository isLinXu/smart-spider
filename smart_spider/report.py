# coding=utf-8
"""统一任务报告 schema（S3）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .dataset_contracts import CONTRACT_FORMAT_VERSION, utc_now


@dataclass
class UnifiedReport:
    """四条采集轨道共用的报告事实源。"""

    job_id: str
    track: str  # crawl | dataset | multimodal | filter
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
        if self.track not in {"crawl", "dataset", "multimodal", "filter"}:
            raise ValueError(f"unknown track: {self.track!r}")
        if self.format_version != CONTRACT_FORMAT_VERSION:
            raise ValueError(
                f"unsupported report format_version={self.format_version}; "
                f"expected {CONTRACT_FORMAT_VERSION}"
            )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

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
            },
            resource_stats=dict(raw.get("resource_metrics") or {}),
            recovery=dict(raw.get("recovery") or {}),
            errors=list(raw.get("errors") or []),
            extras={k: v for k, v in raw.items() if k not in {
                "job_id", "config", "saved", "total_target", "elapsed_seconds",
                "source_metrics", "sources", "filtered", "scene_quality",
                "resource_metrics", "recovery", "errors",
            }},
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
