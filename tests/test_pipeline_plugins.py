# coding=utf-8
"""本地队列 / 对象存储 / Redis 假客户端插件测试。"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from smart_spider.pipeline import LocalFilesystemObjectStore, LocalSqliteTaskQueue, get_task_queue
from smart_spider.pipeline.redis_queue import RedisTaskQueue


class _FakePipeline:
    def __init__(self, client: "FakeRedis"):
        self._client = client
        self._ops: list[tuple] = []

    def hset(self, key, mapping=None, **kwargs):
        self._ops.append(("hset", key, mapping or kwargs))
        return self

    def sadd(self, key, *values):
        self._ops.append(("sadd", key, values))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def execute(self):
        for op in self._ops:
            name = op[0]
            if name == "hset":
                self._client.hset(op[1], mapping=op[2])
            elif name == "sadd":
                self._client.sadd(op[1], *op[2])
            elif name == "zadd":
                self._client.zadd(op[1], op[2])
        self._ops.clear()
        return []


class FakeRedis:
    """足够覆盖 RedisTaskQueue 语义的内存客户端。"""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = defaultdict(dict)
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.sets: dict[str, set[str]] = defaultdict(set)
        self.zsets: dict[str, dict[str, float]] = defaultdict(dict)

    def pipeline(self):
        return _FakePipeline(self)

    def hset(self, key, mapping=None, **kwargs):
        payload = mapping or kwargs
        self.hashes[key].update({str(k): str(v) for k, v in payload.items()})

    def hgetall(self, key):
        return dict(self.hashes.get(key) or {})

    def sadd(self, key, *values):
        self.sets[key].update(str(v) for v in values)

    def smembers(self, key):
        return set(self.sets.get(key) or set())

    def lpush(self, key, *values):
        for value in values:
            self.lists[key].insert(0, str(value))

    def rpop(self, key):
        if not self.lists.get(key):
            return None
        return self.lists[key].pop()

    def zadd(self, key, mapping):
        for member, score in mapping.items():
            self.zsets[key][str(member)] = float(score)

    def zrem(self, key, *members):
        removed = 0
        for member in members:
            if str(member) in self.zsets.get(key, {}):
                del self.zsets[key][str(member)]
                removed += 1
        return removed

    def zrevrange(self, key, start, end):
        items = sorted(
            self.zsets.get(key, {}).items(), key=lambda item: item[1], reverse=True
        )
        stop = None if end < 0 else end + 1
        return [member for member, _ in items[start:stop]]

    def zrangebyscore(self, key, min_score, max_score):
        low = float("-inf") if min_score == "-inf" else float(min_score)
        high = float("inf") if max_score == "+inf" else float(max_score)
        return [
            member
            for member, score in self.zsets.get(key, {}).items()
            if low <= score <= high
        ]


def test_local_object_store_roundtrip(tmp_path):
    store = LocalFilesystemObjectStore(str(tmp_path / "objects"))
    key = store.put_bytes("", b"hello-bytes")
    assert store.exists(key)
    assert store.get_bytes(key) == b"hello-bytes"
    store.delete(key)
    assert not store.exists(key)


def test_local_task_queue_lifecycle(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    record = queue.enqueue("dataset_crawl", {"n": 1})
    assert record.status == "pending"
    claimed = queue.claim(kind="dataset_crawl", worker_id="w1", lease_seconds=60)
    assert claimed is not None
    assert claimed.task_id == record.task_id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.claimed_by == "w1"
    assert claimed.lease_until > time.time()
    done = queue.complete(record.task_id)
    assert done.status == "succeeded"
    assert queue.claim() is None


def test_local_task_queue_retry_then_dead(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=2)
    record = queue.enqueue("dataset_crawl", {"n": 1}, max_attempts=2)
    first = queue.claim(kind="dataset_crawl")
    assert first is not None
    retried = queue.complete(first.task_id, error="boom")
    assert retried.status == "pending"
    assert retried.attempts == 1
    assert retried.error == "boom"

    second = queue.claim(kind="dataset_crawl")
    assert second is not None
    assert second.attempts == 2
    dead = queue.complete(second.task_id, error="boom-again")
    assert dead.status == "dead"
    assert queue.claim() is None

    revived = queue.retry(record.task_id, reset_attempts=True)
    assert revived.status == "pending"
    assert revived.attempts == 0
    assert revived.error == ""


def test_local_task_queue_recover_expired_claims(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=2)
    queue.enqueue("dataset_crawl", {"n": 1}, max_attempts=2)
    claimed = queue.claim(lease_seconds=1, worker_id="w")
    assert claimed is not None
    # Force lease into the past.
    with queue._lock, queue._connect() as conn:
        conn.execute(
            "UPDATE tasks SET lease_until=? WHERE task_id=?",
            (time.time() - 10, claimed.task_id),
        )
        conn.commit()
    recovered = queue.recover_expired_claims()
    assert recovered == 1
    record = queue.get(claimed.task_id)
    assert record.status == "pending"
    assert record.error == "lease_expired"


def test_local_task_queue_list_tasks(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    a = queue.enqueue("dataset_crawl", {"n": 1})
    b = queue.enqueue("other", {"n": 2})
    queue.complete(queue.claim(kind="dataset_crawl").task_id)
    items = queue.list_tasks(kind="dataset_crawl")
    assert [item.task_id for item in items] == [a.task_id]
    pending = queue.list_tasks(status="pending")
    assert [item.task_id for item in pending] == [b.task_id]


def _assert_queue_semantics(queue: Any):
    record = queue.enqueue("dataset_crawl", {"n": 1}, max_attempts=2)
    claimed = queue.claim(kind="dataset_crawl", worker_id="rw", lease_seconds=30)
    assert claimed is not None
    assert claimed.attempts == 1
    soft = queue.complete(claimed.task_id, error="temp")
    assert soft.status == "pending"
    again = queue.claim(kind="dataset_crawl")
    assert again.attempts == 2
    dead = queue.complete(again.task_id, error="final")
    assert dead.status == "dead"
    assert queue.list_tasks(status="dead")[0].task_id == record.task_id
    revived = queue.retry(record.task_id, reset_attempts=True)
    assert revived.status == "pending"
    assert revived.attempts == 0


def test_redis_task_queue_semantics_with_fake_client():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client, prefix="test")
    _assert_queue_semantics(queue)

    # lease recovery path
    second = queue.enqueue("dataset_crawl", {"n": 2}, max_attempts=2)
    claimed = queue.claim(kind="dataset_crawl", lease_seconds=5)
    assert claimed is not None
    queue._client.zadd(queue._key("running"), {claimed.task_id: time.time() - 1})
    claimed_loaded = queue.get(claimed.task_id)
    claimed_loaded.lease_until = time.time() - 1
    queue._save(claimed_loaded)
    assert queue.recover_expired_claims() == 1
    assert queue.get(second.task_id).status == "pending"


def test_get_task_queue_redis_accepts_client():
    client = FakeRedis()
    queue = get_task_queue("redis", client=client, prefix="factory")
    assert isinstance(queue, RedisTaskQueue)
    record = queue.enqueue("dataset_crawl", {"x": 1})
    assert queue.get(record.task_id).status == "pending"


def test_redis_claim_without_kind_sees_browse():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client, prefix="mix")
    browse = queue.enqueue("authorized_browse", {"url": "https://example.com"})
    claimed = queue.claim()
    assert claimed is not None
    assert claimed.task_id == browse.task_id
    assert claimed.kind == "authorized_browse"
