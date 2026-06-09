# coding=utf-8
"""站点深度爬取编排器。

架构定位
--------
SiteCrawler 是 SmartSpider 框架中「站点深度爬取模式」的核心编排器，
与 SmartSpider（搜索引擎模式）平级，共享以下基础设施:

- SmartHttpClient : UA随机化、代理轮换、限速、退避重试、TLS指纹伪造
- UrlDeduplicator : URL + 内容双重去重（BloomFilter 优先）
- CrawlStats      : 线程安全采集统计
- CrawlEvent      : 回调钩子系统

数据流
------
列表页(N页) ──collect_pages()──▶ 详情页列表(M篇)
                                        │
                              extract_images() + get_detail_pagination()
                                        │
                              图片 URL 列表 ──▶ 下载保存

与 SmartSpider 的区别
--------------------
| 维度       | SmartSpider (搜索引擎模式) | SiteCrawler (站点深度模式) |
|-----------|------------------------|------------------------|
| 数据源     | 搜索引擎结果页           | 目标站点列表页           |
| 爬取策略   | 广度优先（关键词×引擎）   | 深度优先（列表→详情→资源）|
| URL 发现   | 搜索引擎 API/HTML        | SiteParser 解析          |
| CLIP 过滤  | ✓（图片模态）            | ✗（站点图片通常已精准）   |
| 适用场景   | 泛主题采集               | 定向站点全量采集          |

用法
----
>>> from smart_spider.site_crawler import SiteCrawler
>>> from smart_spider.site_parser import get_site_parser
>>>
>>> crawler = SiteCrawler(
...     site_parser=get_site_parser("jdlingyu"),
...     output_dir="./output_jdlingyu",
...     start_page=1,
...     end_page=5,
... )
>>> crawler.crawl()
"""

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional

import numpy as np
from loguru import logger
from PIL import Image
from tqdm import tqdm

from .http_client import SmartHttpClient
from .site_parser import SiteParser
from .smart_spider import CrawlEvent, CrawlStats, UrlDeduplicator, _detect_ext


