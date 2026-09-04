# coding=utf-8
"""队列与对象存储插件协议（ADR-0009）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class TaskRecord:
    """本地/远程队列中的一条任务记录。"""

    task_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending | running | succeeded | failed
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0


@runtime_checkable
class ObjectStore(Protocol):
    """内容寻址或按 key 存放的对象存储抽象。"""

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> str:
        """写入对象，返回可稳定引用的存储键或 URI。"""

    def get_bytes(self, key: str) -> bytes:
        """读取对象字节。"""

    def exists(self, key: str) -> bool:
        """对象是否存在。"""

    def delete(self, key: str) -> None:
        """删除对象（幂等）。"""


@runtime_checkable
class TaskQueue(Protocol):
    """任务队列抽象：单机 SQLite 默认，后续可换 Redis 等。"""

    def enqueue(self, kind: str, payload: dict[str, Any], *, task_id: Optional[str] = None) -> TaskRecord:
        """入队并返回任务记录。"""

    def claim(self, *, kind: Optional[str] = None) -> Optional[TaskRecord]:
        """领取一条 pending 任务并标记为 running。"""

    def complete(self, task_id: str, *, error: str = "") -> TaskRecord:
        """标记成功或失败。"""

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 id 查询。"""
