# coding=utf-8
"""轻量进程内指标注册表（Prometheus text exposition，无额外依赖）。

用于 API ``/metrics`` 与 worker 任务计数；不替代完整可观测平台。
"""
from __future__ import annotations

import threading
from typing import Iterable, Mapping, Optional


def _label_key(labels: Optional[Mapping[str, str]]) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{key}="{value}"' for key, value in labels)
    return "{" + inner + "}"


class MetricsRegistry:
    """Thread-safe counter/gauge registry with Prometheus 0.0.4 text output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._gauges: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._help: dict[str, str] = {}

    def help(self, name: str, text: str) -> None:
        with self._lock:
            self._help[name] = text

    def inc(
        self,
        name: str,
        amount: float = 1.0,
        labels: Optional[Mapping[str, str]] = None,
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            bucket = self._counters.setdefault(name, {})
            bucket[key] = bucket.get(key, 0.0) + float(amount)

    def set_gauge(
        self,
        name: str,
        value: float,
        labels: Optional[Mapping[str, str]] = None,
    ) -> None:
        key = _label_key(labels)
        with self._lock:
            bucket = self._gauges.setdefault(name, {})
            bucket[key] = float(value)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                "counters": {
                    name: {
                        ",".join(f"{k}={v}" for k, v in labels) or "_": value
                        for labels, value in series.items()
                    }
                    for name, series in self._counters.items()
                },
                "gauges": {
                    name: {
                        ",".join(f"{k}={v}" for k, v in labels) or "_": value
                        for labels, value in series.items()
                    }
                    for name, series in self._gauges.items()
                },
            }

    def render_prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            names = sorted(set(self._counters) | set(self._gauges))
            for name in names:
                help_text = self._help.get(name)
                if help_text:
                    lines.append(f"# HELP {name} {help_text}")
                if name in self._counters:
                    lines.append(f"# TYPE {name} counter")
                    for labels, value in sorted(self._counters[name].items()):
                        lines.append(f"{name}{_format_labels(labels)} {value}")
                if name in self._gauges:
                    lines.append(f"# TYPE {name} gauge")
                    for labels, value in sorted(self._gauges[name].items()):
                        lines.append(f"{name}{_format_labels(labels)} {value}")
        lines.append("")
        return "\n".join(lines)


_REGISTRY = MetricsRegistry()
_REGISTRY.help(
    "smart_spider_tasks_total",
    "Task terminal outcomes observed by workers (by kind and status).",
)
_REGISTRY.help(
    "smart_spider_browse_blocks_total",
    "Authorized-browse block classifications (challenge, forbidden, ...).",
)
_REGISTRY.help(
    "smart_spider_browse_skips_total",
    "Authorized-browse skips: policy_denied, robots_skip, challenge_dead.",
)


def get_metrics() -> MetricsRegistry:
    return _REGISTRY


def observe_task_outcome(kind: str, status: str) -> None:
    get_metrics().inc(
        "smart_spider_tasks_total",
        labels={"kind": kind or "unknown", "status": status or "unknown"},
    )


def observe_browse_block(kind: str) -> None:
    get_metrics().inc(
        "smart_spider_browse_blocks_total",
        labels={"kind": kind or "unknown"},
    )


def observe_browse_skip(reason: str, amount: float = 1.0) -> None:
    get_metrics().inc(
        "smart_spider_browse_skips_total",
        amount=amount,
        labels={"reason": reason or "unknown"},
    )


def scrape_queue_gauges(queue, *, limit: int = 1000) -> None:
    """Best-effort queue depth gauges from TaskQueue.list_tasks."""
    metrics = get_metrics()
    statuses = ("pending", "running", "succeeded", "failed", "dead")
    for status in statuses:
        try:
            items = queue.list_tasks(status=status, limit=limit)
            count = len(items)
        except Exception:
            count = -1
        metrics.set_gauge(
            "smart_spider_queue_tasks",
            float(count),
            labels={"status": status},
        )


def render_metrics_text(
    *,
    extra_lines: Optional[Iterable[str]] = None,
    queue=None,
) -> str:
    if queue is not None:
        scrape_queue_gauges(queue)
    body = get_metrics().render_prometheus()
    if extra_lines:
        extras = "\n".join(line for line in extra_lines if line) + "\n"
        return body + extras
    return body
