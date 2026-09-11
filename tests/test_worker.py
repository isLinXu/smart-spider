# coding=utf-8
"""Worker claim → DatasetCrawler 接线测试。"""
from __future__ import annotations

import time

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
    processed = run_worker(queue, once=True, worker_id="test-worker")
    assert processed == 1
    assert calls == [["cat"], "crawled"]
    done = queue.get(enqueued.task_id)
    assert done.status == "succeeded"
    assert done.attempts == 1
    assert queue.claim() is None


def test_worker_failure_retries_then_dead(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=2)
    config = DatasetCrawlConfig(
        keywords=["dog"],
        total_count=1,
        output_dir=str(tmp_path / "out"),
        use_clip=False,
        state_db="",
    )
    enqueued = queue.enqueue(
        "dataset_crawl", {"config": config.to_dict()}, max_attempts=2
    )

    class BoomCrawler:
        def crawl(self):
            raise RuntimeError("explode")

        @classmethod
        def from_config(cls, cfg, **kwargs):
            return cls()

    monkeypatch.setattr("smart_spider.worker.DatasetCrawler", BoomCrawler)
    assert run_worker(queue, once=True) == 1
    mid = queue.get(enqueued.task_id)
    assert mid.status == "pending"
    assert mid.attempts == 1

    assert run_worker(queue, once=True) == 1
    dead = queue.get(enqueued.task_id)
    assert dead.status == "dead"
    assert dead.attempts == 2


def test_worker_recovers_expired_claims_before_poll(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=2)
    config = DatasetCrawlConfig(
        keywords=["bird"],
        total_count=1,
        output_dir=str(tmp_path / "out"),
        use_clip=False,
        state_db="",
    )
    enqueued = queue.enqueue("dataset_crawl", {"config": config.to_dict()})
    claimed = queue.claim(lease_seconds=1, worker_id="stale")
    assert claimed is not None
    with queue._lock, queue._connect() as conn:
        conn.execute(
            "UPDATE tasks SET lease_until=? WHERE task_id=?",
            (time.time() - 10, enqueued.task_id),
        )
        conn.commit()

    class FakeCrawler:
        def crawl(self):
            return None

        @classmethod
        def from_config(cls, cfg, **kwargs):
            return cls()

    monkeypatch.setattr("smart_spider.worker.DatasetCrawler", FakeCrawler)
    processed = run_worker(queue, once=True, recover_every=1)
    assert processed == 1
    assert queue.get(enqueued.task_id).status == "succeeded"


def test_handle_task_rejects_unknown_kind():
    with pytest.raises(ValueError):
        handle_task(TaskRecord(task_id="1", kind="other", payload={}))


def test_worker_authorized_browse_enqueues_links(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    policy = {
        "allow_hosts": ["example.com"],
        "action_delay_seconds": 0,
        "scroll_passes": 1,
        "respect_robots": False,
        "max_depth": 1,
        "requests_per_second": 1000,
    }
    enqueued = queue.enqueue(
        "authorized_browse",
        {
            "url": "https://example.com/",
            "depth": 0,
            "policy": policy,
            "enqueue_links": True,
        },
    )

    class FakeController:
        current_url = "https://example.com/"

        def __init__(self, **kwargs):
            pass

        def navigate(self, url):
            self.current_url = url
            return (
                '<html><body>'
                '<a href="/a">A</a>'
                '<a href="https://evil.com/x">X</a>'
                "</body></html>"
            )

        def scroll(self):
            return True, self.navigate(self.current_url)

        def close(self):
            return None

    monkeypatch.setattr(
        "smart_spider.browser_controller.BrowserController", FakeController
    )
    processed = run_worker(queue, kind="authorized_browse", once=True)
    assert processed == 1
    assert queue.get(enqueued.task_id).status == "succeeded"
    pending = queue.list_tasks(status="pending", kind="authorized_browse")
    assert len(pending) == 1
    assert pending[0].payload["url"].endswith("/a")


def test_worker_authorized_browse_challenge_goes_dead(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"), default_max_attempts=3)
    enqueued = queue.enqueue(
        "authorized_browse",
        {
            "url": "https://example.com/",
            "policy": {
                "allow_hosts": ["example.com"],
                "action_delay_seconds": 0,
                "scroll_passes": 0,
                "respect_robots": False,
                "requests_per_second": 1000,
            },
        },
    )

    class FakeController:
        current_url = "https://example.com/"

        def __init__(self, **kwargs):
            pass

        def navigate(self, url):
            return "<html>verify you are human</html>"

        def scroll(self):
            return True, ""

        def close(self):
            return None

    monkeypatch.setattr(
        "smart_spider.browser_controller.BrowserController", FakeController
    )
    assert run_worker(queue, kind="authorized_browse", once=True) == 1
    done = queue.get(enqueued.task_id)
    assert done.status == "dead"
    assert "challenge" in done.error
