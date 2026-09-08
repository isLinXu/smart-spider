# coding=utf-8
"""通用图文多模态数据集采集 CLI。

示例：
    python -m smart_spider.multimodal_cli --queries "猫咪,窗边猫" --output ./mm_data
    python -m smart_spider.multimodal_cli --urls "https://example.com/article" --output ./mm_data
    python -m smart_spider.multimodal_cli --site jdlingyu --output ./mm_data
"""
from __future__ import annotations

import argparse
import json
from typing import Optional

from .dataset_contracts import LabelMode, LabelPolicy, Modality
from .http_client import SmartHttpClient
from .multimodal_job import MultimodalDatasetOrchestrator, MultimodalJobConfig
from .multimodal_pipeline import (
    BrowserPageSource,
    DiscoveryTask,
    PageSampleExtractor,
    PlaybookBrowserSource,
    StaticPageSource,
)
from .multimodal_sources import SearchDiscoverySource, SiteDiscoverySource


def _split(value: Optional[str]) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SmartSpider 通用图文多模态数据集采集器",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--queries", help="搜索词，逗号分隔")
    source.add_argument("--urls", help="网页 URL，逗号分隔")
    source.add_argument("--site", help="已注册的站点解析器名称")

    parser.add_argument("--output", "-o", default="./multimodal_dataset")
    parser.add_argument("--max-samples", type=int, default=1000)
    parser.add_argument(
        "--manifest-shard-size",
        type=int,
        default=0,
        help="每个 manifest 分片的样本数；0 表示兼容的单文件 manifest.jsonl",
    )
    parser.add_argument(
        "--annotation-batch-size",
        type=int,
        default=32,
        help="批量标注的最大样本数",
    )
    parser.add_argument(
        "--quality-report",
        help="质量报告路径，默认写入 output/quality_report.json",
    )
    parser.add_argument("--pages", type=int, default=1, help="搜索或站点页数")
    parser.add_argument("--engines", help="搜索引擎名称，逗号分隔；默认使用注册表")
    parser.add_argument(
        "--modalities",
        default="text,image,webpage",
        help="允许的模态，逗号分隔（text,image,webpage,video,audio）",
    )
    parser.add_argument("--labels", help="正式标签，逗号分隔")
    parser.add_argument(
        "--label-mode",
        choices=[item.value for item in LabelMode],
        default=LabelMode.HYBRID.value,
    )
    parser.add_argument(
        "--scene",
        help="启用内置安全场景质量门（需配合 --scene-quality-gate）",
    )
    parser.add_argument(
        "--scene-quality-gate",
        action="store_true",
        help="启用安全场景质量门；缺失专用信号进入人工复核队列",
    )
    parser.add_argument(
        "--scene-review-queue",
        help="场景质量人工复核 JSONL 路径（默认 output/scene_review_queue.jsonl）",
    )
    parser.add_argument("--no-materialize-images", action="store_true")
    parser.add_argument("--browser-fallback", action="store_true", help="URL/站点页面静态失败时启用 Playwright")
    parser.add_argument(
        "--browser-playbook",
        action="store_true",
        help="静态失败时用授权浏览剧本（滚动+抽链）；隐含 --browser-fallback",
    )
    parser.add_argument(
        "--allow-hosts",
        help="授权浏览允许的域名，逗号分隔（配合 --browser-playbook）",
    )
    parser.add_argument(
        "--deny-hosts",
        help="授权浏览拒绝的域名，逗号分隔",
    )
    parser.add_argument(
        "--storage-state",
        help="Playwright storage_state JSON 路径（登录态导入）",
    )
    parser.add_argument(
        "--session-cookies",
        help="Cookie JSON 路径（list 或 {cookies:[...]}）",
    )
    parser.add_argument(
        "--export-storage-state",
        help="任务结束后导出当前浏览器 storage_state 到该路径",
    )
    parser.add_argument(
        "--respect-robots",
        action="store_true",
        help="授权浏览时遵守 robots.txt（默认关闭，避免误伤公开页）",
    )
    parser.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="允许访问受控内网地址（默认拒绝，以防 SSRF）",
    )
    parser.add_argument("--proxy", action="append", default=[])
    parser.add_argument("--rate", type=float, default=5.0)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--connect-timeout", type=float, help="HTTP 连接超时秒数")
    parser.add_argument("--read-timeout", type=float, help="HTTP 读取超时秒数")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-image-bytes", type=int, default=25 * 1024 * 1024,
        help="单张图片最大响应字节数（默认 25 MiB）",
    )
    parser.add_argument(
        "--max-image-pixels", type=int, default=50_000_000,
        help="单张图片最大解码像素数（默认 50,000,000）",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.scene_quality_gate and not args.scene:
        parser.error("--scene-quality-gate requires --scene")
    modalities = tuple(Modality(item) for item in _split(args.modalities))
    labels = _split(args.labels)
    policy = LabelPolicy(
        mode=args.label_mode,
        fixed_labels=labels,
    )
    config = MultimodalJobConfig(
        output_dir=args.output,
        max_samples=args.max_samples,
        manifest_shard_size=args.manifest_shard_size,
        annotation_batch_size=args.annotation_batch_size,
        max_image_bytes=args.max_image_bytes,
        max_image_pixels=args.max_image_pixels,
        quality_report_path=args.quality_report,
        materialize_images=not args.no_materialize_images,
        allowed_modalities=modalities,
        label_policy=policy,
        scene_quality_gate_enabled=args.scene_quality_gate,
        scene=args.scene,
        scene_review_queue_path=args.scene_review_queue,
    )
    http_client = SmartHttpClient(
        proxies=args.proxy,
        rate=args.rate,
        timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        max_retries=args.max_retries,
        allow_private_hosts=args.allow_private_hosts,
    )
    extractor = PageSampleExtractor()
    orchestrator = MultimodalDatasetOrchestrator(
        config,
        http_client=http_client,
        extractor=extractor,
    )
    browser_controller = None
    browser_source = None
    try:
        if args.queries:
            engines = _split(args.engines)
            static_source = SearchDiscoverySource(http_client, engines=engines or None)
            tasks = [DiscoveryTask(
                query=query,
                source="search",
                modalities=modalities,
                min_candidates=1,
                metadata={
                    "engines": engines,
                    "pages": args.pages,
                    "max_candidates": args.max_samples * 2,
                },
            ) for query in _split(args.queries)]
        elif args.urls:
            static_source = StaticPageSource(http_client, extractor)
            tasks = [DiscoveryTask(
                start_url=url,
                source="url",
                modalities=modalities,
                min_candidates=1,
            ) for url in _split(args.urls)]
        else:
            static_source = SiteDiscoverySource(
                http_client,
                parser_name=args.site,
                start_page=1,
                end_page=max(1, args.pages),
            )
            tasks = [DiscoveryTask(
                source=f"site:{args.site}",
                modalities=modalities,
                min_candidates=1,
            )]

        if args.browser_playbook or args.browser_fallback:
            if not args.urls and not args.site:
                raise SystemExit("--browser-fallback/--browser-playbook 当前需要配合 --urls 或 --site 使用")
            from .browser_controller import BrowserController
            from .browser_playbook import AuthorizedBrowsePlaybook
            from .site_policy import SiteCrawlPolicy

            policy = SiteCrawlPolicy.from_mapping(
                {
                    "allow_hosts": _split(args.allow_hosts),
                    "deny_hosts": _split(args.deny_hosts),
                    "storage_state_path": args.storage_state or "",
                    "session_cookies_path": args.session_cookies or "",
                    "respect_robots": bool(args.respect_robots),
                    "allow_private_hosts": args.allow_private_hosts,
                    "action_delay_seconds": 0.3,
                    "requests_per_second": max(0.2, float(args.rate) / 5.0),
                }
            )
            storage_state, cookies = policy.resolve_session()
            browser_controller = BrowserController(
                headless=True,
                cookies=cookies or None,
                storage_state=storage_state,
                allow_private_hosts=args.allow_private_hosts,
            )
            if args.browser_playbook:
                playbook = AuthorizedBrowsePlaybook(browser_controller, policy)
                browser_source = PlaybookBrowserSource(playbook, extractor)
            else:
                browser_source = BrowserPageSource(browser_controller.navigate, extractor)

        report = orchestrator.run(tasks, static_source, browser_source)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    finally:
        if browser_controller is not None:
            if args.export_storage_state:
                try:
                    browser_controller.export_storage_state(args.export_storage_state)
                except Exception as exc:
                    print(f"export_storage_state failed: {exc}")
            browser_controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
