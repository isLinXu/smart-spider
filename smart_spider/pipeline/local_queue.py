# coding=utf-8
"""基于 SQLite 的本地任务队列（TaskQueue 默认实现）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional, Sequence

from .protocols import TASK_STATUSES, TaskRecord


class LocalSqliteTaskQueue:
    """WAL 任务队列：claim 租约、失败重试、dead-letter。"""

    def __init__(self, db_path: str, *, default_max_attempts: int = 3):
        if default_max_attempts <= 0:
            raise ValueError("default_max_attempts must be positive")
        self.db_path = db_path
        self.default_max_attempts = int(default_max_attempts)
        self._lock = threading.Lock()
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    lease_until REAL NOT NULL DEFAULT 0,
                    claimed_by TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._migrate(conn)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_kind "
                "ON tasks(status, kind, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_running_lease "
                "ON tasks(status, lease_until)"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        alterations = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "max_attempts": "INTEGER NOT NULL DEFAULT 3",
            "lease_until": "REAL NOT NULL DEFAULT 0",
            "claimed_by": "TEXT NOT NULL DEFAULT ''",
        }
        for name, declaration in alterations.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        keys = row.keys()
        return TaskRecord(
            task_id=row["task_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            error=row["error"] or "",
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            attempts=int(row["attempts"]) if "attempts" in keys else 0,
            max_attempts=int(row["max_attempts"]) if "max_attempts" in keys else 3,
            lease_until=float(row["lease_until"]) if "lease_until" in keys else 0.0,
            claimed_by=str(row["claimed_by"] or "") if "claimed_by" in keys else "",
        )

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        task_id: Optional[str] = None,
        max_attempts: int = 0,
    ) -> TaskRecord:
        attempts_limit = int(max_attempts or self.default_max_attempts)
        if attempts_limit <= 0:
            raise ValueError("max_attempts must be positive")
        now = time.time()
        record = TaskRecord(
            task_id=task_id or uuid.uuid4().hex,
            kind=kind,
            payload=dict(payload),
            status="pending",
            created_at=now,
            updated_at=now,
            max_attempts=attempts_limit,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, kind, payload, status, error, created_at, updated_at,
                    attempts, max_attempts, lease_until, claimed_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.kind,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.status,
                    record.error,
                    record.created_at,
                    record.updated_at,
                    record.attempts,
                    record.max_attempts,
                    record.lease_until,
                    record.claimed_by,
                ),
            )
            conn.commit()
        return record

    def claim(
        self,
        *,
        kind: Optional[str] = None,
        lease_seconds: float = 300.0,
        worker_id: str = "",
    ) -> Optional[TaskRecord]:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._lock, self._connect() as conn:
            if kind:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' AND kind=? "
                    "ORDER BY created_at ASC LIMIT 1",
                    (kind,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE status='pending' "
                    "ORDER BY created_at ASC LIMIT 1"
                ).fetchone()
            if row is None:
                return None
            now = time.time()
            lease_until = now + float(lease_seconds)
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status='running',
                    updated_at=?,
                    attempts=attempts + 1,
                    lease_until=?,
                    claimed_by=?,
                    error=''
                WHERE task_id=? AND status='pending'
                """,
                (now, lease_until, worker_id or "", row["task_id"]),
            )
            if cursor.rowcount != 1:
                conn.commit()
                return None
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            return self._row_to_record(updated)

    def complete(
        self, task_id: str, *, error: str = "", terminal: bool = False
    ) -> TaskRecord:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            record = self._row_to_record(row)
            if error:
                if terminal or record.attempts >= record.max_attempts:
                    status = "dead"
                else:
                    status = "pending"
                conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, error=?, updated_at=?, lease_until=0, claimed_by=''
                    WHERE task_id=?
                    """,
                    (status, error, now, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status='succeeded', error='', updated_at=?,
                        lease_until=0, claimed_by=''
                    WHERE task_id=?
                    """,
                    (now, task_id),
                )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._row_to_record(updated)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return None if row is None else self._row_to_record(row)

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        block_kind: Optional[str] = None,
    ) -> Sequence[TaskRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"unknown status: {status}")
        clauses: list[str] = []
        args: list[Any] = []
        if status:
            clauses.append("status=?")
            args.append(status)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if block_kind:
            clauses.append("error LIKE ?")
            args.append(f"block:{block_kind}:%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM tasks {where} "
            "ORDER BY updated_at DESC, created_at DESC "
            "LIMIT ? OFFSET ?"
        )
        args.extend([int(limit), int(offset)])
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            return [self._row_to_record(row) for row in rows]

    def recover_expired_claims(self, *, now: Optional[float] = None) -> int:
        moment = time.time() if now is None else float(now)
        recovered = 0
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status='running' AND lease_until > 0 AND lease_until < ?
                """,
                (moment,),
            ).fetchall()
            for row in rows:
                record = self._row_to_record(row)
                if record.attempts >= record.max_attempts:
                    status = "dead"
                    error = record.error or "lease_expired_max_attempts"
                else:
                    status = "pending"
                    error = record.error or "lease_expired"
                conn.execute(
                    """
                    UPDATE tasks
                    SET status=?, error=?, updated_at=?, lease_until=0, claimed_by=''
                    WHERE task_id=? AND status='running'
                    """,
                    (status, error, moment, record.task_id),
                )
                recovered += 1
            conn.commit()
        return recovered

    def retry(self, task_id: str, *, reset_attempts: bool = False) -> TaskRecord:
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            record = self._row_to_record(row)
            if record.status not in {"failed", "dead", "succeeded"}:
                raise ValueError(
                    f"cannot retry task in status={record.status!r}; "
                    "expected failed, dead, or succeeded"
                )
            attempts = 0 if reset_attempts else record.attempts
            conn.execute(
                """
                UPDATE tasks
                SET status='pending', error='', updated_at=?, attempts=?,
                    lease_until=0, claimed_by=''
                WHERE task_id=?
                """,
                (now, attempts, task_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return self._row_to_record(updated)
