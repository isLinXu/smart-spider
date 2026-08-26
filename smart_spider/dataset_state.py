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

    def __init__(self, path: str):
        self.path = path
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
            self._conn.execute(
                """
                INSERT INTO candidates(
                    candidate_id, job_id, url, source, payload_json,
                    available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    source=excluded.source,
                    updated_at=excluded.updated_at
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
            self._record_event_locked(job_id, candidate_id, "candidate_discovered", resource.to_dict())
        return candidate_id

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

    def _record_event_locked(self, job_id: str, candidate_id: Optional[str], event_type: str, payload: dict[str, Any]):
        self._conn.execute(
            """
            INSERT INTO events(job_id, candidate_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, candidate_id, event_type, json.dumps(payload, ensure_ascii=False), utc_now()),
        )

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
