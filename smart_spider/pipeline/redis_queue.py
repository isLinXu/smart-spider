# coding=utf-8
"""可选 Redis TaskQueue 插件（与 LocalSqliteTaskQueue 同语义）。

安装::

    pip install -e ".[redis]"

环境变量::

    SMART_SPIDER_REDIS_URL   默认 redis://127.0.0.1:6379/0
"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional, Sequence

from .protocols import TASK_STATUSES, TaskRecord


def _redis():
    try:
        import redis
    except ImportError as exc:
        raise ImportError(
            "RedisTaskQueue requires redis; install with: pip install -e '.[redis]'"
        ) from exc
    return redis


class RedisTaskQueue:
    """基于 Redis Hash + List + ZSet 的任务队列。

    Keys（prefix 默认 smart_spider）::

        {prefix}:task:{id}           Hash 任务字段
        {prefix}:pending:{kind}      List 待领取
        {prefix}:running             ZSET score=lease_until
        {prefix}:kinds               Set 已知 kind
        {prefix}:index               ZSET score=updated_at（列表）
    """

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        prefix: str = "smart_spider",
        default_max_attempts: int = 3,
        client: Any = None,
    ):
        if default_max_attempts <= 0:
            raise ValueError("default_max_attempts must be positive")
        if client is not None:
            self._client = client
        else:
            redis = _redis()
            self._client = redis.Redis.from_url(
                url or os.environ.get("SMART_SPIDER_REDIS_URL", "redis://127.0.0.1:6379/0"),
                decode_responses=True,
            )
        self.prefix = prefix
        self.default_max_attempts = int(default_max_attempts)

    def _key(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    def _save(self, record: TaskRecord) -> None:
        pipe = self._client.pipeline()
        pipe.hset(
            self._key("task", record.task_id),
            mapping={
                "task_id": record.task_id,
                "kind": record.kind,
                "payload": json.dumps(record.payload, ensure_ascii=False),
                "status": record.status,
                "error": record.error,
                "created_at": str(record.created_at),
                "updated_at": str(record.updated_at),
                "attempts": str(record.attempts),
                "max_attempts": str(record.max_attempts),
                "lease_until": str(record.lease_until),
                "claimed_by": record.claimed_by,
            },
        )
        pipe.sadd(self._key("kinds"), record.kind)
        pipe.zadd(self._key("index"), {record.task_id: record.updated_at})
        pipe.execute()

    def _load(self, task_id: str) -> Optional[TaskRecord]:
        data = self._client.hgetall(self._key("task", task_id))
        if not data:
            return None
        return TaskRecord(
            task_id=data["task_id"],
            kind=data["kind"],
            payload=json.loads(data.get("payload") or "{}"),
            status=data.get("status", "pending"),
            error=data.get("error") or "",
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            attempts=int(float(data.get("attempts") or 0)),
            max_attempts=int(float(data.get("max_attempts") or self.default_max_attempts)),
            lease_until=float(data.get("lease_until") or 0.0),
            claimed_by=str(data.get("claimed_by") or ""),
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
        self._save(record)
        self._client.lpush(self._key("pending", kind), record.task_id)
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
        kinds: list[str]
        if kind:
            kinds = [kind]
        else:
            kinds = sorted(self._client.smembers(self._key("kinds")) or [])
            if not kinds:
                kinds = ["dataset_crawl"]
        for item_kind in kinds:
            task_id = self._client.rpop(self._key("pending", item_kind))
            if not task_id:
                continue
            record = self._load(task_id)
            if record is None or record.status != "pending":
                continue
            now = time.time()
            record.status = "running"
            record.attempts += 1
            record.updated_at = now
            record.lease_until = now + float(lease_seconds)
            record.claimed_by = worker_id or ""
            record.error = ""
            self._save(record)
            self._client.zadd(self._key("running"), {task_id: record.lease_until})
            return record
        return None

    def complete(self, task_id: str, *, error: str = "") -> TaskRecord:
        record = self._load(task_id)
        if record is None:
            raise KeyError(task_id)
        now = time.time()
        record.updated_at = now
        record.lease_until = 0.0
        record.claimed_by = ""
        self._client.zrem(self._key("running"), task_id)
        if error:
            record.error = error
            if record.attempts >= record.max_attempts:
                record.status = "dead"
            else:
                record.status = "pending"
                self._client.lpush(self._key("pending", record.kind), task_id)
        else:
            record.status = "succeeded"
            record.error = ""
        self._save(record)
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._load(task_id)

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[TaskRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"unknown status: {status}")
        # Fetch a window from the updated_at index then filter.
        ids = self._client.zrevrange(self._key("index"), 0, offset + limit + 200)
        records: list[TaskRecord] = []
        for task_id in ids:
            record = self._load(task_id)
            if record is None:
                continue
            if status and record.status != status:
                continue
            if kind and record.kind != kind:
                continue
            records.append(record)
        return records[offset : offset + limit]

    def recover_expired_claims(self, *, now: Optional[float] = None) -> int:
        moment = time.time() if now is None else float(now)
        expired = self._client.zrangebyscore(self._key("running"), "-inf", moment)
        recovered = 0
        for task_id in expired:
            record = self._load(task_id)
            if record is None or record.status != "running":
                self._client.zrem(self._key("running"), task_id)
                continue
            record.updated_at = moment
            record.lease_until = 0.0
            record.claimed_by = ""
            if record.attempts >= record.max_attempts:
                record.status = "dead"
                record.error = record.error or "lease_expired_max_attempts"
            else:
                record.status = "pending"
                record.error = record.error or "lease_expired"
                self._client.lpush(self._key("pending", record.kind), task_id)
            self._client.zrem(self._key("running"), task_id)
            self._save(record)
            recovered += 1
        return recovered

    def retry(self, task_id: str, *, reset_attempts: bool = False) -> TaskRecord:
        record = self._load(task_id)
        if record is None:
            raise KeyError(task_id)
        if record.status not in {"failed", "dead", "succeeded"}:
            raise ValueError(
                f"cannot retry task in status={record.status!r}; "
                "expected failed, dead, or succeeded"
            )
        now = time.time()
        record.status = "pending"
        record.error = ""
        record.updated_at = now
        record.lease_until = 0.0
        record.claimed_by = ""
        if reset_attempts:
            record.attempts = 0
        self._client.zrem(self._key("running"), task_id)
        self._client.lpush(self._key("pending", record.kind), task_id)
        self._save(record)
        return record
