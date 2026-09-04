# coding=utf-8
"""Worker claim → DatasetCrawler 接线测试。"""
from __future__ import annotations

import pytest

from smart_spider.dataset_config import DatasetCrawlConfig
from smart_spider.pipeline import LocalSqliteTaskQueue
from smart_spider.pipeline.protocols import TaskRecord
from smart_spider.worker import handle_task, run_worker


def test_worker_runs_dataset_crawl_once(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    config = DatasetCrawlConfig(
        keywords=["cat"],
        total_count=1,
        output_dir=str(tmp_path / "out"),
        use_clip=False,
        state_db="",
    )
    enqueued = queue.enqueue("dataset_crawl", {"config": config.to_dict()})

    calls = []

    class FakeCrawler:
        def __init__(self, cfg):
            calls.append(list(cfg.keywords))

        def crawl(self):
            calls.append("crawled")

        @classmethod
        def from_config(cls, cfg, **kwargs):
            return cls(cfg)

    monkeypatch.setattr("smart_spider.worker.DatasetCrawler", FakeCrawler)
    processed = run_worker(queue, once=True)
    assert processed == 1
    assert calls == [["cat"], "crawled"]
    assert queue.get(enqueued.task_id).status == "succeeded"
    assert queue.claim() is None


def test_handle_task_rejects_unknown_kind():
    with pytest.raises(ValueError):
        handle_task(TaskRecord(task_id="1", kind="other", payload={}))
