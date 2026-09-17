# coding=utf-8
"""轻量任务 API（optional FastAPI，ADR-0009 / ADR-0013）。

安装::

    pip install -e ".[api]"
    smart-spider-api --host 127.0.0.1 --port 8765

环境变量::

    SMART_SPIDER_API_QUEUE  队列 SQLite 路径（默认 ./runs/task_queue.sqlite3）
    SMART_SPIDER_API_STORE  对象存储根目录（默认 ./runs/objects）
    SMART_SPIDER_QUEUE_BACKEND  sqlite|redis
    SMART_SPIDER_REDIS_URL  Redis 连接串
"""

import argparse
import os
from typing import Any, Dict, Optional

from .dataset_config import DatasetCrawlConfig
from .pipeline import LocalFilesystemObjectStore, TaskRecord, get_object_store, get_task_queue


def create_app(
    queue=None,
    store=None,
):
    """构建 FastAPI app；未安装 fastapi 时抛出 ImportError。"""
    try:
        from fastapi import FastAPI, HTTPException, Query
    except ImportError as exc:
        raise ImportError(
            "smart-spider API requires fastapi; install with: pip install -e '.[api]'"
        ) from exc

    if queue is None:
        backend = os.environ.get("SMART_SPIDER_QUEUE_BACKEND", "sqlite")
        if backend == "redis":
            queue = get_task_queue("redis", url=os.environ.get("SMART_SPIDER_REDIS_URL"))
        else:
            queue = get_task_queue(
                "sqlite",
                path=os.environ.get(
                    "SMART_SPIDER_API_QUEUE",
                    os.path.join("runs", "task_queue.sqlite3"),
                ),
            )
    if store is None:
        store = get_object_store(
            "fs",
            root=os.environ.get("SMART_SPIDER_API_STORE", os.path.join("runs", "objects")),
        )

    def _to_response(record: TaskRecord) -> Dict[str, Any]:
        return {
            "task_id": record.task_id,
            "kind": record.kind,
            "status": record.status,
            "error": record.error,
            "payload": record.payload,
            "attempts": record.attempts,
            "max_attempts": record.max_attempts,
            "lease_until": record.lease_until,
            "claimed_by": record.claimed_by,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    app = FastAPI(title="smart-spider", version="2.2.0")
    app.state.queue = queue
    app.state.store = store

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics():
        """Prometheus text exposition for queue depth and worker counters."""
        from fastapi.responses import PlainTextResponse

        from .metrics import render_metrics_text

        return PlainTextResponse(
            render_metrics_text(queue=queue),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.post("/v1/jobs/dataset")
    def submit_dataset_job(payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payload.get("config", payload)
        try:
            config = DatasetCrawlConfig.from_mapping(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        max_attempts = int(payload.get("max_attempts") or 3)
        record = queue.enqueue(
            "dataset_crawl",
            {"config": config.to_dict()},
            max_attempts=max_attempts,
        )
        return _to_response(record)

    @app.post("/v1/jobs/browse")
    def submit_browse_job(payload: Dict[str, Any]) -> Dict[str, Any]:
        """提交授权浏览任务（域策略 + 浏览器剧本，非对抗绕过）。"""
        from .site_policy import SiteCrawlPolicy

        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        try:
            policy = SiteCrawlPolicy.from_mapping(payload.get("policy") or {})
            if not policy.allows_url(url):
                raise ValueError("url denied by site policy or URL safety checks")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        max_attempts = int(payload.get("max_attempts") or 3)
        record = queue.enqueue(
            "authorized_browse",
            {
                "url": url,
                "depth": int(payload.get("depth") or 0),
                "policy": policy.to_dict(),
                "enqueue_links": bool(payload.get("enqueue_links", True)),
                "headless": bool(payload.get("headless", True)),
                "max_attempts": max_attempts,
            },
            max_attempts=max_attempts,
        )
        return _to_response(record)

    @app.get("/v1/jobs")
    def list_jobs(
        status: Optional[str] = None,
        kind: Optional[str] = None,
        block_kind: Optional[str] = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ) -> Dict[str, Any]:
        try:
            records = queue.list_tasks(
                status=status,
                kind=kind,
                limit=limit,
                offset=offset,
                block_kind=block_kind,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "items": [_to_response(item) for item in records],
            "count": len(records),
            "limit": limit,
            "offset": offset,
        }

    @app.get("/v1/jobs/{task_id}")
    def get_job(task_id: str) -> Dict[str, Any]:
        record = queue.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        return _to_response(record)

    @app.post("/v1/jobs/{task_id}/retry")
    def retry_job(
        task_id: str,
        reset_attempts: bool = False,
    ) -> Dict[str, Any]:
        try:
            record = queue.retry(task_id, reset_attempts=reset_attempts)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="task not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _to_response(record)

    @app.post("/v1/worker/claim")
    def worker_claim(
        kind: Optional[str] = None,
        lease_seconds: float = 300.0,
        worker_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        try:
            record = queue.claim(
                kind=kind,
                lease_seconds=lease_seconds,
                worker_id=worker_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return None if record is None else _to_response(record)

    @app.post("/v1/worker/recover")
    def worker_recover() -> Dict[str, Any]:
        recovered = queue.recover_expired_claims()
        return {"recovered": int(recovered)}

    @app.post("/v1/jobs/{task_id}/complete")
    def complete_job(
        task_id: str, error: str = "", terminal: bool = False
    ) -> Dict[str, Any]:
        try:
            record = queue.complete(task_id, error=error, terminal=terminal)
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
