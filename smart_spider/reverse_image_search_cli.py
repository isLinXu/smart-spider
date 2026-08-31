# coding=utf-8
"""远程网页以图搜图 CLI。

示例：
    smart-spider-reverse-image-search \
        --image ./query.jpg --providers baidu google_lens bing
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from .reverse_image_search import ReverseImageSearcher, resolve_providers


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将本地图片上传到百度、Bing、Google Lens 等网页并返回以图搜图结果"
    )
    parser.add_argument("--image", required=True, help="本地查询图片路径")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=["all"],
        help="Provider 列表：baidu、bing、google_lens、all（默认 all）",
    )
    parser.add_argument("--top-k", type=int, default=20, help="每个 Provider 返回的结果数")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器窗口，便于登录或处理验证")
    parser.add_argument("--proxy", help="浏览器代理，例如 http://127.0.0.1:7890")
    parser.add_argument(
        "--browser-executable",
        help="浏览器可执行文件路径；默认自动查找本机 Chrome/Chromium",
    )
    parser.add_argument(
        "--debug-dir",
        help="失败时保存 Provider 页面 HTML 和截图的目录",
    )
    parser.add_argument(
        "--user-data-dir",
        help="持久化浏览器用户目录，用于保留登录态和站点 Cookie",
    )
    parser.add_argument("--timeout", type=float, default=45.0, help="单个 Provider 超时秒数")
    parser.add_argument("--settle", type=float, default=4.0, help="上传后等待结果加载秒数")
    parser.add_argument("--fail-fast", action="store_true", help="某个 Provider 失败后停止后续 Provider")
    parser.add_argument("--output", help="将 JSON 结果写入文件；默认输出到终端")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.top_k <= 0:
        parser.error("--top-k must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.settle < 0:
        parser.error("--settle must be non-negative")
    try:
        providers = resolve_providers(args.providers)
    except ValueError as exc:
        parser.error(str(exc))

    searcher = ReverseImageSearcher(
        providers=providers,
        headless=not args.no_headless,
        proxy=args.proxy,
        user_data_dir=args.user_data_dir,
        browser_executable=args.browser_executable,
        debug_dir=args.debug_dir,
        timeout_ms=int(args.timeout * 1000),
        settle_ms=int(args.settle * 1000),
    )
    try:
        response = searcher.search(args.image, top_k=args.top_k, fail_fast=args.fail_fast)
    except Exception as exc:
        parser.exit(2, f"reverse image search failed: {exc}\n")
    payload = json.dumps(response.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
