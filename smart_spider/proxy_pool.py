# coding=utf-8
"""代理池（从 http_client 拆出）。"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

import requests



@dataclass
class ProxyEntry:
    url: str                  # e.g. "http://user:pass@1.2.3.4:8080" 或 "socks5://..."
    fail_count: int = 0
    last_used: float = 0.0
    session: Optional[requests.Session] = field(default=None, repr=False)
    # Circuit breaker 状态字段
    cb_state: str = field(default="closed", init=False)      # closed / open / half_open
    cb_open_since: float = field(default=0.0, init=False)    # 进入 OPEN 状态的时刻

    def as_dict(self):
        return {"http": self.url, "https": self.url}


class ProxyPool:
    """线程安全的代理池，集成 Circuit Breaker 熔断模式。

    熔断机制
    --------
    - CLOSED  → 正常工作，请求自由通过
    - OPEN    → 连续失败超过阈值，拒绝请求进入冷却期
    - HALF_OPEN → 冷却期满，允许一个试探请求；成功则 CLOSED，失败则回到 OPEN

    相比旧版的改进
    -------------
    旧版 fail_count 达到 max_fail 后永久排除代理（仅在全部代理失败时暴力重置），
    导致偶发故障的代理永远无法恢复。新版加入 recovery_timeout 冷却期，
    代理在冷却后会自动进入 HALF_OPEN 试探恢复，无需暴力重置。
    """

    def __init__(
        self,
        proxies: list[str],
        max_fail: int = 3,
        recovery_timeout: float = 30.0,
    ):
        self._entries = [ProxyEntry(url=p) for p in proxies]
        self._max_fail = max_fail
        self._recovery_timeout = recovery_timeout
        self._idx = 0
        self._lock = threading.Lock()

    def _is_available(self, entry: ProxyEntry) -> bool:
        """Circuit breaker 可用性检查。"""
        if entry.cb_state == "closed":
            return True
        if entry.cb_state == "open":
            if time.monotonic() - entry.cb_open_since >= self._recovery_timeout:
                entry.cb_state = "half_open"
                return True
            return False
        # half_open: 允许试探请求
        return True

    def _active(self) -> list[ProxyEntry]:
        return [e for e in self._entries if self._is_available(e)]

    def get(self, strategy: str = "round_robin") -> Optional[ProxyEntry]:
        """获取一个可用代理，无代理时返回 None（直连）。"""
        with self._lock:
            active = self._active()
            if not active:
                # 所有代理处于 OPEN 状态 — 强制最早失败的代理进入 HALF_OPEN
                # 防止全部代理同时熔断导致的永久死锁
                if self._entries:
                    earliest = min(
                        self._entries,
                        key=lambda e: e.cb_open_since or float("inf"),
                    )
                    earliest.cb_state = "half_open"
                    active = [earliest]
                if not active:
                    return None
            if strategy == "random":
                entry = random.choice(active)
            else:  # round_robin
                self._idx = self._idx % len(active)
                entry = active[self._idx]
                self._idx = (self._idx + 1) % len(active)
            entry.last_used = time.monotonic()
            return entry

    def report_fail(self, entry: ProxyEntry):
        with self._lock:
            entry.fail_count += 1
            if entry.fail_count >= self._max_fail:
                entry.cb_state = "open"
                entry.cb_open_since = time.monotonic()
            logger.warning(
                f"Proxy {entry.url} fail_count={entry.fail_count} cb={entry.cb_state}"
            )

    def report_success(self, entry: ProxyEntry):
        with self._lock:
            entry.fail_count = max(0, entry.fail_count - 1)
            entry.cb_state = "closed"

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    @property
    def circuit_states(self) -> dict[str, str]:
        """返回各代理的 Circuit Breaker 状态（监控用）。"""
        with self._lock:
            return {e.url: e.cb_state for e in self._entries}

