# coding=utf-8
"""数据集爬取 CLI 入口。

用法
----
# 基本用法：爬取 1000 张猫的图片
python -m smart_spider.dataset_cli --keywords "cat" --total 1000 --output ./dataset_cats

# 多关键词
python -m smart_spider.dataset_cli --keywords "cat,dog,bird" --total 3000 --output ./dataset_animals

# 禁用 CLIP（全量采集，不做语义过滤）
python -m smart_spider.dataset_cli --keywords "landscape" --total 5000 --no-clip --output ./dataset_landscape

# 指定搜索引擎
python -m smart_spider.dataset_cli --keywords "car" --total 2000 --engines baidu,bing --output ./dataset_cars

# 站点深度爬取
python -m smart_spider.dataset_cli --keywords "illustration" --total 1000 --sites jdlingyu --output ./dataset_illust

# 断点续传
python -m smart_spider.dataset_cli --keywords "cat" --total 5000 --resume --output ./dataset_cats

# 以图搜图：关键词发现候选，查询图片做视觉二次筛选
python -m smart_spider.dataset_cli --keywords "cat" --query-image ./query.jpg \
    --image-similarity-threshold 0.75 --total 1000 --output ./dataset_cats

# 自定义分桶大小
python -m smart_spider.dataset_cli --keywords "flower" --total 2000 --batch-size 200 --output ./dataset_flowers

# 使用代理
python -m smart_spider.dataset_cli --keywords "cat" --total 1000 --proxy http://127.0.0.1:7890 --output ./dataset_cats
"""
import argparse
from typing import Optional


def _parse_scene_targets(raw: str) -> dict[str, int]:
    """Parse ``scene=count`` pairs without tying the CLI to profile aliases."""
    targets: dict[str, int] = {}
    for entry in (raw or "").split(","):
        value = entry.strip()
        if not value:
            continue
        if "=" not in value:
            raise ValueError("scene targets must use scene=count pairs")
        scene, count_text = (part.strip() for part in value.rsplit("=", 1))
        try:
            count = int(count_text)
        except ValueError as exc:
            raise ValueError(f"invalid scene target count: {count_text!r}") from exc
        if not scene or count <= 0:
            raise ValueError("scene targets need non-empty names and positive counts")
        targets[scene] = targets.get(scene, 0) + count
    if raw and not targets:
        raise ValueError("scene targets cannot be empty")
    return targets


