# coding=utf-8
"""API 骨架导入测试（无 fastapi 时跳过）。"""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from smart_spider.api import create_app
from smart_spider.pipeline import LocalFilesystemObjectStore, LocalSqliteTaskQueue


def test_api_health_and_dataset_job(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    store = LocalFilesystemObjectStore(str(tmp_path / "objects"))
    app = create_app(queue=queue, store=store)
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"

    bad = client.post(
        "/v1/jobs/dataset",
        json={"config": {"keywords": ["cat"], "total_count": -1}},
    )
    assert bad.status_code == 400

    ok = client.post(
        "/v1/jobs/dataset",
        json={
            "config": {
                "keywords": ["cat"],
                "total_count": 1,
                "output_dir": str(tmp_path / "out"),
                "use_clip": False,
            },
            "max_attempts": 2,
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    task_id = body["task_id"]
    assert body["max_attempts"] == 2
    assert body["attempts"] == 0
    got = client.get(f"/v1/jobs/{task_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "pending"

    listed = client.get("/v1/jobs", params={"kind": "dataset_crawl"})
    assert listed.status_code == 200
    assert listed.json()["count"] == 1
    assert listed.json()["items"][0]["task_id"] == task_id

    claimed = client.post(
        "/v1/worker/claim",
        params={"kind": "dataset_crawl", "worker_id": "api-worker", "lease_seconds": 60},
    )
    assert claimed.status_code == 200
    assert claimed.json()["task_id"] == task_id
    assert claimed.json()["claimed_by"] == "api-worker"
    assert claimed.json()["attempts"] == 1

    failed = client.post(f"/v1/jobs/{task_id}/complete", params={"error": "boom"})
    assert failed.json()["status"] == "pending"

    claimed2 = client.post("/v1/worker/claim", params={"kind": "dataset_crawl"})
    assert claimed2.json()["attempts"] == 2
    dead = client.post(f"/v1/jobs/{task_id}/complete", params={"error": "boom2"})
    assert dead.json()["status"] == "dead"

    retried = client.post(f"/v1/jobs/{task_id}/retry", params={"reset_attempts": True})
    assert retried.status_code == 200
    assert retried.json()["status"] == "pending"
    assert retried.json()["attempts"] == 0

    recovered = client.post("/v1/worker/recover")
    assert recovered.status_code == 200
    assert recovered.json()["recovered"] == 0

    claimed3 = client.post("/v1/worker/claim", params={"kind": "dataset_crawl"})
    done = client.post(f"/v1/jobs/{task_id}/complete")
    assert done.json()["status"] == "succeeded"
    assert claimed3.json()["task_id"] == task_id


def test_api_submit_browse_job(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    store = LocalFilesystemObjectStore(str(tmp_path / "objects"))
    client = TestClient(create_app(queue=queue, store=store))

    denied = client.post(
        "/v1/jobs/browse",
        json={
            "url": "https://evil.com/",
            "policy": {"allow_hosts": ["example.com"]},
        },
    )
    assert denied.status_code == 400

    ok = client.post(
        "/v1/jobs/browse",
        json={
            "url": "https://example.com/start",
            "policy": {
                "allow_hosts": ["example.com"],
                "respect_robots": False,
                "max_depth": 1,
            },
            "enqueue_links": True,
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["kind"] == "authorized_browse"
    assert body["payload"]["url"] == "https://example.com/start"


def test_api_metrics_endpoint(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    store = LocalFilesystemObjectStore(str(tmp_path / "objects"))
    client = TestClient(create_app(queue=queue, store=store))
    queue.enqueue("dataset_crawl", {"config": {"keywords": ["x"], "total_count": 1}})
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "smart_spider_queue_tasks" in response.text
    assert 'status="pending"' in response.text
