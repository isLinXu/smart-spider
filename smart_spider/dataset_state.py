# coding=utf-8
"""SQLite WAL 状态库：候选资源、租约、样本幂等和事件审计。"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from .dataset_contracts import CandidateResource, SampleRecord, utc_now


class DatasetStateStore:
    """轻量持久化任务状态库。

    一个 store 对应一个 SQLite 文件；连接启用 WAL，所有写事务通过进程内
    锁串行化。多进程场景可以通过 SQLite 的 busy_timeout 和短事务安全竞争。
    """

    def __init__(
        self,
        path: str,
        *,
        event_payload_mode: str = "compact",
        max_event_payload_bytes: int = 4096,
    ):
        self.path = path
        if event_payload_mode not in {"compact", "full"}:
            raise ValueError("event_payload_mode must be 'compact' or 'full'")
        if max_event_payload_bytes < 256:
            raise ValueError("max_event_payload_bytes must be at least 256")
        self.event_payload_mode = event_payload_mode
        self.max_event_payload_bytes = max_event_payload_bytes
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def _init_schema(self):
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'discovered',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until REAL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, url),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_candidates_claim
                    ON candidates(job_id, state, available_at, lease_until);
                CREATE TABLE IF NOT EXISTS samples (
                    sample_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    candidate_id TEXT,
                    content_hash TEXT,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'accepted',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, content_hash),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    candidate_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE TABLE IF NOT EXISTS dataset_items (
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    item_index INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_hash TEXT NOT NULL DEFAULT '',
                    relative_path TEXT NOT NULL,
                    staging_path TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    candidate_id TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, item_index),
                    UNIQUE(job_id, relative_path),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                CREATE INDEX IF NOT EXISTS idx_dataset_items_state
                    ON dataset_items(job_id, state, item_index);
                CREATE TABLE IF NOT EXISTS dataset_content_hashes (
                    job_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, content_hash),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                    FOREIGN KEY(item_id) REFERENCES dataset_items(item_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS dataset_counters (
                    job_id TEXT PRIMARY KEY,
                    next_index INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
                """
            )

    def create_job(self, job_id: str, config: Optional[dict[str, Any]] = None, status: str = "pending"):
        now = utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO jobs(job_id, config_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
                """,
                (job_id, json.dumps(config or {}, ensure_ascii=False), status, now, now),
            )

    def add_candidate(self, job_id: str, resource: CandidateResource, available_at: Optional[float] = None) -> str:
        now = utc_now()
        # 同一 URL 可以被不同任务独立处理，状态键必须包含 job_id。
        candidate_id = hashlib.sha256(
            f"{job_id}\0{resource.candidate_id}".encode("utf-8")
        ).hexdigest()
        payload = json.dumps(resource.to_dict(), ensure_ascii=False)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO candidates(
                    candidate_id, job_id, url, source, payload_json,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO NOTHING
                """,
                (
                    candidate_id,
                    job_id,
                    resource.url,
                    resource.source,
                    payload,
                    time.time() if available_at is None else available_at,
                    now,
                    now,
                ),
            )
            if cursor.rowcount:
                self._record_event_locked(
                    job_id, candidate_id, "candidate_discovered", resource.to_dict()
                )
            else:
                self._conn.execute(
                    """
                    UPDATE candidates
                    SET payload_json=?, source=?, updated_at=?
                    WHERE candidate_id=?
                    """,
                    (payload, resource.source, now, candidate_id),
                )
        return candidate_id

    def reserve_dataset_item(
        self,
        job_id: str,
        *,
        content_hash: str,
        source_hash: str,
        staging_path: str,
        batch_size: int,
        filename_token: str,
        extension: str,
        max_count: Optional[int] = None,
        candidate_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Atomically reserve a unique final hash and monotonically increasing index."""
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        extension = extension if extension.startswith(".") else f".{extension}"
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT item_id FROM dataset_content_hashes WHERE job_id=? AND content_hash=?",
                    (job_id, content_hash),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    return {"status": "duplicate", "item_id": int(existing["item_id"])}

                active = int(self._conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM dataset_items
                    WHERE job_id=? AND state IN ('pending', 'prepared', 'committed')
                    """,
                    (job_id,),
                ).fetchone()["count"])
                if max_count is not None and active >= max_count:
                    self._conn.rollback()
                    return {"status": "target_reached"}

                counter = self._conn.execute(
                    "SELECT next_index FROM dataset_counters WHERE job_id=?",
                    (job_id,),
                ).fetchone()
                if counter is None:
                    maximum = self._conn.execute(
                        "SELECT MAX(item_index) AS maximum FROM dataset_items WHERE job_id=?",
                        (job_id,),
                    ).fetchone()["maximum"]
                    item_index = int(maximum) + 1 if maximum is not None else 0
                else:
                    item_index = int(counter["next_index"])

                batch_start = (item_index // batch_size) * batch_size
                relative_path = (
                    f"batch_{batch_start:04d}/{item_index:04d}_{filename_token}{extension}"
                )
                cursor = self._conn.execute(
                    """
                    INSERT INTO dataset_items(
                        job_id, item_index, content_hash, source_hash,
                        relative_path, staging_path, candidate_id, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        job_id, item_index, content_hash, source_hash,
                        relative_path, staging_path, candidate_id, now, now,
                    ),
                )
                item_id = int(cursor.lastrowid)
                self._conn.execute(
                    """
                    INSERT INTO dataset_content_hashes(job_id, content_hash, item_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (job_id, content_hash, item_id, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO dataset_counters(job_id, next_index, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        next_index=excluded.next_index,
                        updated_at=excluded.updated_at
                    """,
                    (job_id, item_index + 1, now),
                )
                self._conn.commit()
                return {
                    "status": "reserved",
                    "item_id": item_id,
                    "index": item_index,
                    "relative_path": relative_path,
                }
            except Exception:
                self._conn.rollback()
                raise

    def prepare_dataset_item(
        self,
        item_id: int,
        metadata: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE dataset_items
                SET metadata_json=?, manifest_json=?, state='prepared', updated_at=?
                WHERE item_id=? AND state='pending'
                """,
                (
                    json.dumps(metadata, ensure_ascii=False),
                    json.dumps(manifest, ensure_ascii=False),
                    now,
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"dataset item {item_id} is not pending")

    def finalize_dataset_item(self, item_id: int) -> bool:
        """Commit a prepared item and its sample in one SQLite transaction."""
        now = utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM dataset_items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown dataset item: {item_id}")
                if row["state"] == "committed":
                    self._conn.rollback()
                    return False
                if row["state"] != "prepared":
                    raise RuntimeError(f"dataset item {item_id} is not prepared")
                manifest = json.loads(row["manifest_json"])
                sample_id = str(manifest.get("id") or f"sha256:{row['content_hash']}")
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO samples(
                        sample_id, job_id, candidate_id, content_hash,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample_id,
                        row["job_id"],
                        row["candidate_id"],
                        row["content_hash"],
                        row["manifest_json"],
                        now,
                        now,
                    ),
                )
                if not cursor.rowcount:
                    existing = self._conn.execute(
                        "SELECT job_id, content_hash FROM samples WHERE sample_id=?",
                        (sample_id,),
                    ).fetchone()
                    if (
                        existing is None
                        or existing["job_id"] != row["job_id"]
                        or existing["content_hash"] != row["content_hash"]
                    ):
                        raise RuntimeError(f"sample conflict while finalizing dataset item {item_id}")
                if row["candidate_id"]:
                    self._conn.execute(
                        """
                        UPDATE candidates
                        SET state='accepted', lease_token=NULL, lease_until=NULL, updated_at=?
                        WHERE candidate_id=?
                        """,
                        (now, row["candidate_id"]),
                    )
                self._conn.execute(
                    """
                    UPDATE dataset_items
                    SET state='committed', staging_path='', updated_at=?
                    WHERE item_id=?
                    """,
                    (now, item_id),
                )
                self._record_event_locked(
                    row["job_id"], row["candidate_id"], "sample_accepted", manifest
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def abort_dataset_item(self, item_id: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM dataset_items WHERE item_id=? AND state!='committed'",
                (item_id,),
            )
            return bool(cursor.rowcount)

    def list_dataset_items(
        self,
        job_id: str,
        states: tuple[str, ...] = ("committed",),
    ) -> list[dict[str, Any]]:
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT * FROM dataset_items
                WHERE job_id=? AND state IN ({placeholders})
                ORDER BY item_index, item_id
                """,
                (job_id, *states),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            item["manifest"] = json.loads(item.pop("manifest_json"))
            result.append(item)
        return result

    def dataset_item_count(
        self,
        job_id: str,
        states: tuple[str, ...] = ("committed",),
    ) -> int:
        if not states:
            return 0
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS count FROM dataset_items WHERE job_id=? AND state IN ({placeholders})",
                (job_id, *states),
            ).fetchone()
        return int(row["count"])

    def dataset_next_index(self, job_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT next_index FROM dataset_counters WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is not None:
                return int(row["next_index"])
            maximum = self._conn.execute(
                "SELECT MAX(item_index) AS maximum FROM dataset_items WHERE job_id=?",
                (job_id,),
            ).fetchone()["maximum"]
        return int(maximum) + 1 if maximum is not None else 0

    def import_dataset_items(
        self,
        job_id: str,
        items: list[dict[str, Any]],
    ) -> int:
        """Register an existing legacy dataset without rewriting its files."""
        if not items:
            return 0
        now = utc_now()
        imported = 0
        next_index = 0
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for position, item in enumerate(items):
                    requested_index = int(item.get("index", position))
                    item_index = max(requested_index, next_index)
                    next_index = item_index + 1
                    cursor = self._conn.execute(
                        """
                        INSERT OR IGNORE INTO dataset_items(
                            job_id, item_index, content_hash, source_hash,
                            relative_path, metadata_json, manifest_json,
                            state, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?)
                        """,
                        (
                            job_id,
                            item_index,
                            str(item["content_hash"]),
                            str(item.get("source_hash") or ""),
                            str(item["relative_path"]),
                            json.dumps(item["metadata"], ensure_ascii=False),
                            json.dumps(item["manifest"], ensure_ascii=False),
                            now,
                            now,
                        ),
                    )
                    if not cursor.rowcount:
                        continue
                    imported += 1
                    manifest_json = json.dumps(item["manifest"], ensure_ascii=False)
                    sample_id = str(
                        item["manifest"].get("id")
                        or f"sha256:{item['content_hash']}"
                    )
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO samples(
                            sample_id, job_id, candidate_id, content_hash,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?)
                        """,
                        (
                            sample_id,
                            job_id,
                            str(item["content_hash"]),
                            manifest_json,
                            now,
                            now,
                        ),
                    )
                    self._conn.execute(
                        """
                        INSERT OR IGNORE INTO dataset_content_hashes(
                            job_id, content_hash, item_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (job_id, str(item["content_hash"]), int(cursor.lastrowid), now),
                    )
                self._conn.execute(
                    """
                    INSERT INTO dataset_counters(job_id, next_index, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        next_index=MAX(dataset_counters.next_index, excluded.next_index),
                        updated_at=excluded.updated_at
                    """,
                    (job_id, next_index, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return imported

    def prune_events(self, job_id: str, keep_latest: int = 10_000) -> int:
        """Delete old audit events while retaining the newest rows for a job."""
        if keep_latest < 0:
            raise ValueError("keep_latest must be non-negative")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                DELETE FROM events
                WHERE job_id=? AND event_id NOT IN (
                    SELECT event_id FROM events WHERE job_id=?
                    ORDER BY event_id DESC LIMIT ?
                )
                """,
                (job_id, job_id, keep_latest),
            )
            return int(cursor.rowcount)

    def claim_candidates(self, job_id: str, limit: int = 10, lease_seconds: int = 300) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        now = time.time()
        token_prefix = uuid.uuid4().hex
        claimed = []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT * FROM candidates
                    WHERE job_id = ?
                      AND state IN ('discovered', 'retry_wait')
                      AND available_at <= ?
                      AND (lease_until IS NULL OR lease_until < ?)
                    ORDER BY created_at, candidate_id
                    LIMIT ?
                    """,
                    (job_id, now, now, limit),
                ).fetchall()
                for index, row in enumerate(rows):
                    token = f"{token_prefix}-{index}"
                    self._conn.execute(
                        """
                        UPDATE candidates
                        SET state='leased', attempts=attempts + 1,
                            lease_token=?, lease_until=?, updated_at=?
                        WHERE candidate_id=?
                        """,
                        (token, now + lease_seconds, utc_now(), row["candidate_id"]),
                    )
                    item = dict(row)
                    item["state"] = "leased"
                    item["attempts"] = row["attempts"] + 1
                    item["lease_token"] = token
                    item["lease_until"] = now + lease_seconds
                    item["payload"] = json.loads(row["payload_json"])
                    claimed.append(item)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return claimed

    def claim_candidate(self, candidate_id: str, lease_seconds: int = 300) -> bool:
        """按候选键领取单个资源，供编排器处理已发现资源。"""
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state, lease_until FROM candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown candidate: {candidate_id}")
            if row["state"] == "leased" and row["lease_until"] and row["lease_until"] >= now:
                return False
            if row["state"] in {"accepted", "rejected", "failed"}:
                return False
            self._conn.execute(
                """
                UPDATE candidates
                SET state='leased', attempts=attempts + 1,
                    lease_token=?, lease_until=?, updated_at=?
                WHERE candidate_id=?
                """,
                (uuid.uuid4().hex, now + lease_seconds, utc_now(), candidate_id),
            )
            return True

    def complete_candidate(self, candidate_id: str):
        self._update_candidate(candidate_id, "fetched", last_error=None)

    def reject_candidate(self, candidate_id: str, reason: str):
        """记录确定性过滤结果，不进入重试队列。"""
        self._update_candidate(candidate_id, "rejected", last_error=reason)

    def fail_candidate(
        self,
        candidate_id: str,
        error: str,
        retry: bool = True,
        max_retries: int = 3,
        retry_delay: float = 5.0,
    ) -> str:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT job_id, attempts FROM candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown candidate: {candidate_id}")
            retryable = retry and row["attempts"] < max_retries
            state = "retry_wait" if retryable else "failed"
            available_at = now + retry_delay if retryable else now
            self._conn.execute(
                """
                UPDATE candidates
                SET state=?, available_at=?, lease_token=NULL, lease_until=NULL,
                    last_error=?, updated_at=?
                WHERE candidate_id=?
                """,
                (state, available_at, error, utc_now(), candidate_id),
            )
            self._record_event_locked(
                row["job_id"], candidate_id, "candidate_failed",
                {"error": error, "state": state},
            )
            return state

    def recover_expired_leases(self, job_id: Optional[str] = None) -> int:
        now = time.time()
        with self._lock, self._conn:
            where = "state='leased' AND lease_until IS NOT NULL AND lease_until < ?"
            args: list[Any] = [now]
            if job_id:
                where += " AND job_id=?"
                args.append(job_id)
            cursor = self._conn.execute(
                f"""
                UPDATE candidates
                SET state='retry_wait', available_at=?, lease_token=NULL,
                    lease_until=NULL, updated_at=?
                WHERE {where}
                """,
                [now, utc_now(), *args],
            )
            return cursor.rowcount

    def add_sample(
        self,
        job_id: str,
        sample: SampleRecord,
        candidate_id: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> bool:
        now = utc_now()
        payload = json.dumps(sample.to_dict(), ensure_ascii=False)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO samples(
                    sample_id, job_id, candidate_id, content_hash,
                    payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sample.sample_id, job_id, candidate_id, content_hash, payload, now, now),
            )
            if cursor.rowcount:
                if candidate_id:
                    self._conn.execute(
                        "UPDATE candidates SET state='accepted', updated_at=? WHERE candidate_id=?",
                        (now, candidate_id),
                    )
                self._record_event_locked(job_id, candidate_id, "sample_accepted", sample.to_dict())
                return True
            return False

    def get_candidate(self, candidate_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    def count_candidates(self, job_id: str, state: Optional[str] = None) -> int:
        with self._lock:
            if state:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM candidates WHERE job_id=? AND state=?",
                    (job_id, state),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS count FROM candidates WHERE job_id=?", (job_id,)
                ).fetchone()
        return int(row["count"])

    def list_events(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE job_id=? ORDER BY event_id", (job_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def _update_candidate(self, candidate_id: str, state: str, last_error: Optional[str]):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT job_id FROM candidates WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown candidate: {candidate_id}")
            self._conn.execute(
                """
                UPDATE candidates
                SET state=?, lease_token=NULL, lease_until=NULL,
                    last_error=?, updated_at=?
                WHERE candidate_id=?
                """,
                (state, last_error, utc_now(), candidate_id),
            )
            self._record_event_locked(row["job_id"], candidate_id, f"candidate_{state}", {})

    def _compact_event_payload(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.event_payload_mode == "full":
            return payload
        compact: dict[str, Any] = {}
        for key in ("id", "file", "url", "source", "query", "modality", "state", "error", "reason"):
            value = payload.get(key)
            if value not in (None, "", [], {}):
                compact[key] = value
        if event_type == "sample_accepted":
            labels = payload.get("labels") or []
            compact["labels"] = [
                item.get("name") for item in labels
                if isinstance(item, dict) and item.get("name")
            ]
        return compact

    def _record_event_locked(self, job_id: str, candidate_id: Optional[str], event_type: str, payload: dict[str, Any]):
        event_payload = self._compact_event_payload(event_type, payload)
        encoded = json.dumps(event_payload, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > self.max_event_payload_bytes:
            encoded = json.dumps(
                {"truncated": True, "event_type": event_type}, ensure_ascii=False
            )
        self._conn.execute(
            """
            INSERT INTO events(job_id, candidate_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, candidate_id, event_type, encoded, utc_now()),
        )

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