class SiteCrawler:
    """站点深度爬取编排器。

    两级爬取流程:
    1. 列表页 → SiteParser.collect_pages() → 收集详情页链接
    2. 详情页 → SiteParser.extract_images() → 解析图片 URL → 下载保存

    特性:
    - 复用 SmartHttpClient（UA随机化、代理轮换、限速、退避重试）
    - URL 去重（BloomFilter 优先）
    - 内容去重（MD5）
    - 低方差过滤（空白/纯色图）
    - 断点续采
    - 优雅关停（SIGINT/SIGTERM）
    - 回调钩子（CrawlEvent）
    """

    def __init__(
        self,
        site_parser: SiteParser,
        output_dir: str = "./output_site",
        start_page: int = 1,
        end_page: int = 5,
        proxies: Optional[list[str]] = None,
        rate: float = 5.0,
        max_retries: int = 3,
        timeout: int = 15,
        max_workers: int = 8,
        resume: bool = False,
        min_width: int = 200,
        min_height: int = 200,
        callbacks: Optional[list] = None,
    ):
        self.parser = site_parser
        self.output_dir = output_dir
        self.start_page = start_page
        self.end_page = end_page
        self.max_workers = max_workers
        self.min_width = min_width
        self.min_height = min_height
        self._callbacks = callbacks or []

        # HTTP 客户端（禁用 curl_cffi，因其 Response 不兼容 apparent_encoding）
        self._http = SmartHttpClient(
            proxies=proxies or [],
            rate=rate,
            max_retries=max_retries,
            timeout=timeout,
            use_curl_cffi=False,
        )

        # URL 去重
        _dedup_path = None
        if resume:
            os.makedirs(output_dir, exist_ok=True)
            _dedup_path = os.path.join(output_dir, ".seen_urls.jsonl")
        self._dedup = UrlDeduplicator(
            persist_path=_dedup_path,
            content_dedup=True,
        )

        # 采集统计
        self.stats = CrawlStats()

        # 内部计数器（兼容旧统计接口）
        self._counter = {
            "articles_found": 0,
            "images_found": 0,
            "images_saved": 0,
            "images_filtered": 0,
            "images_failed": 0,
        }
        self._counter_lock = threading.Lock()

        # 优雅关停
        self._stop = threading.Event()
        import signal as _sig
        self._orig_sigint = _sig.getsignal(_sig.SIGINT)
        self._orig_sigterm = _sig.getsignal(_sig.SIGTERM)

        def _handler(s, f):
            logger.warning("收到中断信号，正在优雅停止...")
            self._stop.set()

        _sig.signal(_sig.SIGINT, _handler)
        _sig.signal(_sig.SIGTERM, _handler)

    def _inc_counter(self, key: str, n: int = 1):
        with self._counter_lock:
            self._counter[key] = self._counter.get(key, 0) + n

    def _emit_callback(self, event: CrawlEvent) -> None:
        """安全触发所有回调。"""
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    # ── 第一级: 收集详情页链接 ──────────────────────────────────────

    def collect_articles(self) -> list[dict]:
        """遍历列表页，收集所有详情页链接。"""
        articles = []
        parser = self.parser
        logger.info(f"[{parser.name}] 开始收集详情页链接: 第 {self.start_page} ~ {self.end_page} 页")

        for page_num in range(self.start_page, self.end_page + 1):
            if self._stop.is_set():
                break

            url = parser.build_listing_url(page_num)
            logger.info(f"[{parser.name}] 正在获取列表页: {url}")

            try:
                resp = self._http.get(
                    url,
                    extra_headers={"Referer": parser.get_listing_referer()},
                )
                resp.encoding = resp.apparent_encoding or "utf-8"
                html = resp.text
                items = parser.collect_pages(html)
                if not items:
                    logger.warning(f"列表页 {page_num} 未找到详情页链接，可能已到达末尾")
                    break
                articles.extend(items)
                self._inc_counter("articles_found", len(items))
                logger.info(f"列表页 {page_num}: 发现 {len(items)} 篇文章")
            except Exception as e:
                logger.error(f"获取列表页 {page_num} 失败: {e}")

        # 去重
        seen_ids = set()
        unique = []
        for art in articles:
            aid = art.get("id", art["url"])
            if aid not in seen_ids:
                seen_ids.add(aid)
                unique.append(art)

        logger.info(f"[{parser.name}] 共收集 {len(unique)} 篇唯一文章")
        return unique

    # ── 第二级: 解析详情页图片 ──────────────────────────────────────

    def fetch_article_images(self, article: dict) -> list[str]:
        """获取一篇文章的所有图片 URL（含多页详情）。"""
        parser = self.parser
        article_url = article["url"]

        if self._dedup.is_seen(article_url):
            logger.debug(f"文章已处理，跳过: {article_url}")
            return []

        all_image_urls = []

        try:
            resp = self._http.get(
                article_url,
                extra_headers={"Referer": parser.get_detail_referer()},
            )
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
        except Exception as e:
            logger.error(f"获取详情页失败 {article_url}: {e}")
            return []

        # 解析第一页图片
        page_images = parser.extract_images(html)
        all_image_urls.extend(page_images)

        # 解析分页总数
        total_pages = parser.get_detail_pagination(html)

        # 如果有多页，逐页获取
        if total_pages > 1:
            for pn in range(2, total_pages + 1):
                if self._stop.is_set():
                    break
                page_url = parser.build_detail_page_url(article_url, pn)
                try:
                    resp = self._http.get(
                        page_url,
                        extra_headers={"Referer": article_url},
                    )
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    page_html = resp.text
                    page_imgs = parser.extract_images(page_html)
                    all_image_urls.extend(page_imgs)
                except Exception as e:
                    logger.warning(f"获取详情分页 {page_url} 失败: {e}")

        article_id = article.get("id", "?")
        title = article.get("title", article_id)
        logger.info(
            f"[{parser.name}] 文章 [{title[:30]}]: "
            f"{total_pages} 页, {len(all_image_urls)} 张图片"
        )
        self._inc_counter("images_found", len(all_image_urls))

        return all_image_urls

    # ── 下载图片 ──────────────────────────────────────────────────

    def download_image(self, url: str, save_dir: str) -> bool:
        """下载单张图片，带尺寸过滤和方差过滤。"""
        parser = self.parser

        if self._dedup.is_seen(url):
            return False

        try:
            # 直接下载完整内容（webp 等格式的头部信息不足以做尺寸预检）
            resp = self._http.get(
                url,
                extra_headers={"Referer": parser.get_download_referer()},
                stream=True,
            )
            try:
                full_content = b"".join(resp.iter_content(chunk_size=65536))
            finally:
                resp.close()

            if len(full_content) < 1024:
                self._inc_counter("images_filtered")
                self.stats.inc_filtered("image")
                return False

            # 格式检测
            ext = _detect_ext(full_content)

            # 解码完整图片
            buf = BytesIO(full_content)
            img = Image.open(buf)
            img.verify()
            buf.seek(0)
            img = Image.open(buf).convert("RGB")

            # 尺寸过滤
            if img.width < self.min_width or img.height < self.min_height:
                self._inc_counter("images_filtered")
                self.stats.inc_filtered("image")
                self._emit_callback(CrawlEvent(
                    event_type="item_filtered", keyword="",
                    media_type="image", url=url,
                    detail={"reason": "small_size"},
                ))
                return False

            # 低方差过滤（空白/纯色图）
            arr = np.array(img.resize((32, 32)))
            if np.std(arr) < 10:
                self._inc_counter("images_filtered")
                self.stats.inc_filtered("image")
                self._emit_callback(CrawlEvent(
                    event_type="item_filtered", keyword="",
                    media_type="image", url=url,
                    detail={"reason": "low_variance"},
                ))
                return False

            # 内容去重
            if self._dedup.is_content_seen(full_content):
                return False

            # 保存原始格式
            name = hashlib.md5(url.encode()).hexdigest()
            fp = os.path.join(save_dir, f"{name}{ext}")
            with open(fp, "wb") as f:
                f.write(full_content)

            self._inc_counter("images_saved")
            self.stats.inc_saved("image")
            self._emit_callback(CrawlEvent(
                event_type="item_saved", keyword="",
                media_type="image", url=url,
                detail={"file": fp},
            ))
            return True

        except Exception as e:
            logger.warning(f"下载图片失败 {url[:60]}: {e}")
            self._inc_counter("images_failed")
            self.stats.inc_failed("image")
            self._emit_callback(CrawlEvent(
                event_type="item_failed", keyword="",
                media_type="image", url=url,
                detail={"error": str(e)},
            ))
            return False

    # ── 主流程 ────────────────────────────────────────────────────

    def crawl(self):
        """执行完整爬取流程。"""
        t0 = time.monotonic()

        # 第一级: 收集文章
        articles = self.collect_articles()
        if not articles:
            logger.warning("未找到任何文章，退出")
            return

        # 第二级: 解析图片 + 下载
        logger.info(f"[{self.parser.name}] 开始处理 {len(articles)} 篇文章的图片...")

        os.makedirs(self.output_dir, exist_ok=True)

        pbar = tqdm(total=len(articles), desc=f"[{self.parser.name}]", unit="篇")

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {}

            for art in articles:
                if self._stop.is_set():
                    break
                future = pool.submit(self._process_article, art)
                futures[future] = art

            for future in as_completed(futures):
                art = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"处理文章出错 [{art.get('title', '')[:30]}]: {e}")
                pbar.update(1)

        pbar.close()

        # 清理
        self._dedup.close()

        # 恢复信号处理器
        import signal as _sig
        _sig.signal(_sig.SIGINT, self._orig_sigint)
        _sig.signal(_sig.SIGTERM, self._orig_sigterm)

        elapsed = time.monotonic() - t0
        c = self._counter
        logger.info("=" * 60)
        logger.info(f"[{self.parser.name}] 爬取完成! 耗时 {elapsed:.1f}s")
        logger.info(f"  文章发现: {c['articles_found']}")
        logger.info(f"  图片发现: {c['images_found']}")
        logger.info(f"  图片保存: {c['images_saved']}")
        logger.info(f"  图片过滤: {c['images_filtered']}")
        logger.info(f"  图片失败: {c['images_failed']}")
        logger.info(f"  输出目录: {os.path.abspath(self.output_dir)}")

        # 生成采集报告
        self.stats.end_time = time.monotonic()
        report = self.stats.summary()
        report["site"] = self.parser.name
        report["counter"] = c
        report["shutdown"] = self._stop.is_set()
        logger.info(f"Crawl stats: {report}")

        report_path = os.path.join(self.output_dir, "_crawl_report.json")
        try:
            import json
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info(f"Crawl report saved to {report_path}")
        except Exception as e:
            logger.warning(f"Failed to save crawl report: {e}")

        self._emit_callback(CrawlEvent(
            event_type="crawl_done", keyword="",
            media_type="image", detail=report,
        ))

    def _process_article(self, article: dict):
        """处理单篇文章: 解析图片 → 下载保存。"""
        if self._stop.is_set():
            return

        article_id = article.get("id", "?")
        title = article.get("title", article_id)
        # 清理标题中的非法文件名字符
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:50]
        save_dir = os.path.join(self.output_dir, f"{article_id}_{safe_title}")

        # 解析图片 URL
        image_urls = self.fetch_article_images(article)
        if not image_urls:
            return

        os.makedirs(save_dir, exist_ok=True)

        # 逐张下载
        for img_url in image_urls:
            if self._stop.is_set():
                break
            self.download_image(img_url, save_dir)
