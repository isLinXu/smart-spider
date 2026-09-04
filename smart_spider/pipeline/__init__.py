# coding=utf-8
"""共享 pipeline 协议与本地默认实现（Facade 收敛基础）。"""

from .local_queue import LocalSqliteTaskQueue
from .local_store import LocalFilesystemObjectStore
from .protocols import ObjectStore, TaskQueue, TaskRecord

__all__ = [
    "LocalFilesystemObjectStore",
    "LocalSqliteTaskQueue",
    "ObjectStore",
    "TaskQueue",
    "TaskRecord",
]


def get_task_queue(backend: str = "sqlite", **kwargs):
    """按名称构造 TaskQueue；redis 需安装 ``.[redis]``。"""
    if backend in {"sqlite", "local", ""}:
        path = kwargs.get("path") or kwargs.get("db_path") or "runs/task_queue.sqlite3"
        return LocalSqliteTaskQueue(path)
    if backend == "redis":
        from .redis_queue import RedisTaskQueue

        return RedisTaskQueue(kwargs.get("url"), prefix=kwargs.get("prefix", "smart_spider"))
    raise ValueError(f"unknown task queue backend: {backend}")


def get_object_store(backend: str = "fs", **kwargs):
    """按名称构造 ObjectStore；s3 需安装 ``.[s3]``。"""
    if backend in {"fs", "local", "filesystem", ""}:
        root = kwargs.get("root") or "runs/objects"
        return LocalFilesystemObjectStore(root)
    if backend == "s3":
        from .s3_store import S3ObjectStore

        return S3ObjectStore(
            kwargs.get("bucket"),
            prefix=kwargs.get("prefix", ""),
            endpoint_url=kwargs.get("endpoint_url"),
        )
    raise ValueError(f"unknown object store backend: {backend}")
