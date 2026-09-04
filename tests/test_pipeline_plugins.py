# coding=utf-8
"""本地队列 / 对象存储插件测试。"""
from __future__ import annotations

from smart_spider.pipeline import LocalFilesystemObjectStore, LocalSqliteTaskQueue


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
    claimed = queue.claim(kind="dataset_crawl")
    assert claimed is not None
    assert claimed.task_id == record.task_id
    assert claimed.status == "running"
    done = queue.complete(record.task_id)
    assert done.status == "succeeded"
    assert queue.claim() is None
