#!/usr/bin/env python3
# coding=utf-8
"""绝对领域 (jdlingyu.com) 妹子图专区爬取脚本。

网站结构
--------
1. 列表页: https://www.jdlingyu.com/collection/meizitu/page/{n}
   - 共 184 页，2202 篇文章
   - 每页约 12 个图集卡片，卡片链接格式: /{id}.html

2. 详情页: https://www.jdlingyu.com/{id}.html[/page/{n}]
   - 图片使用懒加载: <img class="lazy" data-src="https://img.jdlingyu.com/...">
   - 未加载时 src 为占位图 (default-img.jpg)
   - 多页文章有分页: /{id}.html/2, /{id}.html/3 ...

3. 图片 CDN: img.jdlingyu.com — webp 格式

用法
----
# 默认爬取前 5 页（约 60 个图集）
python crawl_jdlingyu.py

# 指定页数范围
python crawl_jdlingyu.py --start_page 1 --end_page 10

# 指定输出目录和并发数
python crawl_jdlingyu.py --output_dir ./jdlingyu_images --max_workers 10

# 使用代理
python crawl_jdlingyu.py --proxies http://127.0.0.1:7890

# 降低请求速率（礼貌爬取）
python crawl_jdlingyu.py --rate 3

# 断点续采
python crawl_jdlingyu.py --resume
"""

import argparse
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from typing import Optional

import numpy as np
import requests
from loguru import logger
from PIL import Image
from tqdm import tqdm

from smart_spider import SmartHttpClient, UrlDeduplicator


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.jdlingyu.com"
COLLECTION_URL = f"{BASE_URL}/collection/meizitu"

# 图片格式魔数
_MAGIC_MAP = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG": ".png",
    b"RIFF": ".webp",
    b"GIF8": ".gif",
}


def _detect_ext(content: bytes) -> str:
    for magic, ext in _MAGIC_MAP.items():
        if content[: len(magic)] == magic:
            if ext == ".webp" and content[8:12] != b"WEBP":
                continue
            return ext
    return ".jpg"


# ──────────────────────────────────────────────────────────────────────────────
# 解析函数
# ──────────────────────────────────────────────────────────────────────────────

