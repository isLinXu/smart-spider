# coding=utf-8
"""任务队列 worker：claim → DatasetCrawler.from_config → complete。

用法::

    # 与 API 共用同一队列
    smart-spider-api &
    smart-spider-worker --once   # 处理一条后退出
    smart-spider-worker          # 常驻轮询
"""
from __future__ import annotations

import argparse
import os
import time
import traceback
from typing import Optional

from loguru import logger

from .dataset_config import DatasetCrawlConfig
from .dataset_crawler import DatasetCrawler
from .pipeline import LocalSqliteTaskQueue, TaskQueue


def handle_task(task) -> None:
    """执行单条已 claim 的任务。"""
    if task.kind == "dataset_crawl":
        raw = task.payload.get("config") or task.payload
        config = DatasetCrawlConfig.from_mapping(raw)
        logger.info(
            "worker running dataset_crawl task={} keywords={} total={}",
            task.task_id,
            config.keywords,
            config.total_count,
        )
        DatasetCrawler.from_config(config).crawl()
        return
    raise ValueError(f"unsupported task kind: {task.kind}")


def run_worker(
    queue: TaskQueue,
    *,
    kind: Optional[str] = "dataset_crawl",
    poll_interval: float = 2.0,
    once: bool = False,
) -> int:
    """轮询领取并执行任务；返回处理条数。"""
    processed = 0
    while True:
        task = queue.claim(kind=kind)
        if task is None:
            if once:
                logger.info("worker: no pending tasks, exiting (--once)")
                return processed
            time.sleep(max(0.1, poll_interval))
            continue
        try:
            handle_task(task)
            queue.complete(task.task_id)
            processed += 1
            logger.info("worker completed task={}", task.task_id)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.error("worker failed task={}: {}\n{}", task.task_id, detail, traceback.format_exc())
            queue.complete(task.task_id, error=detail)
            processed += 1
        if once:
            return processed


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="smart-spider dataset job worker")
    parser.add_argument(
        "--queue",
        default=os.environ.get(
            "SMART_SPIDER_API_QUEUE",
            os.path.join("runs", "task_queue.sqlite3"),
        ),
        help="SQLite 队列路径（默认与 API 一致）",
    )
    parser.add_argument("--kind", default="dataset_crawl", help="领取的任务类型")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true", help="处理至多一条后退出")
    args = parser.parse_args(argv)

    queue = LocalSqliteTaskQueue(args.queue)
    run_worker(
        queue,
        kind=args.kind or None,
        poll_interval=args.poll_interval,
        once=args.once,
    )


if __name__ == "__main__":
    main()
