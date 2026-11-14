# coding=utf-8
"""任务队列 worker：claim → 执行 dataset_crawl / authorized_browse → complete。

用法::

    smart-spider-api &
    smart-spider worker --once
    smart-spider worker --kind dataset_crawl --once
    smart-spider-worker --workers 2
    SMART_SPIDER_QUEUE_BACKEND=redis smart-spider-worker
"""
from __future__ import annotations

import argparse
import os
import signal
import time
import traceback
import uuid
from multiprocessing import Process
from typing import Optional

from loguru import logger

from .dataset_config import DatasetCrawlConfig
from .dataset_crawler import DatasetCrawler
from .pipeline import get_task_queue
from .pipeline.protocols import StaleClaimError, TaskQueue
from .task_execution import TaskExecutionContext, dataset_writer_lock


def handle_task(task, queue: Optional[TaskQueue] = None, execution_context=None) -> None:
    """执行单条已 claim 的任务。"""
    if task.kind == "dataset_crawl":
        if execution_context is not None and not callable(getattr(queue, "lease_guard", None)):
            raise UnsupportedTaskBackendError(
                "dataset_crawl requires a SQLite queue with local publication fencing; "
                "Redis dataset execution is not supported"
            )
        raw = task.payload.get("config") or task.payload
        config = DatasetCrawlConfig.from_mapping(raw)
        if execution_context is not None and config.state_db == "":
            raise UnsupportedTaskBackendError(
                "dataset_crawl workers require a state database and guarded repository; "
                "legacy state_db='' mode is only supported by direct CLI execution"
            )
        if execution_context is not None:
            state_path = config.state_db or os.path.join(config.output_dir, ".dataset_state.sqlite3")
            queue_path = getattr(queue, "db_path", None)
            if queue_path:
                same_file = os.path.realpath(state_path) == os.path.realpath(queue_path)
                if os.path.exists(state_path) and os.path.exists(queue_path):
                    same_file = same_file or os.path.samefile(state_path, queue_path)
                if same_file:
                    raise UnsupportedTaskBackendError(
                        "dataset state database must differ from the queue database; "
                        "publication fencing holds a queue write transaction"
                    )
        logger.info(
            "worker running dataset_crawl task={} keywords={} total={}",
            task.task_id,
            config.keywords,
            config.total_count,
        )
        options = {} if execution_context is None else {
            "commit_guard": execution_context.commit_guard,
            "check_active": execution_context.check_active,
        }
        with dataset_writer_lock(config.output_dir):
            if execution_context is not None:
                execution_context.check_active()
            DatasetCrawler.from_config(config, **options).crawl()
        return
    if task.kind == "authorized_browse":
        _handle_authorized_browse(task, queue, execution_context=execution_context)
        return
    raise ValueError(f"unsupported task kind: {task.kind}")


def _handle_authorized_browse(task, queue: Optional[TaskQueue], execution_context=None) -> dict:
    """授权浏览剧本：导航/滚动/抽链，可选入队后续链接。"""
    from .block_signals import BlockKind
    from .browser_controller import BrowserController
    from .browser_playbook import AuthorizedBrowsePlaybook, enqueue_discovered_links
    from .site_policy import SiteCrawlPolicy

    url = str(task.payload.get("url") or "").strip()
    if not url:
        raise ValueError("authorized_browse payload requires url")
    if execution_context is not None:
        execution_context.check_active()
    policy = SiteCrawlPolicy.from_mapping(task.payload.get("policy") or {})
    depth = int(task.payload.get("depth") or 0)
    storage_state, cookies = policy.resolve_session()
    controller = BrowserController(
        headless=bool(task.payload.get("headless", True)),
        cookies=cookies or None,
        storage_state=storage_state,
        allow_private_hosts=policy.allow_private_hosts,
        site_policy=policy,
    )
    try:
        playbook = AuthorizedBrowsePlaybook(
            controller,
            policy,
            steps=task.payload.get("steps") or (),
        )
        result = playbook.run(url, depth=depth)
    finally:
        controller.close()

    signal = result.block
    if signal is not None and signal.kind != BlockKind.OK:
        # Propagate as exception; run_worker applies terminal for non-retryable.
        raise BrowseBlockError(signal)

    follow_ids: list[str] = []
    enqueue_links = bool(task.payload.get("enqueue_links", True))
    if enqueue_links and queue is not None and depth < policy.max_depth:
        if execution_context is not None:
            execution_context.check_active()
        follow_ids = enqueue_discovered_links(
            queue,
            result.links,
            policy=policy,
            max_attempts=int(task.payload.get("max_attempts") or task.max_attempts or 3),
            extra_payload={
                "enqueue_links": enqueue_links,
                "headless": bool(task.payload.get("headless", True)),
                "steps": task.payload.get("steps") or [],
                "profile": task.payload.get("profile") or "",
            },
            check_active=None if execution_context is None else execution_context.check_active,
        )
    logger.info(
        "authorized_browse task={} url={} links={} enqueued={}",
        task.task_id,
        url,
        len(result.links),
        len(follow_ids),
    )
    return {
        "result": result.to_dict(),
        "enqueued_task_ids": follow_ids,
    }


