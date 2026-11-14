# coding=utf-8
"""Crash-recoverable single-writer repository for image datasets.

SQLite is the source of truth. JSONL files remain compatibility views and can
be rebuilt from committed rows after an interrupted write.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

from .dataset_contracts import Modality, ModalityAsset, SampleRecord, utc_now
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

    BINDING_FILE = ".dataset_repository.json"
    ACTIVE_RUN_FILE = ".active_filter_run.json"
    RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    def __init__(
        self,
        output_dir: str | os.PathLike[str],
        state_store: DatasetStateStore,
        job_id: str,
        *,
        batch_size: int = 100,
        commit_guard: Optional[Callable] = None,
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
        self._commit_guard = commit_guard or nullcontext
        self._closed = False
        self._owns_state = False
        self.recovery_report: dict[str, Any] = {
            "legacy_imported": 0,
            "pending_aborted": 0,
            "prepared_recovered": 0,
            "missing_committed_files": [],
            "orphan_staging_removed": 0,
            "views_rebuilt": False,
            "operations_recovered": 0,
        }
        with self._commit_guard():
            self._bind_repository()
            self._import_legacy_dataset()
            self._recover_dataset_operations()
            self._adopt_legacy_quarantines()
            self._recover_incomplete_commits()
        # Integrity scans read every image and may be slow on large datasets;
        # they must not hold the worker queue's publication/lease lock.
        self._validate_committed_assets()
        if not self._views_match():
            with self._commit_guard():
                self.rebuild_views()
                self.recovery_report["views_rebuilt"] = True

    @classmethod
    def _existing_store(cls, output_dir: str | os.PathLike[str]) -> Optional[tuple[Path, str]]:
        """Locate the bound store without modifying it.

        Check the independently persisted DB/root binding read-only before
        trusting a sidecar, especially when its DB is outside the dataset.
        """
        root = Path(output_dir).expanduser().resolve()
        marker = root / cls.BINDING_FILE
        if marker.exists():
            binding = json.loads(marker.read_text(encoding="utf-8"))
            if binding.get("root") != str(root):
                raise ValueError("dataset repository binding belongs to another root")
            database = Path(str(binding["state_db"]))
            database = (root / database).resolve() if not database.is_absolute() else database.resolve()
            job_id = str(binding["job_id"])
            if not database.is_file():
                raise FileNotFoundError(f"bound dataset state database missing: {database}")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                try:
                    row = connection.execute(
                        "SELECT root FROM dataset_roots WHERE job_id=?", (job_id,)
                    ).fetchone()
                except sqlite3.DatabaseError as exc:
                    raise ValueError("dataset state database has no matching root binding") from exc
                if row is None or row[0] != str(root):
                    raise ValueError("dataset state database belongs to another output root")
        else:
            database = root / ".dataset_state.sqlite3"
            if not database.is_file():
                return None
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
                rows = connection.execute("SELECT DISTINCT job_id FROM dataset_items").fetchall()
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_roots'"
                ).fetchone():
                    bindings = connection.execute("SELECT job_id, root FROM dataset_roots").fetchall()
                    if any(bound_root != str(root) for _, bound_root in bindings):
                        raise ValueError("local dataset state database belongs to another output root")
                    if not rows:
                        rows = [(job,) for job, _ in bindings]
                if not rows:
                    rows = connection.execute("SELECT job_id FROM jobs").fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise ValueError("ambiguous dataset state database: multiple image jobs")
            job_id = str(rows[0][0])
        return database, job_id

    @classmethod
    def inspect_inventory(cls, output_dir: str | os.PathLike[str]) -> Optional[list[dict[str, Any]]]:
        """Read-only authoritative inventory for verification (never repair it)."""
        located = cls._existing_store(output_dir)
        if located is None:
            return None
        database, job_id = located
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dataset_operations'"
            ).fetchone() and connection.execute(
                "SELECT 1 FROM dataset_operations WHERE job_id=? AND state!='published'", (job_id,)
            ).fetchone():
                raise RuntimeError("unfinished dataset operation requires repository recovery")
            rows = connection.execute(
                "SELECT relative_path, content_hash, state FROM dataset_items WHERE job_id=?", (job_id,)
            ).fetchall()
        if any(row["state"] in {"pending", "prepared"} for row in rows):
            raise RuntimeError("unfinished image commit requires repository recovery")
        return [dict(row) for row in rows]

    @classmethod
    def open_existing(cls, output_dir: str | os.PathLike[str]) -> Optional["DatasetRepository"]:
        """Recover a bound repository, or return None for a legacy JSONL dataset."""
        located = cls._existing_store(output_dir)
        if located is None:
            return None
        database, job_id = located
        state = DatasetStateStore(str(database))
        try:
            repository = cls(output_dir, state, job_id)
        except BaseException:
            state.close()
            raise
        repository._owns_state = True
        return repository

    def _bind_repository(self) -> None:
        database = Path(self.state.path).expanduser().resolve()
        marker = self.root / self.BINDING_FILE
        if marker.exists():
            existing = json.loads(marker.read_text(encoding="utf-8"))
            old_database = Path(str(existing["state_db"]))
            if not old_database.is_absolute():
                old_database = self.root / old_database
            if (existing.get("job_id") != self.job_id
                    or existing.get("root") != str(self._canonical_root)
                    or old_database.resolve() != database):
                raise ValueError("output directory is bound to a different dataset repository")
        self.state.bind_dataset_root(self.job_id, str(self._canonical_root))
        try:
            stored_database = database.relative_to(self._canonical_root).as_posix()
        except ValueError:
            stored_database = str(database)
        self._atomic_write_json(marker, {
            "version": 1, "root": str(self._canonical_root),
            "state_db": stored_database, "job_id": self.job_id,
        })

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "posix":
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _checked_path(self, relative: str) -> Path:
        if not relative or Path(relative).is_absolute():
            raise ValueError("dataset operation requires a relative path")
        canonical = self._relative_path(relative)
        if canonical != Path(relative).as_posix():
            raise ValueError(f"non-canonical dataset operation path: {relative}")
        return self.root / canonical

    def _validate_committed_assets(self) -> None:
        missing, corrupted = [], []
        for item in self.state.list_dataset_items(self.job_id):
            path = self._checked_path(item["relative_path"])
            if not path.is_file():
                missing.append(item["relative_path"])
            elif self._sha256_file(path) != item["content_hash"]:
                corrupted.append(item["relative_path"])
        self.recovery_report["missing_committed_files"] = missing[:100]
        if missing or corrupted:
            raise RuntimeError(
                "dataset asset integrity failure; repair before resuming: "
                f"missing={missing[:5]}, hash_mismatch={corrupted[:5]}"
            )

    def _legacy_active_runs(self) -> list[dict[str, Any]]:
        path = self.root / self.ACTIVE_RUN_FILE
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        runs = payload.get("runs", [payload])
        if not isinstance(runs, list):
            raise ValueError("invalid active filter run stack")
        for run in runs:
            if not isinstance(run, dict) or not self.RUN_ID_PATTERN.fullmatch(str(run.get("run_id", ""))):
                raise ValueError("invalid active filter run id")
        return runs

    def _adopt_legacy_quarantines(self) -> None:
        """Migrate pre-journal filter runs without reviving their missing originals."""
        known = {operation["run_id"] for operation in self.state.list_dataset_operations(self.job_id)
                 if operation["kind"] == "quarantine"}
        for run in self._legacy_active_runs():
            run_id = run["run_id"]
            if run_id in known:
                continue
            run_dir = self._checked_path(f"_quarantine/{run_id}")
            records = self._read_jsonl(run_dir / "quarantine_manifest.jsonl")
            decisions = self._read_jsonl(run_dir / "decisions.jsonl")
            if not records and int(run.get("quarantined", 0)):
                raise RuntimeError("legacy quarantine manifest is missing")
            self._quarantine(run_id, decisions, legacy_records=records)
        # Until every legacy run has a journal, keep the old full stack intact.
        # A crash between two adoptions must not erase the remaining run IDs.
        if self.state.list_dataset_operations(self.job_id):
            self._publish_active_runs()

    def _active_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for operation in self.state.list_dataset_operations(self.job_id):
            if operation["state"] == "prepared":
                continue
            if operation["kind"] == "quarantine":
                runs.append({"run_id": operation["run_id"],
                             "run_dir": str(self.root / "_quarantine" / operation["run_id"]),
                             "quarantined": len(operation["plan"]["moves"])})
            elif not runs or runs.pop()["run_id"] != operation["run_id"]:
                raise RuntimeError("dataset quarantine history is not LIFO")
        return runs

    def _move_operation_asset(self, move: dict[str, Any]) -> None:
        source, target = self._checked_path(move["source"]), self._checked_path(move["destination"])
        if source.exists() and target.exists():
            raise FileExistsError(f"both operation paths exist: {source}, {target}")
        current = source if source.is_file() else target
        if not current.is_file():
            raise FileNotFoundError(f"operation asset missing: {source}, {target}")
        if self._sha256_file(current) != move["content_hash"]:
            raise RuntimeError(f"operation asset hash mismatch: {current}")
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            self._fsync_directory(source.parent)
            self._fsync_directory(target.parent)

    def _publish_operation(self, operation: dict[str, Any]) -> None:
        plan, run_id = operation["plan"], operation["run_id"]
        if not self.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid persisted quarantine run id")
        run_dir = self._checked_path(f"_quarantine/{run_id}")
        run_dir.mkdir(parents=True, exist_ok=True)
        if operation["kind"] == "quarantine":
            backups = run_dir / "backups"
            backups.mkdir(exist_ok=True)
            self._atomic_write_jsonl(backups / "metadata.jsonl", plan["metadata"])
            self._atomic_write_jsonl(backups / "manifest.jsonl", plan["manifest"])
            self._atomic_write_jsonl(run_dir / "decisions.jsonl", plan["decisions"])
            self._atomic_write_jsonl(run_dir / "quarantine_manifest.jsonl", plan["quarantine_records"])
            # The caller can replace this recovery-safe minimum with richer stats.
            if not (run_dir / "report.json").exists():
                self._atomic_write_json(run_dir / "report.json", {
                    "run_id": run_id, "applied": True, "transactional": True,
                    "dataset_records": len(plan["metadata"]),
                    "quarantined": len(plan["moves"]),
                })
        else:
            self._atomic_write_json(run_dir / "restore_report.json", plan["result"])
        self.rebuild_views()
        if not plan.get("legacy_adoption"):
            self._publish_active_runs()
        self.state.publish_dataset_operation(int(operation["operation_id"]))

    def _publish_active_runs(self) -> None:
        runs = self._active_runs()
        if runs:
            self._atomic_write_json(self.root / self.ACTIVE_RUN_FILE, {
                "version": 2, "runs": runs, **runs[-1],
            })
        else:
            (self.root / self.ACTIVE_RUN_FILE).unlink(missing_ok=True)
            self._fsync_directory(self.root)

    def _recover_dataset_operations(self) -> None:
        for operation in self.state.list_dataset_operations(self.job_id, unfinished_only=True):
            if not self.RUN_ID_PATTERN.fullmatch(operation["run_id"]):
                raise ValueError("invalid persisted quarantine run id")
            if operation["kind"] not in {"quarantine", "restore"}:
                raise ValueError("invalid persisted dataset operation kind")
            # Validate the whole plan before its first side effect, including
            # the relationship between a sample's path and its quarantine path.
            for move in operation["plan"]["moves"]:
                for field in ("relative_path", "source", "destination"):
                    self._checked_path(move[field])
                relative = move["relative_path"]
                quarantined = f"_quarantine/{operation['run_id']}/files/{relative}"
                expected = ((relative, quarantined) if operation["kind"] == "quarantine"
                            else (quarantined, relative))
                if (move["source"], move["destination"]) != expected:
                    raise ValueError("persisted operation paths do not match its dataset sample")
            if operation["state"] == "prepared":
                for move in operation["plan"]["moves"]:
                    self._move_operation_asset(move)
                self.state.finalize_dataset_operation(int(operation["operation_id"]))
            self._publish_operation(operation)
            self.recovery_report["operations_recovered"] += 1

    def quarantine(self, run_id: str, decisions: list[dict[str, Any]]) -> None:
        with self._lock, self._commit_guard():
            self._recover_dataset_operations()
            self._validate_committed_assets()
            self._quarantine(run_id, decisions)

    def _quarantine(self, run_id: str, decisions: list[dict[str, Any]], *,
                    legacy_records: Optional[list[dict[str, Any]]] = None) -> None:
        if not self.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid quarantine run id")
        items = self.state.list_dataset_items(self.job_id)
        by_path = {item["relative_path"]: item for item in items}
        rejected = [decision for decision in decisions if decision["action"] == "quarantine"]
        if legacy_records is not None:
            rejected = [record["decision"] for record in legacy_records]
        moves, quarantine_records = [], []
        for decision in rejected:
            relative = self._relative_path(str(decision["path"]))
            if relative not in by_path:
                raise ValueError(f"quarantine path is not an active repository sample: {relative}")
            item = by_path[relative]
            target = f"_quarantine/{run_id}/files/{relative}"
            self._checked_path(relative)
            self._checked_path(target)
            moves.append({"item_id": item["item_id"], "relative_path": relative,
                          "content_hash": item["content_hash"], "source": relative,
                          "destination": target})
            quarantine_records.append({"original_path": relative, "quarantine_path": f"files/{relative}",
                                       "decision": decision, "metadata": item["metadata"]})
        if len({move["item_id"] for move in moves}) != len(moves):
            raise ValueError("duplicate quarantine sample")
        plan = {"moves": moves, "metadata": [item["metadata"] for item in items],
                "manifest": [item["manifest"] for item in items], "decisions": decisions,
                "quarantine_records": quarantine_records,
                "legacy_adoption": legacy_records is not None}
        self.state.prepare_dataset_operation(self.job_id, run_id, "quarantine", plan)
        self._recover_dataset_operations()

    def restore_quarantine(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._commit_guard():
            self._recover_dataset_operations()
            operations = self.state.list_dataset_operations(self.job_id)
            completed = next((item for item in operations
                              if item["kind"] == "restore" and item["run_id"] == run_id), None)
            if completed is not None:
                return dict(completed["plan"]["result"])
            runs = self._active_runs()
            if not runs or runs[-1]["run_id"] != run_id:
                raise RuntimeError("only the latest active quarantine run can be restored")
            operation = next(item for item in operations
                             if item["kind"] == "quarantine" and item["run_id"] == run_id)
            moves = [{**move, "source": move["destination"], "destination": move["source"]}
                     for move in operation["plan"]["moves"]]
            result = {"run_id": run_id, "dataset": str(self.root), "restored": len(moves),
                      "restored_at": utc_now()}
            self.state.prepare_dataset_operation(self.job_id, run_id, "restore", {
                "moves": moves, "result": result,
            })
            self._recover_dataset_operations()
            return result

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
        if self.state.dataset_inventory_initialized(self.job_id) or self.state.dataset_item_count(
            self.job_id, ("pending", "prepared", "committed", "quarantined")
        ):
            return
        metadata_rows = self._read_jsonl(self.metadata_path)
        manifest_rows = self._read_jsonl(self.manifest_path)
        quarantined_paths: dict[str, Path] = {}
        # Preserve the full pre-filter inventory when importing a legacy dataset.
        # Its already-moved assets are reconciled by _adopt_legacy_quarantines.
        for run in self._legacy_active_runs():
            run_dir = self._checked_path(f"_quarantine/{run['run_id']}")
            for record in self._read_jsonl(run_dir / "quarantine_manifest.jsonl"):
                relative = self._relative_path(record["original_path"])
                actual = self._checked_path(
                    f"_quarantine/{run['run_id']}/{record['quarantine_path']}"
                )
                quarantined_paths[relative] = actual
                metadata_rows.append(record["metadata"])
            manifest_rows.extend(self._read_jsonl(run_dir / "backups" / "manifest.jsonl"))
        if not metadata_rows:
            self.state.initialize_empty_dataset_inventory(self.job_id)
            return
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
            absolute = quarantined_paths.get(relative, self.root / relative)
            if not absolute.is_file():
                raise FileNotFoundError(absolute)
            content_hash = self._sha256_file(absolute)
            if metadata.get("sha256") and metadata["sha256"] != content_hash:
                raise RuntimeError(f"legacy image hash mismatch: {relative}")
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
            staging_raw = str(item.get("staging_path") or "")
            staging = self._checked_path(staging_raw) if staging_raw else None
            final = self._checked_path(item["relative_path"])
            if item["state"] == "pending":
                if staging is not None and staging.is_file():
                    staging.unlink()
                if self.state.abort_dataset_item(int(item["item_id"])):
                    self.recovery_report["pending_aborted"] += 1
                continue
            if not final.is_file() and staging is not None and staging.is_file():
                if self._sha256_file(staging) != item["content_hash"]:
                    raise RuntimeError(f"prepared image hash mismatch: {staging}")
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, final)
                self._fsync_directory(final.parent)
            if final.is_file():
                if self._sha256_file(final) != item["content_hash"]:
                    raise RuntimeError(f"prepared image hash mismatch: {final}")
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
        try:
            return (self.metadata_path.is_file() and self.manifest_path.is_file()
                    and self._read_jsonl(self.metadata_path) == [item["metadata"] for item in items]
                    and self._read_jsonl(self.manifest_path) == [item["manifest"] for item in items])
        except (OSError, ValueError):
            return False

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
            DatasetRepository._fsync_directory(path.parent)
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

        with self._lock, self._commit_guard():
            self._recover_dataset_operations()
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
                self._fsync_directory(absolute.parent)
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
            try:
                with self._commit_guard():
                    self._recover_dataset_operations()
                    self._recover_incomplete_commits()
                self._validate_committed_assets()
                if not self._views_match():
                    with self._commit_guard():
                        self.rebuild_views()
            finally:
                self._closed = True
                if self._owns_state:
                    self.state.close()
