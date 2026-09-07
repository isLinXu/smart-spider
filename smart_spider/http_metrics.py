# coding=utf-8
"""HTTP 客户端指标（从 http_client 拆出）。"""
from __future__ import annotations

import random
import threading
from collections import Counter
from typing import Optional




class HttpMetrics:
    """Thread-safe request counters and latency summary for one client."""

    def __init__(self, max_latency_samples: int = 2048) -> None:
        if max_latency_samples <= 0:
            raise ValueError("max_latency_samples must be positive")
        self._lock = threading.Lock()
        self._requests = 0
        self._successes = 0
        self._failures = 0
        self._bytes = 0
        self._oversized_responses = 0
        self._dns_rebind_rejections = 0
        self._status = Counter()
        self._latencies_ms: list[float] = []
        self._max_latency_samples = max_latency_samples
        self._latency_seen = 0

    def observe(
        self,
        *,
        success: bool,
        elapsed_ms: float,
        status_code: Optional[int] = None,
        bytes_received: int = 0,
    ) -> None:
        with self._lock:
            self._requests += 1
            self._successes += int(success)
            self._failures += int(not success)
            self._bytes += max(0, int(bytes_received))
            self._latency_seen += 1
            latency = max(0.0, float(elapsed_ms))
            if len(self._latencies_ms) < self._max_latency_samples:
                self._latencies_ms.append(latency)
            else:
                # Reservoir sampling keeps percentile memory bounded during
                # long-running crawls without retaining every request.
                slot = random.randrange(self._latency_seen)
                if slot < self._max_latency_samples:
                    self._latencies_ms[slot] = latency
            if status_code is not None:
                self._status[str(int(status_code))] += 1

    def observe_oversized_response(self) -> None:
        with self._lock:
            self._oversized_responses += 1

    def observe_dns_rebind_rejection(self) -> None:
        with self._lock:
            self._dns_rebind_rejections += 1

    def snapshot(self) -> dict:
        with self._lock:
            latencies = sorted(self._latencies_ms)
            if latencies:
                p50 = latencies[(len(latencies) - 1) // 2]
                p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            else:
                p50 = p95 = 0.0
            return {
                "requests": self._requests,
                "successes": self._successes,
                "failures": self._failures,
                "bytes_received": self._bytes,
                "oversized_responses": self._oversized_responses,
                "dns_rebind_rejections": self._dns_rebind_rejections,
                "status_codes": dict(sorted(self._status.items())),
                "latency_ms": {"p50": round(p50, 2), "p95": round(p95, 2)},
            }

