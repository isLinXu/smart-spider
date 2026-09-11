#!/usr/bin/env python3
# coding=utf-8
"""SmartSpider CLI 入口 — 多模态智能爬虫。

快速使用
--------
# 搜索引擎模式：采集图片（默认）
python spider.py --keywords 猫咪 --max_items 100

# 同时采集图片 + 视频 + 文本
python spider.py --keywords 猫咪 --media_types image video text

# 使用代理 + 限速
python spider.py --keywords 猫咪 --proxies http://127.0.0.1:7890 --rate 5

# 启用 Playwright 动态渲染（处理反爬 SPA 页面）
python spider.py --keywords 猫咪 --use_browser --search_engines xiaohongshu weixin

# 站点深度爬取模式：爬取指定站点的全量内容
python spider.py --site jdlingyu --start_page 1 --end_page 5
python spider.py --site jdlingyu --end_page 10 --output_dir ./output_jdlingyu
"""
import argparse

from smart_spider import SmartSpider


def main():
    parser = argparse.ArgumentParser(
        description="SmartSpider - 多模态智能爬虫（CLIP + 反爬 + Playwright + 站点深度爬取）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 基础参数
    g_basic = parser.add_argument_group("基础参数")
    g_basic.add_argument("--keywords", nargs="+", required=False, help="关键词列表（搜索引擎模式必需）")
    g_basic.add_argument("--site", type=str, default=None,
                         help="站点深度爬取模式：指定站点解析器名称（如 jdlingyu）。"
                              "使用此参数后将进入站点深度爬取模式，忽略 --keywords")
    g_basic.add_argument("--max_items", type=int, default=50,
                         help="每个关键词每种模态的最大采集数量（默认 50）")
    g_basic.add_argument("--media_types", nargs="+", default=["image"],
                         choices=["image", "video", "text"],
                         help="采集模态，可多选：image video text（默认 image）")
    g_basic.add_argument("--output_dir", type=str, default="./output",
                         help="输出根目录（默认 ./output）")

    # 引擎参数
    g_engine = parser.add_argument_group("引擎参数")
    g_engine.add_argument("--search_engines", nargs="*", default=None,
                          help="手动指定引擎列表（默认自动按模态选取）。"
                               "可选: baidu bing sogou 360 bilibili bing_video "
                               "baidu_text bing_text weixin xiaohongshu")

    # CLIP 参数（图片模态）
    g_clip = parser.add_argument_group("CLIP 过滤参数（image 模态）")
    g_clip.add_argument("--similarity_threshold", type=float, default=0.20,
                        help="CLIP 余弦相似度阈值（0~1，默认 0.20）")
    g_clip.add_argument("--batch_size", type=int, default=16,
                        help="CLIP 推理批大小（默认 16，GPU 可调大）")

    # 网络参数
    g_net = parser.add_argument_group("网络参数")
    g_net.add_argument("--timeout", type=int, default=10, help="请求超时秒数（默认 10）")
    g_net.add_argument("--max_workers", type=int, default=20,
                       help="下载线程池大小（默认 20）")
    g_net.add_argument("--proxies", nargs="*", default=[],
                       help="代理列表，支持 http:// 和 socks5://，可传多个")
    g_net.add_argument("--rate", type=float, default=8.0,
                       help="每秒最大请求数（令牌桶限速，默认 8.0）")
    g_net.add_argument("--max_retries", type=int, default=3,
                       help="遭遇 429/503/超时时的最大重试次数（默认 3）")

    # 动态渲染参数
    g_browser = parser.add_argument_group("Playwright 动态渲染参数")
    g_browser.add_argument("--use_browser", action="store_true",
                           help="启用 Playwright 无头浏览器渲染（处理 JS 反爬）")
    g_browser.add_argument("--no_headless", action="store_true",
                           help="显示浏览器窗口（调试用，需配合 --use_browser）")
    g_browser.add_argument("--browser_proxy", type=str, default=None,
                           help="浏览器专用代理（默认同 --proxies 第一个）")
    g_browser.add_argument("--allow-private-hosts", action="store_true",
                           help="允许访问受控内网地址（默认拒绝，以防 SSRF）")

    # 视频参数
    g_video = parser.add_argument_group("视频参数（video 模态）")
    g_video.add_argument("--video_format",
                         default="bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                         help="yt-dlp 格式选择字符串")
    g_video.add_argument("--video_max_size", default="500m",
                         help="yt-dlp 单文件最大体积（默认 500m）")
    g_video.add_argument("--cookies_file", type=str, default=None,
                         help="yt-dlp cookies.txt 文件路径（用于需要登录的平台）")

    # 断点续采
    g_resume = parser.add_argument_group("断点续采")
    g_resume.add_argument("--resume", action="store_true",
                          help="开启断点续采：从 output_dir/.seen_urls.jsonl 恢复去重状态，"
                               "跳过上次已采集的 URL")

    # 站点深度爬取参数
    g_site = parser.add_argument_group("站点深度爬取参数（--site 模式）")
    g_site.add_argument("--start_page", type=int, default=1,
                        help="起始列表页（默认 1）")
    g_site.add_argument("--end_page", type=int, default=5,
                        help="结束列表页（默认 5）")
    g_site.add_argument("--min_width", type=int, default=200,
                        help="最小图片宽度（像素，默认 200）")
    g_site.add_argument("--min_height", type=int, default=200,
                        help="最小图片高度（像素，默认 200）")
    g_site.add_argument("--list_sites", action="store_true",
                        help="列出所有可用的站点解析器")

    # 高级参数
    g_adv = parser.add_argument_group("高级参数")
    g_adv.add_argument("--clip_model", type=str, default="ViT-B/32",
                       help="CLIP 模型名称（默认 ViT-B/32，可选 ViT-B/16, ViT-L/14 等）")
    g_adv.add_argument("--disk_guard_mb", type=int, default=500,
                       help="磁盘剩余空间警戒值 MB（低于此值暂停采集，默认 500）")
    g_adv.add_argument("--no_content_dedup", action="store_true",
                       help="禁用内容哈希去重（默认启用，防止不同 URL 相同内容重复保存）")
    g_adv.add_argument("--video_concurrency", type=int, default=3,
                       help="视频并行下载数（默认 3）")
    g_adv.add_argument("--check_engines", action="store_true",
                       help="仅运行引擎健康检查，不执行采集。输出各引擎可用性和延迟")

    args = parser.parse_args()

    # 列出可用站点解析器
    if args.list_sites:
        from smart_spider import SITE_PARSER_REGISTRY
        if not SITE_PARSER_REGISTRY:
            print("No site parsers registered.")
        else:
            print(f"Available site parsers ({len(SITE_PARSER_REGISTRY)}):")
            for name, p in sorted(SITE_PARSER_REGISTRY.items()):
                print(f"  {name:<16} {p.__class__.__name__}  base={p.base_url}")
        return

    # 站点深度爬取模式
    if args.site:
        from smart_spider import get_site_parser, SiteCrawler
        try:
            site_parser = get_site_parser(args.site)
        except KeyError as e:
            print(f"Error: {e}")
            return
        crawler = SiteCrawler(
            site_parser=site_parser,
            output_dir=args.output_dir,
            start_page=args.start_page,
            end_page=args.end_page,
            proxies=args.proxies or [],
            rate=args.rate,
            max_retries=args.max_retries,
            timeout=args.timeout,
            max_workers=args.max_workers,
            resume=args.resume,
            min_width=args.min_width,
            min_height=args.min_height,
        )
        crawler.crawl()
        return

    # 搜索引擎模式：必须有 --keywords
    if not args.keywords:
        parser.error("--keywords is required in search engine mode (or use --site for site crawling)")

    # 引擎健康检查模式
    if args.check_engines:
        from smart_spider.engines import check_all_engines
        from smart_spider.http_client import SmartHttpClient
        client = SmartHttpClient(
            proxies=args.proxies or [],
            rate=args.rate,
            max_retries=1,
            timeout=5,
        )
        results = check_all_engines(keyword=args.keywords[0], http_client=client)
        print(f"\n{'引擎':<16} {'状态':<6} {'条数':<6} {'延迟ms':<10} {'错误'}")
        print("-" * 60)
        for r in results:
            status = "✓" if r["ok"] else "✗"
            print(f"{r['engine']:<16} {status:<6} {r['item_count']:<6} "
                  f"{r['latency_ms']:<10.1f} {r['error']}")
        ok_count = sum(1 for r in results if r["ok"])
        print(f"\n{ok_count}/{len(results)} engines available")
        return

    spider = SmartSpider(
        keywords=args.keywords,
        max_items=args.max_items,
        media_types=args.media_types,
        similarity_threshold=args.similarity_threshold,
        timeout=args.timeout,
        max_workers=args.max_workers,
        batch_size=args.batch_size,
        search_engines=args.search_engines,
        output_dir=args.output_dir,
        proxies=args.proxies or [],
        rate=args.rate,
        max_retries=args.max_retries,
        use_browser=args.use_browser,
        headless=not args.no_headless,
        browser_proxy=args.browser_proxy,
        allow_private_hosts=args.allow_private_hosts,
        video_format=args.video_format,
        video_max_size=args.video_max_size,
        cookies_file=args.cookies_file,
        resume=args.resume,
        clip_model=args.clip_model,
        disk_guard_mb=args.disk_guard_mb,
        content_dedup=not args.no_content_dedup,
        video_concurrency=args.video_concurrency,
    )
    spider.download()


if __name__ == "__main__":
    main()