class BrowseBlockError(RuntimeError):
    """携带 BlockSignal，供 worker 决定 retry/dead。"""

    def __init__(self, signal):
        super().__init__(signal.queue_error)
        self.signal = signal


class UnsupportedTaskBackendError(RuntimeError):
    """The selected backend cannot safely execute this task kind."""


def run_worker(
    queue: TaskQueue,
    *,
    kind: Optional[str] = None,
    poll_interval: float = 2.0,
    once: bool = False,
    lease_seconds: float = 300.0,
    worker_id: str = "",
    recover_every: int = 10,
    stop_flag: Optional[list] = None,
) -> int:
    """轮询领取并执行任务；返回处理条数。

    ``stop_flag`` 若提供，则为单元素 list，truthy 时优雅退出。
    """
    processed = 0
    polls = 0
    identity = worker_id or f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    while True:
        if stop_flag and stop_flag[0]:
            logger.info("worker {} stopping after graceful signal", identity)
            return processed
        if recover_every > 0 and polls % recover_every == 0:
            try:
                recovered = queue.recover_expired_claims()
                if recovered:
                    logger.info("worker recovered {} expired claims", recovered)
            except Exception as exc:
                logger.warning("recover_expired_claims failed: {}", exc)
        polls += 1
        task = queue.claim(
            kind=kind,
            lease_seconds=lease_seconds,
            worker_id=identity,
        )
        if task is None:
            if once:
                logger.info("worker: no pending tasks, exiting (--once)")
                return processed
            time.sleep(max(0.1, poll_interval))
            continue
        context = TaskExecutionContext(queue, task, lease_seconds=lease_seconds)
        error = ""
        terminal = False
        stale = False
        try:
            with context:
                handle_task(task, queue=queue, execution_context=context)
                context.check_active()
        except StaleClaimError:
            stale = True
            logger.warning("worker discarded stale task={}", task.task_id)
        except BrowseBlockError as exc:
            error = str(exc)
            terminal = not exc.signal.retryable
        except UnsupportedTaskBackendError as exc:
            error = str(exc)
            terminal = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("worker failed task={}: {}\n{}", task.task_id, error, traceback.format_exc())
        if not stale:
            try:
                done = queue.complete(
                    task.task_id, error=error, terminal=terminal, **context.claim_kwargs,
                )
            except StaleClaimError:
                logger.warning("worker completion rejected for stale task={}", task.task_id)
            else:
                from .metrics import observe_task_outcome

                observe_task_outcome(task.kind, done.status)
                logger.info("worker task={} status={}", task.task_id, done.status)
        processed += 1
        if once:
            return processed


