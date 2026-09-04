# coding=utf-8
"""轻量任务 API 骨架（optional FastAPI，ADR-0009）。

安装::

    pip install -e ".[api]"
    smart-spider-api --host 127.0.0.1 --port 8765

环境变量::

    SMART_SPIDER_API_QUEUE  队列 SQLite 路径（默认 ./runs/task_queue.sqlite3）
    SMART_SPIDER_API_STORE  对象存储根目录（默认 ./runs/objects）
"""

import argparse
import os
from typing import Any, Dict, Optional

from .dataset_config import DatasetCrawlConfig
from .pipeline import LocalFilesystemObjectStore, LocalSqliteTaskQueue, TaskRecord


def create_app(
    queue: Optional[LocalSqliteTaskQueue] = None,
    store: Optional[LocalFilesystemObjectStore] = None,
):
    """构建 FastAPI app；未安装 fastapi 时抛出 ImportError。"""
    try:
        from fastapi import FastAPI, HTTPException
    except ImportError as exc:
        raise ImportError(
            "smart-spider API requires fastapi; install with: pip install -e '.[api]'"
        ) from exc

    queue = queue or LocalSqliteTaskQueue(
        os.environ.get("SMART_SPIDER_API_QUEUE", os.path.join("runs", "task_queue.sqlite3"))
    )
    store = store or LocalFilesystemObjectStore(
        os.environ.get("SMART_SPIDER_API_STORE", os.path.join("runs", "objects"))
    )

    def _to_response(record: TaskRecord) -> Dict[str, Any]:
        return {
            "task_id": record.task_id,
            "kind": record.kind,
            "status": record.status,
            "error": record.error,
            "payload": record.payload,
        }

    app = FastAPI(title="smart-spider", version="2.2.0")
    app.state.queue = queue
    app.state.store = store

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/jobs/dataset")
    def submit_dataset_job(payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("config", payload)
        try:
            config = DatasetCrawlConfig.from_mapping(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record = queue.enqueue(
            "dataset_crawl",
            {"config": config.to_dict()},
        )
        return _to_response(record)

    @app.get("/v1/jobs/{task_id}")
    def get_job(task_id: str) -> Dict[str, Any]:
        record = queue.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _to_response(record)

    @app.post("/v1/worker/claim")
    def worker_claim(kind: Optional[str] = None) -> Optional[Dict[str, Any]]:
        record = queue.claim(kind=kind)
        return None if record is None else _to_response(record)

    @app.post("/v1/jobs/{task_id}/complete")
    def complete_job(task_id: str, error: str = "") -> Dict[str, Any]:
        try:
            record = queue.complete(task_id, error=error)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        return _to_response(record)

    return app


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="smart-spider lightweight job API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required; install with: pip install -e '.[api]'"
        ) from exc
    uvicorn.run(
        "smart_spider.api:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
