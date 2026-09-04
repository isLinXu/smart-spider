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
            }
        },
    )
    assert ok.status_code == 200
    task_id = ok.json()["task_id"]
    got = client.get(f"/v1/jobs/{task_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "pending"

    claimed = client.post("/v1/worker/claim", params={"kind": "dataset_crawl"})
    assert claimed.status_code == 200
    assert claimed.json()["task_id"] == task_id

    done = client.post(f"/v1/jobs/{task_id}/complete")
    assert done.json()["status"] == "succeeded"
