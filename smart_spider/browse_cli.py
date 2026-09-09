# coding=utf-8
"""授权站点浏览 CLI（S17）。

示例::

    smart-spider-browse --profile examples/site_profiles/example.com.yaml
    smart-spider-browse --profile ./site.yaml --url https://example.com/a --enqueue
    smart-spider-browse --profile ./site.yaml --once
    smart-spider-browse --profile ./site.yaml --dump-resolved ./resolved.json
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Optional

from .config_io import ConfigIOError, dump_mapping
from .pipeline import get_task_queue
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
        playbook = AuthorizedBrowsePlaybook(controller, profile.policy)
        for url in accepted:
            result = playbook.run(url, depth=0)
            payload = result.to_dict()
            payload["profile"] = profile.name
            results.append(payload)
    finally:
        if owns_controller:
            controller.close()
    return results


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
        "--backend",
        default=os.environ.get("SMART_SPIDER_QUEUE_BACKEND", "sqlite"),
        choices=("sqlite", "local", "redis"),
        help="队列后端（配合 --enqueue）",
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
    }

    if args.enqueue and args.once:
        raise SystemExit("use either --enqueue or --once, not both")

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

    if args.once:
        results = run_profile_once(profile, accepted)
        plan["mode"] = "once"
        plan["results"] = results
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    plan["mode"] = "dry-run"
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
