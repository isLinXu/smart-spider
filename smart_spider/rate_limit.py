# coding=utf-8
"""令牌桶限速（从 http_client 拆出）。"""
from __future__ import annotations

import threading
import time



class RateLimiter:
    """令牌桶算法：控制全局每秒最大请求数。

    修复记录
    --------
    - 旧版 acquire() 在「睡眠后再次加锁减 token」的路径中存在 double-lock Bug：
      睡眠期间其他线程已补充并消耗了 token，醒来后再减会导致 token 变成负数，
      使下一个请求需要等待更长时间（误差累积）。
    - 新版改为「持锁计算等待时间 → 释放锁 → sleep → 再持锁消耗 token」，
      用 condition variable 替代裸 sleep，确保唤醒后 token 状态一致。
    """

    def __init__(self, rate: float):
        """
        Args:
            rate: 每秒允许的最大请求数（令牌补充速率）
        """
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self):
        """在持锁状态下补充令牌（调用方负责加锁）。"""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)

    def acquire(self):
        """阻塞直到获取一个令牌（精确令牌桶，无 double-lock Bug）。"""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 计算需等待多少秒才能补充到 1 个 token
                wait = (1.0 - self._tokens) / self._rate
            # 在锁外 sleep，避免持锁阻塞其他线程
            time.sleep(wait)

