# coding=utf-8
"""配额预留与图片落盘提交（与发现/ingest/CLIP 解耦）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from loguru import logger

from .dataset_contracts import SampleRecord
from .dataset_repository import DatasetCommit


@dataclass
class QuotaHold:
    """Pending scene/source/domain reservations released unless committed."""

    scene_key: str
    source_key: str
    domain_key: str
    scene_held: bool = False
    source_held: bool = False
    domain_held: bool = False
    _scene_quotas: Any = None
    _source_quotas: Any = None
    _domain_quotas: Any = None
    _committed: bool = False

    def commit(self) -> None:
        if self._committed:
            return
        if self.scene_held:
            self._scene_quotas.commit(self.scene_key)
            self.scene_held = False
        if self.source_held:
            self._source_quotas.commit(self.source_key)
            self.source_held = False
        if self.domain_held:
            self._domain_quotas.commit(self.domain_key)
            self.domain_held = False
        self._committed = True

    def release(self) -> None:
        if self._committed:
            return
        if self.domain_held:
            self._domain_quotas.release(self.domain_key)
            self.domain_held = False
        if self.source_held:
            self._source_quotas.release(self.source_key)
            self.source_held = False
        if self.scene_held:
            self._scene_quotas.release(self.scene_key)
            self.scene_held = False


def reserve_ingest_quotas(
    *,
    scene_quotas: Any,
    source_quotas: Any,
    domain_quotas: Any,
    scene_key: str,
    source_key: str,
    domain_key: str,
    track_scene: bool,
) -> tuple[Optional[QuotaHold], str]:
    """Reserve scene/source/domain slots immediately before materialization.

    Returns ``(hold, "")`` on success. On rejection the hold is released and
    the second value is the crawler reject reason.
    """
    hold = QuotaHold(
        scene_key=scene_key,
        source_key=source_key,
        domain_key=domain_key,
        _scene_quotas=scene_quotas,
        _source_quotas=source_quotas,
        _domain_quotas=domain_quotas,
    )
    if not scene_quotas.try_reserve(scene_key):
        return None, "scene_target_reached"
    hold.scene_held = bool(track_scene)
    if not source_quotas.try_reserve(source_key):
        hold.release()
        return None, "source_quota_reached"
    hold.source_held = bool(source_key)
    if not domain_quotas.try_reserve(domain_key):
        hold.release()
        return None, "domain_quota_reached"
    hold.domain_held = bool(domain_key)
    return hold, ""


@dataclass
class MaterializeResult:
    status: str  # committed | reject | fail
    reason: str = ""
    index: int = 0
    path: str = ""
    sample: Any = None

    @property
    def ok(self) -> bool:
        return self.status == "committed"

    @property
    def reject(self) -> bool:
        return self.status == "reject"

    @property
    def fail(self) -> bool:
        return self.status == "fail"


def materialize_accepted_image(
    *,
    repository: Any,
    dir_manager: Any,
    state_store: Any,
    job_id: str,
    output_dir: str,
    manifest_writer: Any,
    metadata_writer: Any,
    output_content: bytes,
    source_content: bytes,
    url: str,
    output_ext: str,
    candidate_id: Optional[str],
    max_count: int,
    build_records: Callable[[int, str, str, str], tuple[SampleRecord, dict[str, Any]]],
    source_hash: str,
    content_hash: str,
) -> MaterializeResult:
    """Write accepted bytes via DatasetRepository or the legacy dir manager."""
    if repository is not None:
        commit: DatasetCommit = repository.commit_image(
            output_content,
            source_content=source_content,
            url=url,
            extension=output_ext,
            candidate_id=candidate_id,
            max_count=max_count,
            build_records=build_records,
            source_hash=source_hash,
        )
        if commit.status != "committed":
            return MaterializeResult(
                status="reject",
                reason=(
                    "target_reached"
                    if commit.status == "target_reached"
                    else "duplicate_content"
                ),
            )
        if commit.sample is None:
            return MaterializeResult(
                status="fail",
                reason="repository_commit_missing_sample",
            )
        dir_manager.record_repository_commit(int(commit.index or 0))
        return MaterializeResult(
            status="committed",
            index=int(commit.index or 0),
            path=commit.path,
            sample=commit.sample,
        )

    saved = dir_manager.save_content(
        url,
        output_ext,
        output_content,
        max_count=max_count,
    )
    if saved is None:
        return MaterializeResult(status="reject", reason="target_reached")
    idx, save_path = saved
    relative_path = os.path.relpath(save_path, output_dir)
    sample, metadata = build_records(idx, save_path, relative_path, content_hash)
    if candidate_id and state_store is not None:
        try:
            state_store.add_sample(
                job_id,
                sample,
                candidate_id=candidate_id,
                content_hash=content_hash,
            )
        except Exception as state_err:
            logger.warning(f"State store sample error: {state_err}")
    manifest_writer.write(sample)
    metadata_writer.write(metadata)
    return MaterializeResult(
        status="committed",
        index=idx,
        path=save_path,
        sample=sample,
    )
