# coding=utf-8
"""Crash-recoverable single-writer repository for image datasets.

SQLite is the source of truth. JSONL files remain compatibility views and can
be rebuilt from committed rows after an interrupted write.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from .dataset_contracts import Modality, ModalityAsset, SampleRecord
from .dataset_state import DatasetStateStore
from .image_safety import write_bytes_with_sha256


@dataclass(frozen=True)
class DatasetCommit:
    status: str
    index: Optional[int] = None
    path: str = ""
    relative_path: str = ""
    content_hash: str = ""
    sample: Optional[SampleRecord] = None

    @property
    def committed(self) -> bool:
        return self.status == "committed"


class DatasetRepository:
    """Coordinate image materialization, SQLite state, and JSONL views.

    The repository supports concurrent threads in one process. Multiple
    writers targeting the same dataset directory are intentionally unsupported;
    SQLite still protects index/hash allocation, but JSONL publication is a
    single-writer compatibility surface.
    """

    def __init__(
        self,
        output_dir: str | os.PathLike[str],
        state_store: DatasetStateStore,
        job_id: str,
        *,
        batch_size: int = 100,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        # Keep the caller's absolute spelling for compatibility metadata (on
        # macOS /var and /private/var may refer to the same directory), while
        # using a canonical root only for containment checks.
        self.root = Path(output_dir).expanduser().absolute()
        self._canonical_root = self.root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = state_store
        self.job_id = job_id
        self.batch_size = batch_size
        self.metadata_path = self.root / "metadata.jsonl"
        self.manifest_path = self.root / "manifest.jsonl"
        self.staging_dir = self.root / ".dataset_staging"
        self.staging_dir.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self.recovery_report: dict[str, Any] = {
            "legacy_imported": 0,
            "pending_aborted": 0,
            "prepared_recovered": 0,
            "missing_committed_files": [],
            "orphan_staging_removed": 0,
            "views_rebuilt": False,
        }
        self._import_legacy_dataset()
        self._recover_incomplete_commits()
        if not self._views_match():
            self.rebuild_views()
            self.recovery_report["views_rebuilt"] = True

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"expected JSON object at {path}:{line_no}")
                rows.append(value)
        return rows

    def _relative_path(self, raw: str) -> str:
        path = Path(raw)
        absolute = path.resolve() if path.is_absolute() else (self.root / path).resolve()
        try:
            return absolute.relative_to(self._canonical_root).as_posix()
        except ValueError as exc:
            raise ValueError(f"dataset path escapes root: {raw}") from exc

    def _legacy_manifest_for(
        self,
        metadata: dict[str, Any],
        relative_path: str,
        manifests: dict[str, dict[str, Any]],
        content_hash: str,
    ) -> dict[str, Any]:
        existing = manifests.get(relative_path)
        if existing is not None:
            return existing
        sample = SampleRecord(
            sample_id=f"sha256:{content_hash}",
            file=relative_path,
            labels=[],
            provenance={
                "url": str(metadata.get("url") or ""),
                "source": str(metadata.get("source") or "legacy"),
                "query": str(metadata.get("keyword") or ""),
            },
            pipeline={"status": "accepted", "legacy_import": True},
            modalities=[ModalityAsset(
                modality=Modality.IMAGE,
                role="image",
                uri=relative_path,
                mime_type="image/jpeg",
            )],
            task_type="image_classification",
        )
        return sample.to_dict()

    def _import_legacy_dataset(self) -> None:
        if self.state.dataset_item_count(
            self.job_id, ("pending", "prepared", "committed")
        ):
            return
        metadata_rows = self._read_jsonl(self.metadata_path)
        if not metadata_rows:
            return
        manifest_rows = self._read_jsonl(self.manifest_path)
        manifests: dict[str, dict[str, Any]] = {}
        for row in manifest_rows:
            raw = str(row.get("file") or "")
            if not raw:
                modalities = row.get("modalities") or []
                if modalities and isinstance(modalities[0], dict):
                    raw = str(
                        modalities[0].get("uri")
                        or modalities[0].get("path")
                        or ""
                    )
            if raw:
                manifests[self._relative_path(raw)] = row

        items: list[dict[str, Any]] = []
        for position, metadata in enumerate(metadata_rows):
            raw = str(metadata.get("file_path") or metadata.get("file") or "")
            if not raw:
                raise ValueError(f"legacy metadata record {position} has no file path")
            relative = self._relative_path(raw)
            absolute = self.root / relative
            if not absolute.is_file():
                raise FileNotFoundError(absolute)
            content_hash = str(metadata.get("sha256") or "")
            if not content_hash:
                content_hash = hashlib.sha256(absolute.read_bytes()).hexdigest()
                metadata["sha256"] = content_hash
            manifest = self._legacy_manifest_for(
                metadata, relative, manifests, content_hash
            )
            items.append({
                "index": metadata.get("index", position),
                "content_hash": content_hash,
                "relative_path": relative,
                "metadata": metadata,
                "manifest": manifest,
            })
        imported = self.state.import_dataset_items(self.job_id, items)
        self.recovery_report["legacy_imported"] = imported
        if imported:
            logger.info(f"DatasetRepository imported {imported} legacy records")

    def _recover_incomplete_commits(self) -> None:
        incomplete = self.state.list_dataset_items(
            self.job_id, ("pending", "prepared")
        )
        referenced_staging = {
            str(item.get("staging_path") or "")
            for item in incomplete
            if item.get("staging_path")
        }
        for item in incomplete:
            staging = self.root / str(item.get("staging_path") or "")
            final = self.root / item["relative_path"]
            if item["state"] == "pending":
                if staging.is_file():
                    staging.unlink()
                if self.state.abort_dataset_item(int(item["item_id"])):
                    self.recovery_report["pending_aborted"] += 1
                continue
            if not final.is_file() and staging.is_file():
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, final)
            if final.is_file():
                self.state.finalize_dataset_item(int(item["item_id"]))
                self.recovery_report["prepared_recovered"] += 1
            else:
                self.state.abort_dataset_item(int(item["item_id"]))
                self.recovery_report["pending_aborted"] += 1

        # A crash before reservation can leave only a temporary file.  Remove
        # those files on startup; staging is private to this repository and no
        # committed record can legitimately point at them.
        for staging_file in self.staging_dir.iterdir():
            if not staging_file.is_file():
                continue
            relative = staging_file.relative_to(self.root).as_posix()
            if relative in referenced_staging:
                continue
            try:
                staging_file.unlink()
                self.recovery_report["orphan_staging_removed"] += 1
            except OSError:
                logger.warning(f"could not remove orphan staging file: {staging_file}")

        missing: list[str] = []
        for item in self.state.list_dataset_items(self.job_id):
            if not (self.root / item["relative_path"]).is_file():
                missing.append(item["relative_path"])
        self.recovery_report["missing_committed_files"] = missing[:100]

    def _view_paths(self, path: Path, field: str) -> list[str]:
        values: list[str] = []
        try:
            for row in self._read_jsonl(path):
                raw = str(row.get(field) or "")
                if raw:
                    values.append(self._relative_path(raw))
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        return values

    def _views_match(self) -> bool:
        items = self.state.list_dataset_items(self.job_id)
        expected = [item["relative_path"] for item in items]
        return (
            self._view_paths(self.metadata_path, "file_path") == expected
            and self._view_paths(self.manifest_path, "file") == expected
        )

    @staticmethod
    def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
            raise

    def rebuild_views(self) -> None:
        with self._lock:
            items = self.state.list_dataset_items(self.job_id)
            self._atomic_write_jsonl(
                self.metadata_path, [item["metadata"] for item in items]
            )
            self._atomic_write_jsonl(
                self.manifest_path, [item["manifest"] for item in items]
            )

    @property
    def active_count(self) -> int:
        return self.state.dataset_item_count(self.job_id)

    @property
    def next_index(self) -> int:
        return self.state.dataset_next_index(self.job_id)

    def commit_image(
        self,
        content: bytes,
        *,
        source_content: bytes,
        url: str,
        extension: str,
        candidate_id: Optional[str],
        max_count: int,
        build_records: Callable[[int, str, str, str], tuple[SampleRecord, dict[str, Any]]],
        source_hash: Optional[str] = None,
    ) -> DatasetCommit:
        """Commit final bytes exactly once and publish compatibility views."""
        if self._closed:
            raise RuntimeError("dataset repository is closed")
        final_bytes = bytes(content)
        source_bytes = bytes(source_content)
        filename_token = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        computed_source_hash = hashlib.sha256(source_bytes).hexdigest()
        if source_hash is not None and source_hash != computed_source_hash:
            raise ValueError("source_hash does not match source_content")
        source_hash = computed_source_hash

        with self._lock:
            temporary: Optional[str] = None
            reservation: Optional[dict[str, Any]] = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.staging_dir,
                    prefix=".item-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = handle.name
                    # Single pass: persist staging bytes and compute SHA-256.
                    content_hash = write_bytes_with_sha256(
                        handle, final_bytes, fsync=True
                    )
                staging_relative = Path(temporary).relative_to(self.root).as_posix()
                reservation = self.state.reserve_dataset_item(
                    self.job_id,
                    content_hash=content_hash,
                    source_hash=source_hash,
                    staging_path=staging_relative,
                    batch_size=self.batch_size,
                    filename_token=filename_token,
                    extension=extension,
                    max_count=max_count,
                    candidate_id=candidate_id,
                )
                if reservation["status"] != "reserved":
                    os.unlink(temporary)
                    return DatasetCommit(
                        status=str(reservation["status"]),
                        content_hash=content_hash,
                    )

                index = int(reservation["index"])
                relative = str(reservation["relative_path"])
                absolute = self.root / relative
                sample, metadata = build_records(
                    index, str(absolute), relative, content_hash
                )
                self.state.prepare_dataset_item(
                    int(reservation["item_id"]), metadata, sample.to_dict()
                )
                absolute.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, absolute)
                temporary = None
                self.state.finalize_dataset_item(int(reservation["item_id"]))
                try:
                    with self.metadata_path.open("a", encoding="utf-8", buffering=1) as handle:
                        handle.write(json.dumps(metadata, ensure_ascii=False) + "\n")
                    with self.manifest_path.open("a", encoding="utf-8", buffering=1) as handle:
                        handle.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
                except OSError:
                    self.rebuild_views()
                return DatasetCommit(
                    status="committed",
                    index=index,
                    path=str(absolute),
                    relative_path=relative,
                    content_hash=content_hash,
                    sample=sample,
                )
            except Exception:
                if reservation and reservation.get("status") == "reserved":
                    # Prepared records and renamed files are recoverable. Pending
                    # rows with only a staging file can be safely aborted.
                    self._recover_incomplete_commits()
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
                raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._recover_incomplete_commits()
            if not self._views_match():
                self.rebuild_views()
            self._closed = True
