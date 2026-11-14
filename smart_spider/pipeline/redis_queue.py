# coding=utf-8
"""可选 Redis TaskQueue 插件（与 LocalSqliteTaskQueue 同语义）。

安装::

    pip install -e ".[redis]"

环境变量::

    SMART_SPIDER_REDIS_URL   默认 redis://127.0.0.1:6379/0
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from typing import Any, Optional, Sequence

from .protocols import TASK_STATUSES, StaleClaimError, TaskRecord


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
        self._write(pipe, record)
        pipe.execute()

    def _write(self, pipe, record: TaskRecord) -> None:
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
                "claim_token": record.claim_token,
                "claim_generation": str(record.claim_generation),
            },
        )
        pipe.sadd(self._key("kinds"), record.kind)
        pipe.zadd(self._key("index"), {record.task_id: record.updated_at})

    def _load(self, task_id: str, client=None) -> Optional[TaskRecord]:
        reader = self._client if client is None else client
        data = reader.hgetall(self._key("task", task_id))
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
            claim_token=str(data.get("claim_token") or ""),
            claim_generation=int(data.get("claim_generation") or 0),
        )

    def _transaction(self, operation, *keys):
        """All reads precede MULTI; watched changes retry the entire decision."""
        for _ in range(32):
            try:
                with self._client.pipeline() as pipe:
                    pipe.watch(*keys)
                    return operation(pipe)
            except Exception as exc:
                # Keep the injected-client path independent of redis imports.
                # redis-py raises WatchError for EXEC conflicts and for a
                # connection lost while watching; both need a fresh decision.
                if type(exc).__name__ != "WatchError":
                    raise
        raise RuntimeError("Redis task transaction exceeded conflict retry budget")

    @staticmethod
    def _require_claim(record, task_id, claim_token, worker_id):
        if record is None:
            raise KeyError(task_id)
        if (not claim_token or record.status != "running"
                or record.claim_token != claim_token
                or record.claimed_by != worker_id
                or record.lease_until <= time.time()):
            raise StaleClaimError(f"stale task claim: {task_id}")

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
        def enqueue_atomic(pipe):
            if self._load(record.task_id, pipe) is not None:
                raise ValueError(f"task already exists: {record.task_id}")
            pipe.multi()
            self._write(pipe, record)
            pipe.lpush(self._key("pending", kind), record.task_id)
            pipe.execute()
            return record

        return self._transaction(enqueue_atomic, self._key("task", record.task_id))

    def claim(
        self,
        *,
        kind: Optional[str] = None,
        lease_seconds: float = 300.0,
        worker_id: str = "",
    ) -> Optional[TaskRecord]:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        kinds: list[str]
        if kind:
            kinds = [kind]
        else:
            kinds = sorted(self._client.smembers(self._key("kinds")) or [])
        for item_kind in kinds:
            pending_key = self._key("pending", item_kind)

            def claim_atomic(pipe):
                task_id = pipe.lindex(pending_key, -1)
                if not task_id:
                    return None
                pipe.watch(self._key("task", task_id))
                record = self._load(task_id, pipe)
                if record is None or record.status != "pending":
                    pipe.multi()
                    pipe.rpop(pending_key)
                    pipe.execute()
                    return False  # Discard a stale index entry and keep looking.
                now = time.time()
                record.status = "running"
                record.attempts += 1
                record.updated_at = now
                record.lease_until = now + float(lease_seconds)
                record.claimed_by = worker_id or ""
                record.claim_token = uuid.uuid4().hex
                record.claim_generation += 1
                record.error = ""
                pipe.multi()
                pipe.rpop(pending_key)
                self._write(pipe, record)
                pipe.zadd(self._key("running"), {task_id: record.lease_until})
                pipe.execute()
                return record

            while True:
                record = self._transaction(claim_atomic, pending_key)
                if record is False:
                    continue
                if record is not None:
                    return record
                break
        return None

    def complete(
        self, task_id: str, *, claim_token: str, worker_id: str = "",
        error: str = "", terminal: bool = False,
    ) -> TaskRecord:
        def complete_atomic(pipe):
            record = self._load(task_id, pipe)
            self._require_claim(record, task_id, claim_token, worker_id)
            record.updated_at = time.time()
            record.lease_until = 0.0
            record.claimed_by = ""
            record.claim_token = ""
            record.error = error
            if error:
                record.status = "dead" if terminal or record.attempts >= record.max_attempts else "pending"
            else:
                record.status = "succeeded"
            pipe.multi()
            pipe.zrem(self._key("running"), task_id)
            if record.status == "pending":
                pipe.lpush(self._key("pending", record.kind), task_id)
            self._write(pipe, record)
            pipe.execute()
            return record

        return self._transaction(complete_atomic, self._key("task", task_id))

    def renew(
        self, task_id: str, *, claim_token: str, worker_id: str = "",
        lease_seconds: float = 300.0,
    ) -> TaskRecord:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")

        def renew_atomic(pipe):
            record = self._load(task_id, pipe)
            self._require_claim(record, task_id, claim_token, worker_id)
            record.updated_at = time.time()
            record.lease_until = record.updated_at + lease_seconds
            pipe.multi()
            self._write(pipe, record)
            pipe.zadd(self._key("running"), {task_id: record.lease_until})
            pipe.execute()
            return record

        return self._transaction(renew_atomic, self._key("task", task_id))

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._load(task_id)

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
        # Fetch a window from the updated_at index then filter.
        ids = self._client.zrevrange(self._key("index"), 0, offset + limit + 200)
        records: list[TaskRecord] = []
        prefix = f"block:{block_kind}:" if block_kind else ""
        for task_id in ids:
            record = self._load(task_id)
            if record is None:
                continue
            if status and record.status != status:
                continue
            if kind and record.kind != kind:
                continue
            if prefix and not str(record.error or "").startswith(prefix):
                continue
            records.append(record)
        return records[offset : offset + limit]

    def recover_expired_claims(self, *, now: Optional[float] = None) -> int:
        moment = time.time() if now is None else float(now)
        expired = self._client.zrangebyscore(self._key("running"), "-inf", moment)
        recovered = 0
        for task_id in expired:
            def recover_atomic(pipe):
                record = self._load(task_id, pipe)
                if record is None or record.status != "running":
                    pipe.multi()
                    pipe.zrem(self._key("running"), task_id)
                    pipe.execute()
                    return 0
                if record.lease_until > moment:
                    return 0  # Renewed since the initial index scan.
                record.updated_at = moment
                record.lease_until = 0.0
                record.claimed_by = ""
                record.claim_token = ""
                if record.attempts >= record.max_attempts:
                    record.status = "dead"
                    record.error = record.error or "lease_expired_max_attempts"
                else:
                    record.status = "pending"
                    record.error = record.error or "lease_expired"
                pipe.multi()
                if record.status == "pending":
                    pipe.lpush(self._key("pending", record.kind), task_id)
                pipe.zrem(self._key("running"), task_id)
                self._write(pipe, record)
                pipe.execute()
                return 1

            recovered += self._transaction(recover_atomic, self._key("task", task_id))
        return recovered

    def retry(self, task_id: str, *, reset_attempts: bool = False) -> TaskRecord:
        def retry_atomic(pipe):
            record = self._load(task_id, pipe)
            if record is None:
                raise KeyError(task_id)
            if record.status not in {"failed", "dead", "succeeded"}:
                raise ValueError(
                    f"cannot retry task in status={record.status!r}; "
                    "expected failed, dead, or succeeded"
                )
            record.status = "pending"
            record.error = ""
            record.updated_at = time.time()
            record.lease_until = 0.0
            record.claimed_by = ""
            record.claim_token = ""
            if reset_attempts:
                record.attempts = 0
            pipe.multi()
            pipe.zrem(self._key("running"), task_id)
            pipe.lpush(self._key("pending", record.kind), task_id)
            self._write(pipe, record)
            pipe.execute()
            return record

        return self._transaction(retry_atomic, self._key("task", task_id))
