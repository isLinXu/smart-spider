"""CLI for Douyin video links, user profiles and keyword searches."""
from __future__ import annotations

import argparse
import json
import queue
import threading
from pathlib import Path

from .douyin import DouyinCrawler, _byte_limit, download_item
from .http_client import SmartHttpClient
from .media_io import VideoDownloader


def build_parser():
    parser = argparse.ArgumentParser(description="抖音视频采集：分享链接、用户主页或关键词")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="视频/用户主页链接，或包含分享链接的完整文案")
    source.add_argument("--keyword", help="抖音视频搜索关键词")
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--output", default="./output_douyin")
    parser.add_argument("--cookies-file", help="Netscape cookies.txt，浏览器与 yt-dlp 共用")
    parser.add_argument("--no-headless", action="store_true", help="显示浏览器")
    parser.add_argument("--browser-channel", choices=["chrome", "msedge"],
                        help="使用已安装的 Chrome/Edge（独立临时会话）")
    parser.add_argument("--login-wait", type=int, default=0, help="页面打开后等待人工登录/验证的秒数")
    parser.add_argument("--wait-for-login", action="store_true",
                        help="显示浏览器并等待终端回车确认后继续，不按固定时间关闭")
    parser.add_argument("--max-scrolls", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--proxy", help="浏览器和下载共用代理")
    parser.add_argument("--max-size", default="500m", help="单视频大小上限")
    parser.add_argument("--metadata-only", action="store_true", help="只保存视频信息，不下载视频")
    return parser


def _confirm_login(page):
    """Keep Playwright events flowing while the operator completes verification."""
    result = queue.Queue(maxsize=1)

    def read_confirmation():
        try:
            input("请在 Chrome 完成验证并确认看到搜索结果，然后在此终端按回车继续：\n")
            result.put(None)
        except (EOFError, OSError) as exc:
            result.put(exc)

    threading.Thread(target=read_confirmation, daemon=True).start()
    while True:
        try:
            error = result.get_nowait()
        except queue.Empty:
            if page.is_closed():
                raise RuntimeError("人工验证浏览器已关闭")
            page.wait_for_timeout(500)
            continue
        if error is not None:
            raise RuntimeError("人工确认需要交互式终端，请在终端运行 --wait-for-login") from error
        return


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_items <= 0:
        parser.error("--max-items 必须大于 0")
    crawler = None
    try:
        _byte_limit(args.max_size)
        crawler = DouyinCrawler(cookies_file=args.cookies_file, headless=not (args.no_headless or args.wait_for_login),
                                proxy=args.proxy, timeout=args.timeout,
                                max_scrolls=args.max_scrolls, login_wait=args.login_wait,
                                browser_channel=args.browser_channel,
                                login_confirmation=_confirm_login if args.wait_for_login else None)
        items = crawler.discover(args.keyword or args.url, keyword=args.keyword is not None,
                                 limit=args.max_items)
    except Exception as exc:
        if crawler is not None and isinstance(crawler.diagnostics, dict):
            output = Path(args.output)
            output.mkdir(parents=True, exist_ok=True)
            (output / "douyin_diagnostics.json").write_text(
                json.dumps(crawler.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        parser.exit(1, f"抖音采集失败：{exc}\n")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with SmartHttpClient(proxies=[args.proxy] if args.proxy else [], rate=1,
                         timeout=args.timeout) as http:
        downloader = VideoDownloader(str(output), proxy=args.proxy, max_filesize=args.max_size,
                                     cookies_file=args.cookies_file, timeout=args.timeout * 30)
        for item in items:
            success = None if args.metadata_only else download_item(
                item, str(output), downloader=downloader, http_client=http)
            # Signed media URLs are ephemeral and are not persisted in public manifests.
            results.append({"url": item["url"],
                            "meta": {k: v for k, v in item["meta"].items() if k != "media_url"},
                            "downloaded": success})
    report = {"discovered": len(items), "downloaded": sum(r["downloaded"] is True for r in results),
              "failed": sum(r["downloaded"] is False for r in results), "items": results}
    (output / "douyin_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "items"}, ensure_ascii=False))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
