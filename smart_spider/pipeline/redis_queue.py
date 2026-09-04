# coding=utf-8
"""可选 Redis TaskQueue 插件骨架。

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
from typing import Any, Optional

from .protocols import TaskRecord


def _redis():
    try:
        import redis
    except ImportError as exc:
        raise ImportError(
            "RedisTaskQueue requires redis; install with: pip install -e '.[redis]'"
        ) from exc
    return redis


class RedisTaskQueue:
    """基于 Redis List + Hash 的任务队列（最小可用骨架）。"""

    def __init__(self, url: Optional[str] = None, *, prefix: str = "smart_spider"):
        redis = _redis()
        self._client = redis.Redis.from_url(
            url or os.environ.get("SMART_SPIDER_REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
        self.prefix = prefix

    def _key(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

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
        pipe = self._client.pipeline()
        pipe.hset(
            self._key("task", record.task_id),
            mapping={
                "task_id": record.task_id,
                "kind": record.kind,
                "payload": json.dumps(record.payload, ensure_ascii=False),
                "status": record.status,
                "error": "",
                "created_at": str(record.created_at),
                "updated_at": str(record.updated_at),
            },
        )
        pipe.lpush(self._key("pending", kind), record.task_id)
        pipe.execute()
        return record

    def claim(self, *, kind: Optional[str] = None) -> Optional[TaskRecord]:
        kinds = [kind] if kind else ["dataset_crawl"]
        for item_kind in kinds:
            task_id = self._client.rpop(self._key("pending", item_kind))
            if not task_id:
                continue
            key = self._key("task", task_id)
            now = time.time()
            self._client.hset(key, mapping={"status": "running", "updated_at": str(now)})
            return self.get(task_id)
        return None

    def complete(self, task_id: str, *, error: str = "") -> TaskRecord:
        status = "failed" if error else "succeeded"
        key = self._key("task", task_id)
        if not self._client.exists(key):
            raise KeyError(task_id)
        self._client.hset(
            key,
            mapping={"status": status, "error": error, "updated_at": str(time.time())},
        )
        record = self.get(task_id)
        assert record is not None
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
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
        )
