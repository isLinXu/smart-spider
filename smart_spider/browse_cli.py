# coding=utf-8
"""授权站点浏览 CLI（S17–S20）。

示例::

    smart-spider browse --profile examples/site_profiles/example.com.yaml
    smart-spider browse --profile ./site.yaml --once
    smart-spider browse --profile ./site.yaml --login --export-storage-state ./state.json
    smart-spider browse --profile ./site.yaml --retry-dead --block-kind challenge
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from .compliance import (
    build_publish_checklist,
    checklist_exit_code,
    write_publish_checklist,
)
from .config_io import ConfigIOError, dump_mapping
from .pipeline import get_task_queue
from .report import UnifiedReport, format_browse_summary, summarize_browse_results
from .site_profile import SiteProfile, load_site_profile


def _split_urls(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def enqueue_profile_jobs(
    profile: SiteProfile,
    urls: list[str],
    *,
    queue=None,
    backend: str = "sqlite",
    queue_path: str = "",
) -> list[str]:
    """将种子 URL 入队为 authorized_browse 任务，返回 task_id 列表。"""
    accepted = profile.validate_seeds(urls)
    if queue is None:
        if backend in {"sqlite", "local", ""}:
            queue = get_task_queue(
                "sqlite",
                path=queue_path
                or os.environ.get(
                    "SMART_SPIDER_API_QUEUE",
                    os.path.join("runs", "task_queue.sqlite3"),
                ),
            )
        else:
            queue = get_task_queue(
                "redis",
                url=os.environ.get("SMART_SPIDER_REDIS_URL"),
            )
    task_ids: list[str] = []
    for url in accepted:
        record = queue.enqueue(
            "authorized_browse",
            profile.browse_payload(url, depth=0),
            max_attempts=profile.max_attempts,
        )
        task_ids.append(record.task_id)
    return task_ids


def run_profile_once(
    profile: SiteProfile,
    urls: list[str],
    *,
    controller=None,
    bfs: bool = True,
) -> list[dict]:
    """本地执行授权浏览剧本（默认创建 BrowserController）。"""
    from .browser_controller import BrowserController
    from .browser_playbook import AuthorizedBrowsePlaybook

    accepted = profile.validate_seeds(urls)
    owns_controller = controller is None
    if controller is None:
        storage_state, cookies = profile.policy.resolve_session()
        controller = BrowserController(
            headless=profile.headless,
            cookies=cookies or None,
            storage_state=storage_state,
            allow_private_hosts=profile.policy.allow_private_hosts,
        )
    results: list[dict] = []
    try:
        playbook = AuthorizedBrowsePlaybook(
            controller, profile.policy, steps=profile.steps
        )
        if bfs:
            pages = playbook.crawl_bfs(accepted, steps=profile.steps)
        else:
            pages = [playbook.run(url, depth=0, steps=profile.steps) for url in accepted]
        for result in pages:
            payload = result.to_dict()
            payload["profile"] = profile.name
            results.append(payload)
    finally:
        if owns_controller:
            controller.close()
    return results


def retry_dead_tasks(
    *,
    queue,
    block_kind: str = "",
    reset_attempts: bool = True,
) -> list[str]:
    """Retry dead authorized_browse tasks, optionally filtered by block kind."""
    records = queue.list_tasks(
        status="dead",
        kind="authorized_browse",
        block_kind=block_kind or None,
        limit=1000,
    )
    retried: list[str] = []
    for record in records:
        updated = queue.retry(record.task_id, reset_attempts=reset_attempts)
        retried.append(updated.task_id)
    return retried


def _block_kinds(results: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        block = item.get("block") or {}
        kind = str(block.get("kind") or "")
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="授权站点浏览：按 SiteProfile 校验种子 URL 并入队或本地执行",
    )
    parser.add_argument(
        "--profile",
        "-p",
        required=True,
        help="SiteProfile YAML/JSON 路径",
    )
    parser.add_argument(
        "--url",
        help="额外种子 URL，逗号分隔（与 profile.seed_urls 合并）",
    )
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="将任务写入队列（默认只校验并打印计划）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="本地立即执行浏览剧本（需要 Playwright）",
    )
    parser.add_argument(
        "--no-bfs",
        action="store_true",
        help="--once 时只访问种子，不做同域 BFS",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="显示浏览器等待人工登录，然后导出会话",
    )
    parser.add_argument(
        "--export-storage-state",
        help="将 Playwright storage_state 写到该路径",
    )
    parser.add_argument(
        "--retry-dead",
        action="store_true",
        help="将 dead 的 authorized_browse 任务重新入队",
    )
    parser.add_argument(
        "--block-kind",
        default="",
        help="按 block kind 过滤死信（challenge/auth_required/...）",
    )
    parser.add_argument(
        "--allow-unready",
        action="store_true",
        help="publish_checklist.ready=false 时仍返回 0",
    )
    parser.add_argument(
        "--output",
        default="",
        help="浏览报告与 publish checklist 输出目录",
    )
    parser.add_argument(
        "--backend",
        default=os.environ.get("SMART_SPIDER_QUEUE_BACKEND", "sqlite"),
        choices=("sqlite", "local", "redis"),
        help="队列后端（配合 --enqueue / --retry-dead）",
    )
    parser.add_argument(
        "--queue",
        default=os.environ.get(
            "SMART_SPIDER_API_QUEUE",
            os.path.join("runs", "task_queue.sqlite3"),
        ),
        help="SQLite 队列路径",
    )
    parser.add_argument(
        "--dump-resolved",
        help="写出解析后的 SiteProfile JSON/YAML",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="覆盖 profile.max_attempts",
    )
    parser.add_argument(
        "--no-enqueue-links",
        action="store_true",
        help="发现链接后不自动入队后续任务",
    )
    return parser


def _open_queue(args):
    if args.backend in {"sqlite", "local", ""}:
        return get_task_queue("sqlite", path=args.queue)
    return get_task_queue("redis", url=os.environ.get("SMART_SPIDER_REDIS_URL"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    overrides: dict = {}
    if args.max_attempts is not None:
        overrides["max_attempts"] = args.max_attempts
    if args.no_enqueue_links:
        overrides["enqueue_links"] = False

    try:
        profile = load_site_profile(args.profile, overrides=overrides or None)
    except (ConfigIOError, ValueError, TypeError) as exc:
        raise SystemExit(f"invalid site profile: {exc}") from exc

    urls = profile.resolve_seeds(_split_urls(args.url))
    try:
        accepted = profile.validate_seeds(urls)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.dump_resolved:
        dump_mapping(profile.to_dict(), args.dump_resolved)

    plan = {
        "profile": profile.name,
        "urls": accepted,
        "policy": profile.policy.to_dict(),
        "enqueue_links": profile.enqueue_links,
        "max_attempts": profile.max_attempts,
        "compliance": profile.compliance_policy().to_dict(),
        "steps": [step.to_dict() for step in profile.steps],
    }

    exclusive = [args.enqueue, args.once, args.login, args.retry_dead]
    if sum(bool(item) for item in exclusive) > 1:
        raise SystemExit("use only one of --enqueue, --once, --login, --retry-dead")

    if args.retry_dead:
        queue = _open_queue(args)
        ids = retry_dead_tasks(
            queue=queue,
            block_kind=args.block_kind,
        )
        plan["mode"] = "retry-dead"
        plan["task_ids"] = ids
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    if args.enqueue:
        task_ids = enqueue_profile_jobs(
            profile,
            accepted,
            backend=args.backend,
            queue_path=args.queue,
        )
        plan["mode"] = "enqueue"
        plan["task_ids"] = task_ids
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    if args.login:
        from .browser_controller import BrowserController

        export_path = args.export_storage_state or profile.policy.storage_state_path
        if not export_path:
            raise SystemExit("--login requires --export-storage-state or profile.storage_state_path")
        controller = BrowserController(
            headless=False,
            allow_private_hosts=profile.policy.allow_private_hosts,
        )
        try:
            seed = accepted[0]
            controller.navigate(seed)
            print(
                f"Complete login in the browser for {seed}, then press Enter to export storage_state.",
                file=sys.stderr,
            )
            try:
                input()
            except EOFError as exc:
                raise SystemExit("login aborted: no terminal input") from exc
            controller.export_storage_state(export_path)
        finally:
            controller.close()
        plan["mode"] = "login"
        plan["storage_state"] = export_path
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    if args.once:
        results = run_profile_once(
            profile, accepted, bfs=not args.no_bfs
        )
        plan["mode"] = "once"
        plan["results"] = results
        block_kinds = _block_kinds(results)
        plan["block_kinds"] = block_kinds
        summary = summarize_browse_results(results)
        plan["summary"] = summary
        output_dir = args.output or os.path.join("runs", "browse", profile.name)
        os.makedirs(output_dir, exist_ok=True)
        checklist = build_publish_checklist(
            job_id=profile.name,
            policy=profile.compliance_policy(),
            block_kinds=block_kinds,
        )
        plan["publish_checklist_path"] = write_publish_checklist(output_dir, checklist)
        plan["publish_checklist"] = checklist.to_dict()
        unified_path = os.path.join(output_dir, "unified_report.json")
        plan["unified_report_path"] = unified_path
        UnifiedReport.from_browse_plan(plan, job_id=profile.name).write(unified_path)
        print(format_browse_summary(summary), file=sys.stderr)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        code = checklist_exit_code(checklist, allow_unready=args.allow_unready)
        if code:
            print(
                "publish_checklist.ready is false; pass --allow-unready to ignore",
                file=sys.stderr,
            )
        return code

    plan["mode"] = "dry-run"
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
