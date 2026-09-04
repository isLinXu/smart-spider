# coding=utf-8
"""插件工厂与可选后端导入测试。"""
from __future__ import annotations

import pytest

from smart_spider.pipeline import get_object_store, get_task_queue


def test_get_local_backends(tmp_path):
    queue = get_task_queue("sqlite", path=str(tmp_path / "q.sqlite3"))
    store = get_object_store("fs", root=str(tmp_path / "objects"))
    record = queue.enqueue("dataset_crawl", {"x": 1})
    assert queue.get(record.task_id).status == "pending"
    key = store.put_bytes("a/b.bin", b"abc")
    assert store.get_bytes(key) == b"abc"


def test_redis_queue_class_importable_when_installed():
    pytest.importorskip("redis")
    from smart_spider.pipeline.redis_queue import RedisTaskQueue

    assert RedisTaskQueue is not None


def test_s3_store_requires_bucket_when_boto3_installed():
    pytest.importorskip("boto3")
    from smart_spider.pipeline.s3_store import S3ObjectStore

    with pytest.raises(ValueError):
        S3ObjectStore(bucket="")