def parse_article_links(html: str) -> list[dict]:
    """从列表页 HTML 解析文章链接。

    Returns:
        [{"url": "https://www.jdlingyu.com/149524.html",
          "title": "...", "meta": {...}}]
    """
    items = []
    # 匹配文章链接: /数字ID.html
    seen = set()
    for m in re.finditer(
        r'href="(https://www\.jdlingyu\.com/(\d+)\.html)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        url = m.group(1)
        aid = m.group(2)
        title = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if aid in seen:
            continue
        seen.add(aid)
        items.append({"url": url, "title": title, "id": aid})

    return items


def parse_detail_images(html: str) -> list[str]:
    """从详情页 HTML 解析图片真实 URL。

    jdlingyu 使用懒加载:
      <img class="lazy" data-src="https://img.jdlingyu.com/i/..." />
    未加载时 src 是占位图，真实 URL 在 data-src 属性中。
    """
    urls = []

    # 方式1: data-src 属性（懒加载）
    for m in re.finditer(r'data-src="(https://img\.jdlingyu\.com/[^"]+)"', html):
        urls.append(m.group(1))

    # 方式2: src 属性中已经是真实 URL（已加载的图片）
    for m in re.finditer(r'src="(https://img\.jdlingyu\.com/[^"]+)"', html):
        url = m.group(1)
        if url not in urls:
            urls.append(url)

    # 方式3: 其他 CDN 域名的图片（排除内网地址和占位图）
    for m in re.finditer(r'(?:data-src|src)="(https?://[^"]*\.(?:jpg|jpeg|png|webp|gif)[^"]*)"', html, re.IGNORECASE):
        url = m.group(1)
        if (url not in urls
                and "default-img" not in url
                and "avatar" not in url
                and not url.startswith(("http://192.", "http://10.", "http://172."))):
            urls.append(url)

    return urls


def parse_detail_pagination(html: str) -> int:
    """解析详情页的分页总数。

    分页格式: <a href="/149524.html/2">2</a> ... <span>3 页</span>
    """
    # 匹配 "N 页" 或 "共N页"
    m = re.search(r'(\d+)\s*页', html)
    if m:
        return int(m.group(1))

    # 匹配分页链接中的最大页码
    pages = re.findall(r'/\d+\.html/(\d+)', html)
    if pages:
        return max(int(p) for p in pages)

    return 1


# ──────────────────────────────────────────────────────────────────────────────
# 爬取器
# ──────────────────────────────────────────────────────────────────────────────

class JDLingyuCrawler:
    """绝对领域妹子图爬取器。

    两级爬取流程:
    1. 列表页 → 收集文章链接
    2. 详情页 → 解析图片 URL → 下载保存

    特性:
    - 复用 SmartHttpClient（UA随机化、代理轮换、限速、退避重试）
    - URL 去重（BloomFilter 优先）
    - 内容去重（MD5）
    - 低方差过滤（空白/纯色图）
    - 断点续采
    """

    def __init__(
        self,
        output_dir: str = "./output_jdlingyu",
        proxies: Optional[list[str]] = None,
        rate: float = 5.0,
        max_retries: int = 3,
        timeout: int = 15,
        max_workers: int = 8,
        resume: bool = False,
        min_width: int = 200,
        min_height: int = 200,
    ):
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.min_width = min_width
        self.min_height = min_height

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

        # 统计
        self._stats = {
            "articles_found": 0,
            "images_found": 0,
            "images_saved": 0,
            "images_filtered": 0,
            "images_failed": 0,
        }
        self._stats_lock = threading.Lock()

        # 优雅关停
        self._stop = threading.Event()
        import signal as _sig
        _orig = _sig.getsignal(_sig.SIGINT)
        def _handler(s, f):
            logger.warning("收到中断信号，正在优雅停止...")
            self._stop.set()
        _sig.signal(_sig.SIGINT, _handler)

    def _inc_stat(self, key: str, n: int = 1):
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + n

    # ── 第一级: 收集文章链接 ──────────────────────────────────────

    def collect_articles(self, start_page: int = 1, end_page: int = 5) -> list[dict]:
        """遍历列表页，收集所有文章链接。"""
        articles = []
        logger.info(f"开始收集文章链接: 第 {start_page} ~ {end_page} 页")

        for page_num in range(start_page, end_page + 1):
            if self._stop.is_set():
                break

            if page_num == 1:
                url = COLLECTION_URL
            else:
                url = f"{COLLECTION_URL}/page/{page_num}"

            logger.info(f"正在获取列表页: {url}")
            try:
                resp = self._http.get(url, engine="bing",
                                     extra_headers={"Referer": BASE_URL + "/"})
                resp.encoding = resp.apparent_encoding or "utf-8"
                html = resp.text
                items = parse_article_links(html)
                if not items:
                    logger.warning(f"列表页 {page_num} 未找到文章链接，可能已到达末尾")
                    break
                articles.extend(items)
                self._inc_stat("articles_found", len(items))
                logger.info(f"列表页 {page_num}: 发现 {len(items)} 篇文章")
            except Exception as e:
                logger.error(f"获取列表页 {page_num} 失败: {e}")

        # 去重
        seen_ids = set()
        unique = []
        for art in articles:
            if art["id"] not in seen_ids:
                seen_ids.add(art["id"])
                unique.append(art)

        logger.info(f"共收集 {len(unique)} 篇唯一文章")
        return unique

    # ── 第二级: 解析详情页图片 ──────────────────────────────────────

    def fetch_article_images(self, article: dict) -> list[str]:
        """获取一篇文章的所有图片 URL（含多页详情）。"""
        article_url = article["url"]
        article_id = article["id"]

        if self._dedup.is_seen(article_url):
            logger.debug(f"文章已处理，跳过: {article_url}")
            return []

        all_image_urls = []

        try:
            resp = self._http.get(article_url, engine="bing",
                                  extra_headers={"Referer": COLLECTION_URL})
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
        except Exception as e:
            logger.error(f"获取详情页失败 {article_url}: {e}")
            return []

        # 解析第一页图片
        page_images = parse_detail_images(html)
        all_image_urls.extend(page_images)

        # 解析分页总数
        total_pages = parse_detail_pagination(html)

        # 如果有多页，逐页获取
        if total_pages > 1:
            for pn in range(2, total_pages + 1):
                if self._stop.is_set():
                    break
                page_url = f"{article_url}/{pn}"
                try:
                    resp = self._http.get(page_url, engine="bing",
                                         extra_headers={"Referer": article_url})
                    resp.encoding = resp.apparent_encoding or "utf-8"
                    page_html = resp.text
                    page_imgs = parse_detail_images(page_html)
                    all_image_urls.extend(page_imgs)
                except Exception as e:
                    logger.warning(f"获取详情分页 {page_url} 失败: {e}")

        logger.info(f"文章 [{article.get('title', article_id)[:30]}]: "
                    f"{total_pages} 页, {len(all_image_urls)} 张图片")
        self._inc_stat("images_found", len(all_image_urls))

        return all_image_urls

    # ── 下载图片 ──────────────────────────────────────────────────

    def download_image(self, url: str, save_dir: str) -> bool:
        """下载单张图片，带尺寸过滤和方差过滤。

        注意: jdlingyu 的图片多为 webp 格式，peek（8KB）不足以解码，
        因此直接下载完整内容再做验证，而非先 peek 再读 body。
        """
        if self._dedup.is_seen(url):
            return False

        try:
            # 直接下载完整内容（webp 等格式的头部信息不足以做尺寸预检）
            # 加 Referer 绕过防盗链
            resp = self._http.get(
                url, engine="bing",
                extra_headers={"Referer": "https://www.jdlingyu.com/"},
                stream=True,
            )
            try:
                full_content = b"".join(resp.iter_content(chunk_size=65536))
            finally:
                resp.close()

            if len(full_content) < 1024:
                # 太小，可能是错误页或图标
                self._inc_stat("images_filtered")
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
                self._inc_stat("images_filtered")
                return False

            # 低方差过滤（空白/纯色图）
            arr = np.array(img.resize((32, 32)))
            if np.std(arr) < 10:
                self._inc_stat("images_filtered")
                return False

            # 内容去重
            if self._dedup.is_content_seen(full_content):
                return False

            # 保存原始格式（不做 convert 保存，保留原始质量）
            name = hashlib.md5(url.encode()).hexdigest()
            fp = os.path.join(save_dir, f"{name}{ext}")
            with open(fp, "wb") as f:
                f.write(full_content)

            self._inc_stat("images_saved")
            return True

        except Exception as e:
            logger.warning(f"下载图片失败 {url[:60]}: {e}")
            self._inc_stat("images_failed")
            return False

    # ── 主流程 ────────────────────────────────────────────────────

    def crawl(self, start_page: int = 1, end_page: int = 5):
        """执行完整爬取流程。"""
        t0 = time.monotonic()

        # 第一级: 收集文章
        articles = self.collect_articles(start_page, end_page)
        if not articles:
            logger.warning("未找到任何文章，退出")
            return

        # 第二级: 解析图片 + 下载
        logger.info(f"开始处理 {len(articles)} 篇文章的图片...")

        # 为每篇文章创建子目录
        os.makedirs(self.output_dir, exist_ok=True)

        pbar = tqdm(total=len(articles), desc="文章", unit="篇")

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

        elapsed = time.monotonic() - t0
        logger.info("=" * 60)
        logger.info(f"爬取完成! 耗时 {elapsed:.1f}s")
        logger.info(f"  文章发现: {self._stats['articles_found']}")
        logger.info(f"  图片发现: {self._stats['images_found']}")
        logger.info(f"  图片保存: {self._stats['images_saved']}")
        logger.info(f"  图片过滤: {self._stats['images_filtered']}")
        logger.info(f"  图片失败: {self._stats['images_failed']}")
        logger.info(f"  输出目录: {os.path.abspath(self.output_dir)}")

    def _process_article(self, article: dict):
        """处理单篇文章: 解析图片 → 下载保存。"""
        if self._stop.is_set():
            return

        article_id = article["id"]
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


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="绝对领域 (jdlingyu.com) 妹子图专区爬取脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 页面范围
    parser.add_argument("--start_page", type=int, default=1,
                        help="起始列表页（默认 1）")
    parser.add_argument("--end_page", type=int, default=5,
                        help="结束列表页（默认 5，共 184 页）")

    # 输出
    parser.add_argument("--output_dir", type=str, default="./output_jdlingyu",
                        help="输出目录（默认 ./output_jdlingyu）")

    # 网络
    parser.add_argument("--proxies", nargs="*", default=[],
                        help="代理列表，支持 http:// 和 socks5://")
    parser.add_argument("--rate", type=float, default=5.0,
                        help="每秒最大请求数（默认 5.0，礼貌爬取）")
    parser.add_argument("--max_retries", type=int, default=3,
                        help="最大重试次数（默认 3）")
    parser.add_argument("--timeout", type=int, default=15,
                        help="请求超时秒数（默认 15）")

    # 并发
    parser.add_argument("--max_workers", type=int, default=8,
                        help="下载线程池大小（默认 8）")

    # 过滤
    parser.add_argument("--min_width", type=int, default=200,
                        help="最小图片宽度（像素，默认 200）")
    parser.add_argument("--min_height", type=int, default=200,
                        help="最小图片高度（像素，默认 200）")

    # 断点续采
    parser.add_argument("--resume", action="store_true",
                        help="开启断点续采")

    args = parser.parse_args()

    crawler = JDLingyuCrawler(
        output_dir=args.output_dir,
        proxies=args.proxies or [],
        rate=args.rate,
        max_retries=args.max_retries,
        timeout=args.timeout,
        max_workers=args.max_workers,
        resume=args.resume,
        min_width=args.min_width,
        min_height=args.min_height,
    )

    crawler.crawl(
        start_page=args.start_page,
        end_page=args.end_page,
    )


if __name__ == "__main__":
    main()
