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
import sys


def main():
    parser = argparse.ArgumentParser(
        description="数据集爬取工具：基于 SmartSpider 的大规模图片采集，按每 100 张分桶存储",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 必需参数
    parser.add_argument(
        "--keywords", "-k", type=str, required=True,
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

    args = parser.parse_args()

    # 解析参数
    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    labels = [label.strip() for label in args.labels.split(",") if label.strip()] if args.labels else None
    engines = [e.strip() for e in args.engines.split(",") if e.strip()] if args.engines else None
    sites = [s.strip() for s in args.sites.split(",") if s.strip()] if args.sites else None
    proxies = [args.proxy] if args.proxy else None

    # spider_tools 参数
    st_sites = [s.strip() for s in args.st_sites.split(",") if s.strip()] if args.st_sites else None
    st_pages = None
    if args.st_pages:
        from .spider_tools_bridge import parse_page_spec
        st_pages = parse_page_spec(args.st_pages)

    # 导入并运行
    from .dataset_crawler import DatasetCrawler

    crawler = DatasetCrawler(
        keywords=keywords,
        total_count=args.total,
        output_dir=args.output,
        batch_size=args.batch_size,
        use_clip=not args.no_clip,
        similarity_threshold=args.similarity_threshold,
        clip_model=args.clip_model,
        query_image=args.query_image,
        image_similarity_threshold=args.image_similarity_threshold,
        image_output_format=args.image_output_format,
        jpeg_quality=args.jpeg_quality,
        proxies=proxies,
        rate=args.rate,
        max_workers=args.max_workers,
        timeout=args.timeout,
        max_retries=args.max_retries,
        min_width=args.min_width,
        min_height=args.min_height,
        min_file_size=args.min_file_size,
        search_engines=engines,
        site_parsers=sites,
        site_start_page=args.site_start_page,
        site_end_page=args.site_end_page,
        st_sites=st_sites,
        st_tags=args.st_tags,
        st_query=args.st_query,
        st_pages=st_pages,
        st_limit_per_site=args.st_limit,
        resume=args.resume,
        label_mode=args.label_mode,
        labels=labels,
        state_db="" if args.no_state_db else args.state_db,
    )

    crawler.crawl()


if __name__ == "__main__":
    main()
