# coding=utf-8
"""进程内 metrics 与 Prometheus 文本导出测试。"""
from __future__ import annotations

from smart_spider.metrics import MetricsRegistry, observe_task_outcome, render_metrics_text
from smart_spider.pipeline import LocalSqliteTaskQueue


def test_metrics_registry_prometheus_text():
    registry = MetricsRegistry()
    registry.help("demo_total", "demo counter")
    registry.inc("demo_total", labels={"kind": "a"})
    registry.inc("demo_total", labels={"kind": "a"})
    registry.set_gauge("demo_gauge", 3.5, labels={"status": "pending"})
    text = registry.render_prometheus()
    assert 'demo_total{kind="a"} 2.0' in text or 'demo_total{kind="a"} 2' in text
    assert 'demo_gauge{status="pending"} 3.5' in text
    assert "# TYPE demo_total counter" in text


def test_render_metrics_includes_queue_gauges(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    queue.enqueue("dataset_crawl", {"n": 1})
    observe_task_outcome("dataset_crawl", "succeeded")
    text = render_metrics_text(queue=queue)
    assert "smart_spider_queue_tasks" in text
    assert 'status="pending"' in text
    assert "smart_spider_tasks_total" in text
