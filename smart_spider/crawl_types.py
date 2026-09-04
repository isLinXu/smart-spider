# coding=utf-8
"""采集回调事件与统计类型（从 smart_spider 拆出）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class CrawlEvent:
    """采集生命周期事件，传递给回调函数。"""

    event_type: str
    keyword: str
    media_type: str
    engine: str = ""
    url: str = ""
    detail: dict = field(default_factory=dict)


CallbackFn = Callable[[CrawlEvent], None]


@dataclass
class CrawlStats:
    """单次采集任务的统计数据，线程安全。"""

    saved: dict = field(default_factory=dict)
    filtered: dict = field(default_factory=dict)
    failed: dict = field(default_factory=dict)
    source_metrics: dict = field(default_factory=dict)
    resource_metrics: dict = field(default_factory=dict)
    pages_fetched: int = 0
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc_saved(self, media_type: str, n: int = 1):
        with self._lock:
            self.saved[media_type] = self.saved.get(media_type, 0) + n

    def inc_filtered(self, media_type: str, n: int = 1):
        with self._lock:
            self.filtered[media_type] = self.filtered.get(media_type, 0) + n

    def inc_failed(self, media_type: str, n: int = 1):
        with self._lock:
            self.failed[media_type] = self.failed.get(media_type, 0) + n

    def inc_pages(self, n: int = 1):
        with self._lock:
            self.pages_fetched += n

    def observe_source(
        self,
        source: str,
        *,
        success: bool,
        candidates: int = 0,
        elapsed_ms: float = 0.0,
        error: str = "",
    ) -> None:
        with self._lock:
            item = self.source_metrics.setdefault(
                source,
                {
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "candidates": 0,
                    "elapsed_ms_total": 0.0,
                    "last_error": "",
                },
            )
            item["attempts"] += 1
            item["successes"] += int(success)
            item["failures"] += int(not success)
            item["candidates"] += max(0, int(candidates))
            item["elapsed_ms_total"] += max(0.0, float(elapsed_ms))
            if error:
                item["last_error"] = str(error)[:500]

    def set_resource_metrics(self, **metrics: Any) -> None:
        with self._lock:
            self.resource_metrics = {
                str(key): value
                for key, value in metrics.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }

    @property
    def elapsed_seconds(self) -> float:
        end = self.end_time or time.monotonic()
        return max(0.0, end - self.start_time)

    def summary(self) -> dict:
        with self._lock:
            source_metrics = {}
            for source, item in self.source_metrics.items():
                row = dict(item)
                row["success_rate"] = round(
                    row["successes"] / row["attempts"] if row["attempts"] else 0.0,
                    4,
                )
                row["elapsed_ms_total"] = round(row["elapsed_ms_total"], 2)
                source_metrics[source] = row
            return {
                "saved": dict(self.saved),
                "filtered": dict(self.filtered),
                "failed": dict(self.failed),
                "pages_fetched": self.pages_fetched,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
                "source_metrics": source_metrics,
                "resource_metrics": dict(self.resource_metrics),
            }
