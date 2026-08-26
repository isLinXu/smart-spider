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
    parser.add_argument("--no-materialize-images", action="store_true")
    parser.add_argument("--browser-fallback", action="store_true", help="URL/站点页面静态失败时启用 Playwright")
    parser.add_argument("--proxy", action="append", default=[])
    parser.add_argument("--rate", type=float, default=5.0)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--max-retries", type=int, default=3)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
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
        quality_report_path=args.quality_report,
        materialize_images=not args.no_materialize_images,
        allowed_modalities=modalities,
        label_policy=policy,
    )
    http_client = SmartHttpClient(
        proxies=args.proxy,
        rate=args.rate,
        timeout=args.timeout,
        max_retries=args.max_retries,
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

        if args.browser_fallback:
            if not args.urls and not args.site:
                raise SystemExit("--browser-fallback 当前需要配合 --urls 或 --site 使用")
            from .browser_controller import BrowserController
            browser_controller = BrowserController(headless=True)
            browser_source = BrowserPageSource(browser_controller.navigate, extractor)

        report = orchestrator.run(tasks, static_source, browser_source)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    finally:
        if browser_controller is not None:
            browser_controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