def _worker_process_main(
    backend: str,
    queue_kwargs: dict,
    *,
    kind: Optional[str],
    poll_interval: float,
    lease_seconds: float,
    recover_every: int,
) -> None:
    stop_flag = [False]

    def _handle(signum, _frame):
        logger.warning("worker process received signal {}, shutting down", signum)
        stop_flag[0] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    queue = get_task_queue(backend, **queue_kwargs)
    run_worker(
        queue,
        kind=kind,
        poll_interval=poll_interval,
        lease_seconds=lease_seconds,
        recover_every=recover_every,
        stop_flag=stop_flag,
    )


def run_worker_pool(
    *,
    backend: str = "sqlite",
    queue_kwargs: Optional[dict] = None,
    workers: int = 1,
    kind: Optional[str] = None,
    poll_interval: float = 2.0,
    once: bool = False,
    lease_seconds: float = 300.0,
    recover_every: int = 10,
) -> int:
    """单进程或多进程 worker 入口。"""
    queue_kwargs = dict(queue_kwargs or {})
    if workers <= 1 or once:
        queue = get_task_queue(backend, **queue_kwargs)
        stop_flag = [False]

        def _handle(signum, _frame):
            logger.warning("worker received signal {}, shutting down", signum)
            stop_flag[0] = True

        previous_int = signal.signal(signal.SIGINT, _handle)
        previous_term = signal.signal(signal.SIGTERM, _handle)
        try:
            return run_worker(
                queue,
                kind=kind,
                poll_interval=poll_interval,
                once=once,
                lease_seconds=lease_seconds,
                recover_every=recover_every,
                stop_flag=stop_flag,
            )
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)

    processes: list[Process] = []
    for index in range(workers):
        process = Process(
            target=_worker_process_main,
            kwargs={
                "backend": backend,
                "queue_kwargs": queue_kwargs,
                "kind": kind,
                "poll_interval": poll_interval,
                "lease_seconds": lease_seconds,
                "recover_every": recover_every,
            },
            name=f"smart-spider-worker-{index}",
        )
        process.start()
        processes.append(process)
        logger.info("started worker process pid={} name={}", process.pid, process.name)

    stop_flag = [False]

    def _handle(signum, _frame):
        logger.warning("pool received signal {}, terminating children", signum)
        stop_flag[0] = True
        for process in processes:
            if process.is_alive():
                process.terminate()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    while any(process.is_alive() for process in processes) and not stop_flag[0]:
        time.sleep(0.2)
    for process in processes:
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
    return 0


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="smart-spider queue worker")
    parser.add_argument(
        "--backend",
        default=os.environ.get("SMART_SPIDER_QUEUE_BACKEND", "sqlite"),
        choices=("sqlite", "local", "redis"),
        help="队列后端（可用环境变量 SMART_SPIDER_QUEUE_BACKEND）",
    )
    parser.add_argument(
        "--queue",
        default=os.environ.get(
            "SMART_SPIDER_API_QUEUE",
            os.path.join("runs", "task_queue.sqlite3"),
        ),
        help="SQLite 队列路径（backend=sqlite）",
    )
    parser.add_argument(
        "--redis-url",
        default=os.environ.get("SMART_SPIDER_REDIS_URL"),
        help="Redis URL（backend=redis）",
    )
    parser.add_argument(
        "--kind",
        default="",
        help="领取的任务类型；留空则领取队列中任意 kind（dataset_crawl / authorized_browse 等）",
    )
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=1, help="进程数（>1 启用多进程）")
    parser.add_argument("--once", action="store_true", help="处理至多一条后退出")
    parser.add_argument(
        "--recover-every",
        type=int,
        default=10,
        help="每 N 次轮询回收一次超时 claim（0 关闭）",
    )
    args = parser.parse_args(argv)

    backend = args.backend
    if backend in {"sqlite", "local"}:
        queue_kwargs = {"path": args.queue}
    else:
        queue_kwargs = {"url": args.redis_url}

    run_worker_pool(
        backend=backend,
        queue_kwargs=queue_kwargs,
        workers=max(1, int(args.workers)),
        kind=args.kind or None,
        poll_interval=args.poll_interval,
        once=args.once,
        lease_seconds=args.lease_seconds,
        recover_every=args.recover_every,
    )


if __name__ == "__main__":
    main()
