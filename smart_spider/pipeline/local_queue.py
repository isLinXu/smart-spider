# coding=utf-8
"""基于 SQLite 的本地任务队列（TaskQueue 默认实现）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from .protocols import TaskRecord


class LocalSqliteTaskQueue:
    """单进程友好的 WAL 队列；适合 API / worker 同机部署。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        parent = __import__("os").path.dirname(db_path)
        if parent:
            __import__("os").makedirs(parent, exist_ok=True)
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
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status_kind "
                "ON tasks(status, kind, created_at)"
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            error=row["error"] or "",
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        task_id: Optional[str] = None,
    ) -> TaskRecord:
        now = time.time()
        record = TaskRecord(
            task_id=task_id or uuid.uuid4().hex,
            kind=kind,
            payload=dict(payload),
            status="pending",
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, kind, payload, status, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.task_id,
                    record.kind,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.status,
                    record.error,
                    record.created_at,
                    record.updated_at,
                ),
            )
            conn.commit()
        return record

    def claim(self, *, kind: Optional[str] = None) -> Optional[TaskRecord]:
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
            conn.execute(
                "UPDATE tasks SET status='running', updated_at=? WHERE task_id=? AND status='pending'",
                (now, row["task_id"]),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            return self._row_to_record(updated)

    def complete(self, task_id: str, *, error: str = "") -> TaskRecord:
        status = "failed" if error else "succeeded"
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (status, error, now, task_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            return self._row_to_record(row)

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return None if row is None else self._row_to_record(row)
