# coding=utf-8
"""Unified ``smart-spider`` CLI with subcommands.

Legacy ``smart-spider --keywords ...`` still dispatches to search crawl.
Installed aliases (``smart-spider-dataset`` etc.) keep calling their modules.
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable, Optional

COMMANDS: dict[str, tuple[str, str, str]] = {
    "crawl": ("spider", "main", "搜索引擎 / 站点深度采集"),
    "dataset": ("smart_spider.dataset_cli", "main", "工业图片数据集采集"),
    "multimodal": ("smart_spider.multimodal_cli", "main", "多模态编排任务"),
    "browse": ("smart_spider.browse_cli", "main", "授权站点浏览"),
    "douyin": ("smart_spider.douyin_cli", "main", "抖音视频采集"),
    "xiaohongshu": ("smart_spider.xiaohongshu_cli", "main", "小红书封面采集"),
    "image-search": ("smart_spider.image_search_cli", "main", "本地以图搜图"),
    "reverse-image-search": (
        "smart_spider.reverse_image_search_cli",
        "main",
        "远程网页以图搜图",
    ),
    "filter": ("smart_spider.dataset_filter_cli", "main", "离线广告/语义过滤"),
    "quality-validate": (
        "smart_spider.quality_validation_cli",
        "main",
        "场景数据集质量校验",
    ),
    "api": ("smart_spider.api", "main", "轻量任务 API"),
    "worker": ("smart_spider.worker", "main", "队列 worker"),
    "db-stats": ("smart_spider.db_stats_cli", "main", "SQLite 状态统计"),
}


def _load_main(module_name: str, attr: str) -> Callable:
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def _invoke(func: Callable, argv: list[str]) -> int:
    result = func(argv)
    if result is None:
        return 0
    return int(result)


def _help_text() -> str:
    lines = [
        "usage: smart-spider <command> [args]",
        "",
        "Commands:",
    ]
    width = max(len(name) for name in COMMANDS)
    for name, (_mod, _fn, summary) in COMMANDS.items():
        lines.append(f"  {name.ljust(width)}  {summary}")
    lines.append("")
    lines.append("Legacy: smart-spider --keywords ... still runs the crawl command.")
    lines.append("Each command also remains available as smart-spider-<command>.")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_help_text())
        return 0
    command = args[0]
    if command in COMMANDS:
        module_name, attr, _summary = COMMANDS[command]
        func = _load_main(module_name, attr)
        return _invoke(func, args[1:])
    if command.startswith("-"):
        return _invoke(_load_main("spider", "main"), args)
    parser = argparse.ArgumentParser(prog="smart-spider", add_help=False)
    parser.error(f"unknown command {command!r}\n\n{_help_text()}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
