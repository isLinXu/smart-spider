# coding=utf-8
"""队列与对象存储插件协议（ADR-0009 / ADR-0013）。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

# pending → running → succeeded
#                 ↘ failed (attempt exhausted mid-flight via recover) → dead
#                 ↘ pending (retry after failure / lease expiry)
TASK_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", "dead"}
)


@dataclass
class TaskRecord:
    """本地/远程队列中的一条任务记录。"""

    task_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    attempts: int = 0
    max_attempts: int = 3
    lease_until: float = 0.0
    claimed_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    """任务队列抽象：单机 SQLite 默认，可换 Redis。"""

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        task_id: Optional[str] = None,
        max_attempts: int = 3,
    ) -> TaskRecord:
        """入队并返回任务记录。"""

    def claim(
        self,
        *,
        kind: Optional[str] = None,
        lease_seconds: float = 300.0,
        worker_id: str = "",
    ) -> Optional[TaskRecord]:
        """领取一条 pending 任务并标记为 running（带租约）。"""

    def complete(
        self, task_id: str, *, error: str = "", terminal: bool = False
    ) -> TaskRecord:
        """标记成功；失败时按 attempts/max_attempts 重试或进入 dead。

        ``terminal=True`` 表示不可恢复错误（如挑战页/策略拒绝），直接死信。
        """

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 id 查询。"""

    def list_tasks(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        block_kind: Optional[str] = None,
    ) -> Sequence[TaskRecord]:
        """列出任务（最新更新优先）。``block_kind`` 匹配 ``error`` 中的 ``block:<kind>:``。"""

    def recover_expired_claims(self, *, now: Optional[float] = None) -> int:
        """回收超时 running：可重试则回 pending，否则进 dead。"""

    def retry(self, task_id: str, *, reset_attempts: bool = False) -> TaskRecord:
        """将 failed/dead 任务重新入队为 pending。"""