def main(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(
        description="数据集爬取工具：基于 SmartSpider 的大规模图片采集，按每 100 张分桶存储",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 必需参数
    parser.add_argument(
        "--config", type=str, default=None,
        help="YAML/JSON 配置文件；与 CLI 参数合并（CLI 优先）",
    )
    parser.add_argument(
        "--dump-config", type=str, default=None,
        help="将合并后的配置写出到路径后退出（yaml/json）",
    )
    parser.add_argument(
        "--keywords", "-k", type=str, required=False, default=None,
        help="搜索关键词，多个用逗号分隔（如 'cat,dog,bird'）",
    )
    parser.add_argument(
        "--total", "-n", type=int, default=1000,
        help="目标总图片数量（默认 1000）",
    )
    parser.add_argument(
        "--output", "-o", type=str, default="./dataset_output",
        help="输出目录（默认 ./dataset_output）",
    )

    # CLIP 参数
    parser.add_argument(
        "--no-clip", action="store_true",
        help="禁用 CLIP 过滤（全量采集，不做语义过滤）",
    )
    parser.add_argument(
        "--similarity-threshold", type=float, default=0.22,
        help="CLIP 相似度阈值（默认 0.22，越低越宽松）",
    )
    parser.add_argument(
        "--clip-model", type=str, default="ViT-B/32",
        help="CLIP 模型名称（默认 ViT-B/32）",
    )
    parser.add_argument(
        "--query-image", type=str, default=None,
        help="查询图片路径；候选下载后按视觉相似度二次筛选",
    )
    parser.add_argument(
        "--image-similarity-threshold", type=float, default=0.75,
        help="查询图片相似度阈值（默认 0.75）",
    )
    parser.add_argument(
        "--image-output-format", choices=("jpg", "original"), default="jpg",
        help="图片落盘格式：jpg（默认）或 original（保留源格式）",
    )
    parser.add_argument(
        "--jpeg-quality", type=int, default=95,
        help="JPEG 输出质量 1-100（默认 95）",
    )

    # 搜索引擎
    parser.add_argument(
        "--engines", "-e", type=str, default=None,
        help="搜索引擎，多个用逗号分隔（如 'baidu,bing'），默认自动选择图片引擎",
    )

    # 站点深度爬取
    parser.add_argument(
        "--sites", "-s", type=str, default=None,
        help="站点解析器，多个用逗号分隔（如 'jdlingyu'）",
    )
    parser.add_argument(
        "--site-start-page", type=int, default=1,
        help="站点爬取起始页（默认 1）",
    )
    parser.add_argument(
        "--site-end-page", type=int, default=10,
        help="站点爬取结束页（默认 10）",
    )

    # 分桶参数
    parser.add_argument(
        "--batch-size", "-b", type=int, default=100,
        help="每个子目录存放的图片数量（默认 100）",
    )

    # 网络参数
    parser.add_argument(
        "--proxy", "-p", type=str, default=None,
        help="代理地址（如 http://127.0.0.1:7890）",
    )
    parser.add_argument(
        "--rate", type=float, default=8.0,
        help="请求速率（每秒请求数，默认 8.0）",
    )
    parser.add_argument(
        "--max-workers", "-w", type=int, default=20,
        help="最大并发线程数（默认 20）",
    )
    parser.add_argument(
        "--timeout", type=int, default=10,
        help="HTTP 超时秒数（默认 10）",
    )
    parser.add_argument("--connect-timeout", type=float, help="HTTP 连接超时秒数")
    parser.add_argument("--read-timeout", type=float, help="HTTP 读取超时秒数")
    parser.add_argument(
        "--max-retries", type=int, default=3,
        help="单个请求最大重试次数（默认 3；批量采集可设为 0-1 以快速跳过失效来源）",
    )

    # 图片过滤
    parser.add_argument(
        "--min-width", type=int, default=200,
        help="最小图片宽度（默认 200）",
    )
    parser.add_argument(
        "--min-height", type=int, default=200,
        help="最小图片高度（默认 200）",
    )
    parser.add_argument(
        "--min-file-size", type=int, default=1024,
        help="最小文件大小（字节，默认 1024）",
    )
    parser.add_argument(
        "--max-file-size", type=int, default=12 * 1024 * 1024,
        help="最大图片文件大小（字节，默认 12 MiB）",
    )
    parser.add_argument(
        "--max-image-pixels", type=int, default=50_000_000,
        help="解码图片最大像素数（默认 50,000,000）",
    )
    parser.add_argument("--max-inflight-pages", type=int, help="最多并发搜索结果页")
    parser.add_argument("--max-inflight-downloads", type=int, help="最多并发下载")
    parser.add_argument("--max-pending-candidates", type=int, help="最多待处理候选")
    parser.add_argument(
        "--per-domain-concurrency", type=int, default=2,
        help="每个图片域名的最大并发下载数（默认 2）",
    )
    parser.add_argument(
        "--memory-budget-mb", type=int, default=512,
        help="下载响应的内存预算 MiB（默认 512）",
    )
    parser.add_argument(
        "--scene-targets",
        help="独立场景目标，逗号分隔 scene=count，例如 '打电话=20000,吸烟=20000'",
    )
    parser.add_argument(
        "--max-source-share", type=float, default=1.0,
        help="单一发现来源最大占比 (0, 1]，默认不限制",
    )
    parser.add_argument(
        "--max-domain-share", type=float, default=1.0,
        help="单一原始图片域名最大占比 (0, 1]，默认不限制",
    )
    parser.add_argument(
        "--scene-quality-gate",
        action="store_true",
        help="启用检测器感知的场景质量门；缺失信号进入人工复核队列",
    )
    parser.add_argument(
        "--scene-review-queue",
        help="场景质量人工复核 JSONL 路径（默认 output/scene_review_queue.jsonl）",
    )
    parser.add_argument(
        "--no-curl-cffi", action="store_true",
        help="禁用 curl_cffi，使用 requests 流式下载（长时间批量任务更稳健）",
    )
    parser.add_argument(
        "--allow-private-hosts", action="store_true",
        help="允许访问内网/私有地址（仅限受控环境，默认拒绝以防 SSRF）",
    )

    # 断点续传
    parser.add_argument(
        "--resume", "-r", action="store_true",
        help="启用断点续传（从上次中断处继续）",
    )

    # 标签与任务状态
    parser.add_argument(
        "--label-mode", choices=("fixed", "discovery", "hybrid"), default="hybrid",
        help="标签模式：fixed/discovery/hybrid（默认 hybrid）",
    )
    parser.add_argument(
        "--labels", type=str, default=None,
        help="正式标签，逗号分隔；未指定时默认使用搜索关键词",
    )
    parser.add_argument(
        "--state-db", type=str, default=None,
        help="SQLite 状态库路径；默认写入输出目录",
    )
    parser.add_argument(
        "--no-state-db", action="store_true",
        help="关闭 SQLite 状态库（仅保留旧的文件进度模式）",
    )

    # spider_tools 桥接
    parser.add_argument(
        "--st-sites", type=str, default=None,
        help="spider_tools 站点，多个用逗号分隔（如 'danbooru,safebooru'）",
    )
    parser.add_argument(
        "--st-tags", type=str, default="",
        help="spider_tools 标签（Danbooru/Safebooru 用，空格分隔）",
    )
    parser.add_argument(
        "--st-query", type=str, default="",
        help="spider_tools 搜索词（Wallhaven/Unsplash 用）",
    )
    parser.add_argument(
        "--st-pages", type=str, default=None,
        help="spider_tools 页码范围（如 '1-5' 或 '1,3,5'）",
    )
    parser.add_argument(
        "--st-limit", type=int, default=0,
        help="spider_tools 每站点最大收集数量（0=不限）",
    )


    args = parser.parse_args(argv)

    from .config_io import ConfigIOError, dump_mapping, load_mapping
    from .dataset_config import DatasetCrawlConfig
    from .dataset_crawler import DatasetCrawler
    import sys as _sys

    argv_tokens = list(argv if argv is not None else _sys.argv[1:])

    def _flag_present(*names: str) -> bool:
        for tok in argv_tokens:
            for name in names:
                if tok == name or tok.startswith(name + "="):
                    return True
        return False

    cli_overrides: dict = {}
    if args.keywords:
        cli_overrides["keywords"] = [
            kw.strip() for kw in args.keywords.split(",") if kw.strip()
        ]
    if args.labels:
        cli_overrides["labels"] = [
            label.strip() for label in args.labels.split(",") if label.strip()
        ]
    if args.engines:
        cli_overrides["search_engines"] = [
            e.strip() for e in args.engines.split(",") if e.strip()
        ]
    if args.sites:
        cli_overrides["site_parsers"] = [
            s.strip() for s in args.sites.split(",") if s.strip()
        ]
    if args.proxy:
        cli_overrides["proxies"] = [args.proxy]
    try:
        scene_targets = _parse_scene_targets(args.scene_targets or "")
    except ValueError as exc:
        parser.error(str(exc))
    if _flag_present("--scene-targets"):
        cli_overrides["scene_targets"] = scene_targets
    if args.scene_quality_gate:
        cli_overrides["scene_quality_gate_enabled"] = True
    if args.scene_review_queue:
        cli_overrides["scene_review_queue_path"] = args.scene_review_queue
    if args.no_clip:
        cli_overrides["use_clip"] = False
    if args.no_curl_cffi:
        cli_overrides["use_curl_cffi"] = False
    if args.allow_private_hosts:
        cli_overrides["allow_private_hosts"] = True
    if args.resume:
        cli_overrides["resume"] = True
    if args.no_state_db:
        cli_overrides["state_db"] = ""
    elif args.state_db:
        cli_overrides["state_db"] = args.state_db
    if args.st_sites:
        cli_overrides["st_sites"] = [
            s.strip() for s in args.st_sites.split(",") if s.strip()
        ]
    if args.st_pages:
        from .spider_tools_bridge import parse_page_spec
        cli_overrides["st_pages"] = parse_page_spec(args.st_pages)

    scalar_flags = {
        ("--total", "-n"): ("total", "total_count"),
        ("--output", "-o"): ("output", "output_dir"),
        ("--batch-size", "-b"): ("batch_size", "batch_size"),
        ("--similarity-threshold",): ("similarity_threshold", "similarity_threshold"),
        ("--clip-model",): ("clip_model", "clip_model"),
        ("--query-image",): ("query_image", "query_image"),
        ("--image-similarity-threshold",): ("image_similarity_threshold", "image_similarity_threshold"),
        ("--image-output-format",): ("image_output_format", "image_output_format"),
        ("--jpeg-quality",): ("jpeg_quality", "jpeg_quality"),
        ("--rate",): ("rate", "rate"),
        ("--max-workers", "-w"): ("max_workers", "max_workers"),
        ("--timeout",): ("timeout", "timeout"),
        ("--max-retries",): ("max_retries", "max_retries"),
        ("--min-width",): ("min_width", "min_width"),
        ("--min-height",): ("min_height", "min_height"),
        ("--min-file-size",): ("min_file_size", "min_file_size"),
        ("--max-file-size",): ("max_file_size", "max_file_size"),
        ("--max-image-pixels",): ("max_image_pixels", "max_image_pixels"),
        ("--connect-timeout",): ("connect_timeout", "connect_timeout"),
        ("--read-timeout",): ("read_timeout", "read_timeout"),
        ("--max-inflight-pages",): ("max_inflight_pages", "max_inflight_pages"),
        ("--max-inflight-downloads",): ("max_inflight_downloads", "max_inflight_downloads"),
        ("--max-pending-candidates",): ("max_pending_candidates", "max_pending_candidates"),
        ("--per-domain-concurrency",): ("per_domain_concurrency", "per_domain_concurrency"),
        ("--memory-budget-mb",): ("memory_budget_mb", "memory_budget_mb"),
        ("--max-source-share",): ("max_source_share", "max_source_share"),
        ("--max-domain-share",): ("max_domain_share", "max_domain_share"),
        ("--site-start-page",): ("site_start_page", "site_start_page"),
        ("--site-end-page",): ("site_end_page", "site_end_page"),
        ("--label-mode",): ("label_mode", "label_mode"),
        ("--st-tags",): ("st_tags", "st_tags"),
        ("--st-query",): ("st_query", "st_query"),
        ("--st-limit",): ("st_limit", "st_limit_per_site"),
    }
    apply_all_defaults = args.config is None
    for flags, (attr, field) in scalar_flags.items():
        if apply_all_defaults or _flag_present(*flags):
            value = getattr(args, attr)
            if value is not None:
                cli_overrides[field] = value

    try:
        merged = load_mapping(args.config, overrides=cli_overrides, use_env=True)
        if not merged.get("keywords"):
            parser.error("--keywords is required (or provide it in --config)")
        config = DatasetCrawlConfig.from_mapping(merged)
    except (ValueError, ConfigIOError, TypeError) as exc:
        parser.error(str(exc))

    if args.dump_config:
        try:
            dump_mapping(config.to_dict(), args.dump_config)
        except ConfigIOError as exc:
            parser.error(str(exc))
        return

    crawler = DatasetCrawler.from_config(config)
    crawler.crawl()


if __name__ == "__main__":
    main()
