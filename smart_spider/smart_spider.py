# coding=utf-8
"""SmartSpider 核心模块：多模态智能爬虫。

架构总览
--------
                     ┌─────────────────────────────────────┐
                     │         SmartSpider                 │
                     │  keywords × engines × media_types   │
                     └──────────────┬──────────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
     ┌────────────────┐   ┌─────────────────┐   ┌────────────────┐
     │  Static Engine │   │ Dynamic Engine  │   │  Video Engine  │
     │  (HTTP GET)    │   │ (Playwright)    │   │  (yt-dlp)      │
     └───────┬────────┘   └───────┬─────────┘   └───────┬────────┘
             │                    │                      │
             └──────────┬─────────┘                      │
                        ▼                                 ▼
              ┌──────────────────┐              ┌────────────────────┐
              │  infer_queue     │              │  video_queue       │
              │ (PIL Image 批)   │              │ (page URL 批)      │
              └────────┬─────────┘              └─────────┬──────────┘
                       ▼                                   ▼
              ┌──────────────────┐              ┌────────────────────┐
              │  CLIP 推理线程   │              │  VideoDownloader   │
              │  批量过滤保存    │              │  yt-dlp 下载       │
              └──────────────────┘              └────────────────────┘

模态管道说明
-----------
- IMAGE : 下载线程 → CLIP相似度过滤 → 写入 output/{keyword}/image/
- VIDEO : 搜索线程收集页面URL → yt-dlp下载 → 写入 output/{keyword}/video/
- TEXT  : 搜索线程收集文章URL → trafilatura提取正文 → 写入 output/{keyword}/text/

优化变更记录（v2）
-----------------
1. 删除文件底部重复 CLI 代码，统一到 spider.py
2. _count_pages 重构为并行探测 + 遇空页即停策略，避免串行阻塞
3. pbar_lock 与 seen_lock 职责分离，减少锁争用
4. VideoDownloader 接入 ProxyPool，支持代理轮换
5. 引入 UrlDeduplicator（可选 BloomFilter，降级到 set）
6. 修复 BilibiliVideoEngine thumb URL 协议补全 Bug（在 engines.py）
7. TextExtractor 中 import re 移至模块顶部
"""
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable, Optional

import numpy as np
from loguru import logger
from PIL import Image
from tqdm import tqdm

from .browser import DynamicRenderer
from .engines import (
    ENGINE_REGISTRY,
    MediaType,
    RenderMode,
    get_engine,
)
from .http_client import SmartHttpClient, ProxyPool
from .utils import create_file, is_image_downloaded, print_logo_str

# torch / clip 延迟导入：仅在图片模态（CLIP 推理）需要时加载
# 这样 SiteCrawler / UrlDeduplicator 等组件可在无 torch 环境下独立使用
try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    torch = None  # type: ignore

try:
    import clip
    _CLIP_AVAILABLE = True
except ImportError:
    _CLIP_AVAILABLE = False
    logger.warning("openai-clip not installed. Image modal disabled. "
                   "Run: pip install git+https://github.com/openai/CLIP.git")

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

_SENTINEL = object()
MAX_CHECK_PAGES = 50          # 翻页探测上限（并行探测时作为并发上限）
_PAGE_PROBE_WORKERS = 8       # 翻页探测并行度
_QUEUE_PUT_TIMEOUT = 5.0      # 队列 put 超时秒数（防止消费者卡死时生产者永久阻塞）
_DEFAULT_DISK_GUARD_MB = 500  # 磁盘剩余低于此值(MB)时暂停采集


# ──────────────────────────────────────────────────────────────────────────────
# 回调钩子系统
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CrawlEvent:
    """采集生命周期事件，传递给回调函数。"""
    event_type: str          # "item_saved" / "item_filtered" / "item_failed" / "page_fetched" / "crawl_done"
    keyword: str
    media_type: str
    engine: str = ""
    url: str = ""
    detail: dict = field(default_factory=dict)


CallbackFn = Callable[[CrawlEvent], None]


# ──────────────────────────────────────────────────────────────────────────────
# 采集统计数据
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CrawlStats:
    """单次采集任务的统计数据，线程安全。"""
    saved: dict = field(default_factory=lambda: {})   # {media_type: count}
    filtered: dict = field(default_factory=lambda: {})  # {media_type: count}
    failed: dict = field(default_factory=lambda: {})  # {media_type: count}
    pages_fetched: int = 0
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc_saved(self, media_type: str, n: int = 1):
        with self._lock:
            self.saved[media_type] = self.saved.get(media_type, 0) + n

    def inc_filtered(self, media_type: str, n: int = 1):
        with self._lock:
            self.filtered[media_type] = self.filtered.get(media_type, 0) + n

    def inc_failed(self, media_type: str, n: int = 1):
        with self._lock:
            self.failed[media_type] = self.failed.get(media_type, 0) + n

    def inc_pages(self, n: int = 1):
        with self._lock:
            self.pages_fetched += n

    @property
    def elapsed_seconds(self) -> float:
        end = self.end_time or time.monotonic()
        return max(0.0, end - self.start_time)

    def summary(self) -> dict:
        with self._lock:
            return {
                "saved": dict(self.saved),
                "filtered": dict(self.filtered),
                "failed": dict(self.failed),
                "pages_fetched": self.pages_fetched,
                "elapsed_seconds": round(self.elapsed_seconds, 2),
            }


# ──────────────────────────────────────────────────────────────────────────────
# 模态完成事件管理器
# ──────────────────────────────────────────────────────────────────────────────

class _ModalDoneEvents:
    """管理各模态独立的 done_event 以及全局 all_done_event。

    替代旧版 download() 中的猴补丁（ev.set = _patched_set）写法，
    将"模态完成 -> 检查全局完成"的逻辑封装在此类中，更易测试和复用。

    用法
    ----
        tracker = _ModalDoneEvents(["image", "video"])
        tracker.mark_done("image")          # image 模态完成
        tracker.is_done("image")            # True
        tracker.is_all_done()               # False（video 还没完成）
        tracker.mark_done("video")
        tracker.is_all_done()               # True
        tracker.reset()                     # 为下一个关键词重置
    """

    def __init__(self, active_media: list):
        self._active = list(active_media)
        self._lock = threading.Lock()
        # 每种模态一个 Event；all_done 在全部模态完成后置位
        self._modal_events: dict[str, threading.Event] = {
            mt: threading.Event() for mt in self._active
        }
        self._all_done = threading.Event()

    # ── 查询接口 ────────────────────────────────────────────────────

    def modal_event(self, mt: str) -> threading.Event:
        """返回指定模态的 done Event（供 worker 线程持有并在完成时调用 set）。"""
        return self._modal_events[mt]

    @property
    def all_done_event(self) -> threading.Event:
        """全局完成 Event（所有模态均 done 时置位）。"""
        return self._all_done

    def is_done(self, mt: str) -> bool:
        return self._modal_events[mt].is_set()

    def is_all_done(self) -> bool:
        return self._all_done.is_set()

    # ── 写入接口 ────────────────────────────────────────────────────

    def mark_done(self, mt: str) -> None:
        """将指定模态标记为完成，并在所有模态均完成时自动置位 all_done。"""
        self._modal_events[mt].set()
        with self._lock:
            if all(e.is_set() for e in self._modal_events.values()):
                if not self._all_done.is_set():
                    self._all_done.set()
                    logger.info("All modals reached max_items; stopping all crawl tasks.")

    def reset(self) -> None:
        """重置所有 Event，供多关键词循环时在每个关键词开始前调用。"""
        with self._lock:
            for ev in self._modal_events.values():
                ev.clear()
            self._all_done.clear()

_MAGIC_MAP = {
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG":      ".png",
    b"RIFF":         ".webp",
    b"GIF8":         ".gif",
    # AVIF: ftyp box at offset 4, brand = avif or avis
    # Byte pattern: ....ftypavif or ....ftypavis
}

_AVIF_BRANDS = {b"avif", b"avis"}


def _detect_ext(content: bytes) -> str:
    for magic, ext in _MAGIC_MAP.items():
        if content[: len(magic)] == magic:
            if ext == ".webp" and content[8:12] != b"WEBP":
                continue
            return ext
    # AVIF/AVIS detection: check ftyp box brand
    if len(content) >= 12:
        # ISO BMFF box: 4-byte size + 'ftyp' + brand (at offset 8-12)
        if content[4:8] == b"ftyp" and content[8:12] in _AVIF_BRANDS:
            return ".avis" if content[8:12] == b"avis" else ".avif"
    return ".jpg"


# ──────────────────────────────────────────────────────────────────────────────
# URL 去重器（BloomFilter 优先，降级到 set）
# ──────────────────────────────────────────────────────────────────────────────

class UrlDeduplicator:
    """线程安全的 URL + 内容双重去重器。

    双重去重策略
    ------------
    1. URL 去重：MD5(url)，防止同一 URL 重复采集
    2. 内容去重：MD5(content)，防止不同 URL 但相同内容的资源重复保存
       （常见于 CDN 镜像、缩略图/原图指向同一文件）

    存储后端
    --------
    优先使用 pybloom-live 的 BloomFilter（内存效率高，适合长期运行）；
    若未安装则降级到 in-memory set（精确但内存无界增长）。

    持久化（断点续采）
    ------------------
    传入 persist_path 时，每次新增 URL hash 都追加写入 .jsonl 文件。
    初始化时若文件已存在则自动加载，实现跨进程的去重状态恢复。

    >>> dedup = UrlDeduplicator(persist_path="./output/.seen_urls.jsonl")
    """

    def __init__(
        self,
        capacity: int = 1_000_000,
        error_rate: float = 0.001,
        persist_path: Optional[str] = None,
        content_dedup: bool = True,
    ):
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._persist_file = None  # 写追加文件句柄
        self._content_dedup = content_dedup

        try:
            from pybloom_live import ScalableBloomFilter
            self._bloom = ScalableBloomFilter(
                initial_capacity=capacity,
                error_rate=error_rate,
            )
            self._set = None
            # 内容去重也使用 BloomFilter
            self._content_bloom = ScalableBloomFilter(
                initial_capacity=capacity,
                error_rate=error_rate,
            ) if content_dedup else None
            self._content_set: Optional[set] = None
            logger.info("UrlDeduplicator: using BloomFilter (pybloom-live)")
        except ImportError:
            self._bloom = None
            self._set: set = set()
            self._content_bloom = None
            self._content_set: Optional[set] = set() if content_dedup else None
            logger.debug("UrlDeduplicator: using in-memory set "
                         "(install pybloom-live for memory-efficient dedup)")

        # 加载持久化状态
        if persist_path:
            self._load(persist_path)
            self._persist_file = open(persist_path, "a", encoding="utf-8", buffering=1)  # line-buffered

    def _load(self, path: str):
        """从持久化文件恢复已见 URL hash。"""
        if not os.path.exists(path):
            return
        loaded = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    h = line.strip()
                    if not h:
                        continue
                    if self._bloom is not None:
                        self._bloom.add(h)
                    else:
                        self._set.add(h)
                    loaded += 1
            logger.info(f"UrlDeduplicator: loaded {loaded} seen URLs from {path}")
        except Exception as e:
            logger.warning(f"UrlDeduplicator: failed to load {path}: {e}")

    def is_seen(self, url: str) -> bool:
        """检查 URL 是否已见过，并将其标记为已见。线程安全。"""
        h = hashlib.md5(url.encode()).hexdigest()
        with self._lock:
            if self._bloom is not None:
                if h in self._bloom:
                    return True
                self._bloom.add(h)
            else:
                if h in self._set:
                    return True
                self._set.add(h)
            # 持久化新增 hash
            if self._persist_file is not None:
                try:
                    self._persist_file.write(h + "\n")
                except Exception:
                    pass
            return False

    def is_content_seen(self, content: bytes) -> bool:
        """检查内容是否已见过（MD5 去重），并将其标记为已见。线程安全。

        用于防止不同 URL 指向相同内容的资源重复保存（CDN 镜像等场景）。
        未启用内容去重时始终返回 False。
        """
        if not self._content_dedup:
            return False
        h = hashlib.md5(content).hexdigest()
        with self._lock:
            if self._content_bloom is not None:
                if h in self._content_bloom:
                    return True
                self._content_bloom.add(h)
            elif self._content_set is not None:
                if h in self._content_set:
                    return True
                self._content_set.add(h)
            return False

    def close(self):
        """关闭持久化文件句柄。"""
        with self._lock:
            if self._persist_file is not None:
                try:
                    self._persist_file.close()
                except Exception:
                    pass
                self._persist_file = None

    def __len__(self) -> int:
        with self._lock:
            if self._set is not None:
                return len(self._set)
            return -1  # BloomFilter 无法精确计数

    def __del__(self):
        self.close()


# ──────────────────────────────────────────────────────────────────────────────
# 视频下载器（yt-dlp），接入 ProxyPool
# ──────────────────────────────────────────────────────────────────────────────

class VideoDownloader:
    """用 yt-dlp 下载视频页面 URL 对应的视频流。

    支持 ProxyPool 轮换代理（优先）或固定代理（fallback）。
    下载失败时自动换代理重试（最多 max_retries 次）。
    """

    def __init__(
        self,
        output_dir: str,
        proxy_pool: Optional[ProxyPool] = None,
        proxy: Optional[str] = None,
        max_filesize: str = "500m",
        format_spec: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        cookies_file: Optional[str] = None,
        max_retries: int = 2,
        timeout: int = 300,
    ):
        self.output_dir = output_dir
        self._proxy_pool = proxy_pool
        self._static_proxy = proxy
        self.max_filesize = max_filesize
        self.format_spec = format_spec
        self.cookies_file = cookies_file
        self.max_retries = max_retries
        self.timeout = timeout

    def _pick_proxy(self) -> Optional[str]:
        """从 ProxyPool 取一个代理 URL，无代理时返回 None。"""
        if self._proxy_pool and not self._proxy_pool.is_empty():
            entry = self._proxy_pool.get()
            return entry.url if entry else None
        return self._static_proxy

    def _ytdlp_cmd(self, url: str, out_path: str, proxy: Optional[str]) -> list[str]:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--format", self.format_spec,
            "--max-filesize", self.max_filesize,
            "--output", os.path.join(out_path, "%(title)s.%(ext)s"),
            "--no-warnings",
            "--quiet",
            "--write-info-json",
            "--write-thumbnail",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        if self.cookies_file and os.path.exists(self.cookies_file):
            cmd += ["--cookies", self.cookies_file]
        cmd.append(url)
        return cmd

    def download(self, url: str, out_path: str) -> bool:
        """下载视频，失败时换代理重试（最多 max_retries 次）。

        Bug fix: old version picked a proxy once and returned False on failure
        without retrying.  With a proxy pool this meant a bad proxy would cause
        every download to fail silently.  Now each retry picks a fresh proxy.
        """
        os.makedirs(out_path, exist_ok=True)
        for attempt in range(self.max_retries + 1):
            proxy = self._pick_proxy()
            cmd = self._ytdlp_cmd(url, out_path, proxy)
            logger.info(
                f"yt-dlp (attempt {attempt + 1}/{self.max_retries + 1}): {url[:60]}"
                + (f" [proxy={proxy}]" if proxy else "")
            )
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout
                )
                if result.returncode == 0:
                    logger.info(f"视频下载成功: {url[:60]}")
                    return True
                logger.warning(
                    f"yt-dlp 失败 [{result.returncode}] attempt {attempt + 1}: "
                    f"{result.stderr[:200]}"
                )
            except subprocess.TimeoutExpired:
                logger.error(f"VideoDownloader timeout (attempt {attempt + 1}): {url[:60]}")
            except FileNotFoundError:
                logger.error("yt-dlp not found; install with: pip install yt-dlp")
                return False  # no point retrying if binary is missing
            except Exception as e:
                logger.error(f"VideoDownloader unexpected error (attempt {attempt + 1}): {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 文本提取器
# ──────────────────────────────────────────────────────────────────────────────

class TextExtractor:
    """从网页 URL 提取纯文本正文，依赖 trafilatura（可选）。"""

    def __init__(self, http_client: SmartHttpClient):
        self._client = http_client
        try:
            import trafilatura
            self._trafilatura = trafilatura
        except ImportError:
            self._trafilatura = None
            logger.warning("trafilatura not installed, text extraction will use basic regex. "
                           "Run: pip install trafilatura")

    def extract(self, url: str) -> Optional[dict]:
        """
        提取页面正文。

        Returns:
            {"url": str, "title": str, "text": str, "date": str, "author": str} 或 None
        """
        try:
            html = self._client.get_text(url)
        except Exception as e:
            logger.error(f"TextExtractor fetch error {url[:60]}: {e}")
            return None

        if not html or len(html.strip()) < 50:
            logger.debug(f"TextExtractor: empty/too-short response for {url[:60]}")
            return None

        if self._trafilatura:
            try:
                result = self._trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    output_format="json",
                    url=url,
                )
                if result:
                    data = json.loads(result)
                    text = data.get("text", "")
                    if text and len(text.strip()) > 20:
                        return {
                            "url": url,
                            "title": data.get("title", ""),
                            "text": text,
                            "date": data.get("date", ""),
                            "author": data.get("author", ""),
                        }
            except Exception as e:
                logger.warning(f"trafilatura error: {e}")

        # 降级：简单正则提取 <title> + <p> 文本
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL | re.IGNORECASE)
        text = "\n".join(
            re.sub(r"<[^>]+>", "", p).strip()
            for p in paragraphs
            if len(re.sub(r"<[^>]+>", "", p).strip()) > 50
        )
        # 有 title 或有正文时返回结果；两者都空则返回 None
        if not text and not title:
            return None
        return {"url": url, "title": title, "text": text, "date": "", "author": ""}


# ──────────────────────────────────────────────────────────────────────────────
# SmartSpider 主类
# ──────────────────────────────────────────────────────────────────────────────

class SmartSpider:
    """
    多模态智能爬虫。

    支持模态
    --------
    - image  : 图片（CLIP 过滤）
    - video  : 视频（yt-dlp 下载）
    - text   : 文章正文（trafilatura 提取）

    反爬能力
    --------
    - 代理池轮换（HTTP / SOCKS5）
    - 令牌桶限速
    - 指数退避重试
    - 完整浏览器指纹（UA / Header / Sec-Fetch）
    - Playwright 动态渲染（SPA、JS 加密、Cookie 验证）
    - 请求随机抖动延迟
    """

    def __init__(
        self,
        keywords: list[str],
        max_items: int = 50,
        media_types: Optional[list[str]] = None,
        similarity_threshold: float = 0.20,
        timeout: int = 10,
        max_workers: int = 20,
        search_engines: Optional[list[str]] = None,
        output_dir: str = ".",
        batch_size: int = 16,
        # 反爬参数
        proxies: Optional[list[str]] = None,
        rate: float = 8.0,
        max_retries: int = 3,
        # 动态渲染
        use_browser: bool = False,
        headless: bool = True,
        browser_proxy: Optional[str] = None,
        cookies: Optional[list[dict]] = None,
        # 视频参数
        video_format: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        video_max_size: str = "500m",
        cookies_file: Optional[str] = None,
        # 断点续采
        resume: bool = False,
        # v2.1 新增参数
        clip_model: str = "ViT-B/32",
        callbacks: Optional[list[CallbackFn]] = None,
        disk_guard_mb: int = _DEFAULT_DISK_GUARD_MB,
        content_dedup: bool = True,
        video_concurrency: int = 3,
    ):
        print_logo_str()

        self.keywords = keywords
        self.max_items = max_items
        self.media_types = set(media_types or ["image"])
        self.similarity_threshold = similarity_threshold
        self.timeout = timeout
        self.max_workers = max_workers
        self.output_dir = output_dir
        self.batch_size = batch_size
        self.use_browser = use_browser
        self.clip_model_name = clip_model
        self._callbacks = callbacks or []
        self._disk_guard_mb = disk_guard_mb
        self._video_concurrency = video_concurrency

        # 优雅关停：监听 SIGINT/SIGTERM
        self._shutdown_requested = threading.Event()
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)

        def _shutdown_handler(signum, frame):
            logger.warning(f"Received signal {signum}, graceful shutdown initiated...")
            self._shutdown_requested.set()

        signal.signal(signal.SIGINT, _shutdown_handler)
        signal.signal(signal.SIGTERM, _shutdown_handler)

        # 采集统计
        self.stats = CrawlStats()

        # 引擎选择：按指定引擎 or 自动匹配模态
        if search_engines:
            self.search_engines = search_engines
        else:
            self.search_engines = [
                name for name, eng in ENGINE_REGISTRY.items()
                if eng.media_type.value in self.media_types
            ]
        logger.info(f"Active engines: {self.search_engines}")

        # ── 锁职责分离 ──────────────────────────────────────────────
        # pbar_lock 保护进度条更新（低频）
        # URL 去重由 UrlDeduplicator._lock 内部管理，无需外部锁
        self._pbar_lock = threading.Lock()

        # URL 去重 + 内容去重（BloomFilter 优先，降级 set）
        # resume=True 时从持久化文件恢复已见 URL，实现断点续采
        _dedup_path: Optional[str] = None
        if resume:
            os.makedirs(output_dir, exist_ok=True)
            _dedup_path = os.path.join(output_dir, ".seen_urls.jsonl")
            if os.path.exists(_dedup_path):
                logger.info(f"断点续采：从 {_dedup_path} 恢复去重状态")
            else:
                logger.info(f"断点续采：去重状态将保存到 {_dedup_path}")
        self._dedup = UrlDeduplicator(persist_path=_dedup_path, content_dedup=content_dedup)

        # 网络层
        self._proxy_pool = ProxyPool(proxies or [])
        self._http = SmartHttpClient(
            proxies=proxies or [],
            rate=rate,
            max_retries=max_retries,
            timeout=timeout,
        )

        # 动态渲染器（懒加载）
        self._renderer: Optional[DynamicRenderer] = None
        self._renderer_cookies = cookies or []
        self._browser_proxy = browser_proxy
        self._headless = headless
        if use_browser:
            self._renderer = DynamicRenderer(
                proxy=browser_proxy,
                headless=headless,
                cookies=cookies or [],
            )

        # CLIP 模型（图片模态专用）
        if "image" in self.media_types:
            if not _CLIP_AVAILABLE:
                raise RuntimeError(
                    "openai-clip is required for image modal. "
                    "Run: pip install git+https://github.com/openai/CLIP.git"
                )
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model, self.preprocess = clip.load(self.clip_model_name, device=self.device)
            self.model.eval()
            logger.info(f"CLIP model '{self.clip_model_name}' loaded on {self.device}")
        else:
            self.model = self.preprocess = None
            self.device = "cpu"

        # CLIP 文本特征全局缓存：key=keyword，val=已归一化的 tensor
        # 跨批次复用，避免相同关键词重复 encode（GPU encode 耗时约 10ms/次）
        self._clip_text_cache: dict[str, Optional[torch.Tensor]] = {}
        self._clip_text_cache_lock = threading.Lock()

        # 视频下载器：传入 ProxyPool，支持轮换
        self._video_dl = VideoDownloader(
            output_dir=output_dir,
            proxy_pool=self._proxy_pool if proxies else None,
            proxy=proxies[0] if proxies else None,  # fallback
            format_spec=video_format,
            max_filesize=video_max_size,
            cookies_file=cookies_file,
            timeout=timeout * 30,  # 视频下载超时 = HTTP 超时 × 30（视频文件大）
        )

        # 文本提取器
        self._text_extractor = TextExtractor(self._http)

    # ──────────────────────────────────────────────────────────────
    # URL 去重（使用独立的 UrlDeduplicator）
    # ──────────────────────────────────────────────────────────────

    def _is_seen(self, url: str) -> bool:
        return self._dedup.is_seen(url)

    @classmethod
    def _ensure_stub_attrs(cls, instance: "SmartSpider") -> None:
        """为通过 object.__new__() 创建的测试桩补齐 v2.1 新增属性。

        测试中使用 ``object.__new__(SmartSpider)`` 跳过 ``__init__`` 时，
        新增属性不会自动初始化，调用此方法一次性补齐，避免 AttributeError。
        """
        import threading as _th

        if not hasattr(instance, "_shutdown_requested"):
            instance._shutdown_requested = _th.Event()
        if not hasattr(instance, "_original_sigint"):
            instance._original_sigint = signal.SIG_DFL
        if not hasattr(instance, "_original_sigterm"):
            instance._original_sigterm = signal.SIG_DFL
        if not hasattr(instance, "stats"):
            instance.stats = CrawlStats()
        if not hasattr(instance, "_callbacks"):
            instance._callbacks = []
        if not hasattr(instance, "_disk_guard_mb"):
            instance._disk_guard_mb = _DEFAULT_DISK_GUARD_MB
        if not hasattr(instance, "_video_concurrency"):
            instance._video_concurrency = 3
        if not hasattr(instance, "clip_model_name"):
            instance.clip_model_name = "ViT-B/32"
        if not hasattr(instance, "_should_stop"):
            instance._should_stop = lambda: instance._shutdown_requested.is_set()
        if not hasattr(instance, "_dedup"):
            instance._dedup = UrlDeduplicator(content_dedup=False)

    def _emit_callback(self, event: CrawlEvent) -> None:
        """安全触发所有回调。"""
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    def _check_disk_space(self) -> bool:
        """检查磁盘剩余空间，低于阈值返回 False。"""
        try:
            usage = shutil.disk_usage(self.output_dir)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < self._disk_guard_mb:
                logger.error(
                    f"Disk space guard: only {free_mb:.0f} MB free "
                    f"(threshold={self._disk_guard_mb} MB), pausing crawl"
                )
                return False
        except Exception:
            pass  # 无法检测时不阻断
        return True

    def _should_stop(self) -> bool:
        """检查是否应该停止采集（SIGINT/SIGTERM 或磁盘不足）。"""
        return self._shutdown_requested.is_set() or not self._check_disk_space()

    # ──────────────────────────────────────────────────────────────
    # 页面获取（静态 / 动态自动路由）
    # ──────────────────────────────────────────────────────────────

    def _fetch_page(self, engine_name: str, url: str) -> str:
        eng = get_engine(engine_name)
        if eng.render_mode == RenderMode.DYNAMIC:
            if self._renderer is None:
                # 懒初始化渲染器
                self._renderer = DynamicRenderer(
                    proxy=self._browser_proxy,
                    headless=self._headless,
                    cookies=self._renderer_cookies,
                )
            return self._renderer.render(url, scroll_to_bottom=True)
        else:
            return self._http.get_text(url, engine=engine_name)

    def _log_context(self, keyword: str = "", engine: str = "") -> str:
        """生成结构化日志前缀，便于 grep 和分析。"""
        parts = []
        if keyword:
            parts.append(f"kw={keyword}")
        if engine:
            parts.append(f"eng={engine}")
        return f"[{'|'.join(parts)}] " if parts else ""

    def _get_text_feature(self, keyword: str) -> Optional["torch.Tensor"]:
        """获取关键词的 CLIP 文本特征（实例级缓存，线程安全）。

        首次调用时 encode 并缓存，后续直接返回缓存 tensor。
        避免同一关键词在每个批次重复 encode（GPU encode ≈10ms/次）。
        """
        with self._clip_text_cache_lock:
            if keyword in self._clip_text_cache:
                return self._clip_text_cache[keyword]

        # 在锁外做 encode，避免长时间持锁
        try:
            tok = clip.tokenize([keyword]).to(self.device)
            with torch.no_grad():
                tf = self.model.encode_text(tok)
                tf = tf / tf.norm(dim=-1, keepdim=True)
            result: Optional[torch.Tensor] = tf
        except Exception as e:
            logger.error(f"Text encode error '{keyword}': {e}")
            result = None

        with self._clip_text_cache_lock:
            self._clip_text_cache[keyword] = result
        return result

    # ──────────────────────────────────────────────────────────────
    # CLIP 推理线程（图片模态）
    # ──────────────────────────────────────────────────────────────

    def _infer_worker(
        self,
        infer_queue: queue.Queue,
        pbar: tqdm,
        save_dir: str,
        done_event: threading.Event,
        target: int,
        modal_tracker: Optional["_ModalDoneEvents"] = None,
    ):
        batch: list[tuple] = []
        saved = [0]  # 用列表包装使嵌套函数可修改

        def _flush(items):
            if not items:
                return
            # Fast-path: if we have already saved enough images, skip the
            # entire CLIP forward pass (stack + encode_image is expensive).
            # Bug fix: old code ran encode_image even when saved[0] >= target,
            # wasting GPU time on every batch that arrives after the goal is met.
            if saved[0] >= target:
                return
            images, keywords_b, urls, metas, contents = zip(*items)
            try:
                tensors = torch.stack([self.preprocess(img) for img in images]).to(self.device)
            except Exception as e:
                logger.error(f"Preprocess error: {e}")
                return

            with torch.no_grad():
                img_feats = self.model.encode_image(tensors)
                img_feats = img_feats / img_feats.norm(dim=-1, keepdim=True)

            # 文本特征（从全局缓存获取，避免跨批次重复 encode）
            kw_cache: dict[str, Optional[torch.Tensor]] = {}
            for kw in dict.fromkeys(keywords_b):
                kw_cache[kw] = self._get_text_feature(kw)

            for i, (img, kw, url, meta, content) in enumerate(
                zip(images, keywords_b, urls, metas, contents)
            ):
                tf = kw_cache.get(kw)
                if tf is None:
                    continue
                sim = torch.nn.functional.cosine_similarity(
                    img_feats[i].unsqueeze(0), tf
                ).item()
                logger.debug(f"[{kw}] sim={sim:.4f}  {url[:55]}")

                if sim > self.similarity_threshold:
                    # Stop saving once target is reached, even within the same batch.
                    # Bug fix: old code only set done_event but kept looping over the
                    # remaining items in the batch, causing saved[0] to exceed target.
                    if saved[0] >= target:
                        return
                    try:
                        # 内容去重：防止不同 URL 指向相同内容的图片重复保存
                        if self._dedup.is_content_seen(content):
                            logger.debug(f"Content duplicate, skipping {url[:55]}")
                            continue
                        # 直接使用 _search_worker 已下载的 content，无需二次请求
                        ext = _detect_ext(content)
                        name = hashlib.md5(url.encode()).hexdigest()
                        fp = os.path.join(save_dir, f"{name}{ext}")
                        with open(fp, "wb") as f:
                            f.write(content)
                        if meta:
                            meta_fp = fp + ".json"
                            with open(meta_fp, "w", encoding="utf-8") as f:
                                json.dump({"url": url, **meta}, f, ensure_ascii=False)
                        logger.info(f"图片保存: {name}{ext} [{kw}] sim={sim:.3f}")
                        with self._pbar_lock:
                            pbar.update(1)
                        saved[0] += 1
                        self.stats.inc_saved("image")
                        self._emit_callback(CrawlEvent(
                            event_type="item_saved", keyword=kw,
                            media_type="image", url=url,
                            detail={"sim": round(sim, 4), "file": fp},
                        ))
                        if saved[0] >= target:
                            if modal_tracker is not None:
                                modal_tracker.mark_done("image")
                            else:
                                done_event.set()
                            return  # stop processing remaining items in this batch
                    except Exception as e:
                        logger.error(f"Save image error {url[:60]}: {e}")
                        self.stats.inc_failed("image")
                        self._emit_callback(CrawlEvent(
                            event_type="item_failed", keyword=kw,
                            media_type="image", url=url,
                            detail={"error": str(e)},
                        ))
                else:
                    self.stats.inc_filtered("image")
                    self._emit_callback(CrawlEvent(
                        event_type="item_filtered", keyword=kw,
                        media_type="image", url=url,
                        detail={"sim": round(sim, 4)},
                    ))

        while True:
            if self._should_stop():
                _flush(batch)
                break
            try:
                item = infer_queue.get(timeout=3)
            except queue.Empty:
                _flush(batch)
                batch = []
                continue

            if item is _SENTINEL:
                _flush(batch)
                break

            # Once target is reached, drain the queue without accumulating items
            # in memory.  Holding large PIL Image objects in `batch` between flushes
            # wastes RAM for images that will never be saved.
            if saved[0] >= target:
                continue  # discard item; wait for SENTINEL

            batch.append(item)
            if len(batch) >= self.batch_size:
                _flush(batch)
                batch = []

    # ──────────────────────────────────────────────────────────────
    # 搜索页抓取线程（通用）
    # ──────────────────────────────────────────────────────────────

    def _search_worker(
        self,
        engine_name: str,
        page_url: str,
        keyword: str,
        infer_queue: Optional[queue.Queue],
        video_queue: Optional[queue.Queue],
        text_queue: Optional[queue.Queue],
        done_event: Optional[threading.Event] = None,
        all_done_event: Optional[threading.Event] = None,
    ):
        # 本模态已达目标量，或全局所有模态均已完成时，提前退出
        if done_event is not None and done_event.is_set():
            return
        if all_done_event is not None and all_done_event.is_set():
            return
        # 优雅关停检查
        if self._should_stop():
            return
        eng = get_engine(engine_name)
        try:
            html = self._fetch_page(engine_name, page_url)
            self.stats.inc_pages()
        except Exception as e:
            logger.error(f"{self._log_context(keyword, engine_name)}"
                         f"Fetch error {page_url[:60]}: {e}")
            self.stats.inc_failed(eng.media_type.value)
            return

        items = eng.extract_items(html)
        logger.debug(f"{self._log_context(keyword, engine_name)}"
                     f"{page_url[:55]} → {len(items)} items")

        for item in items:
            # Early exit: if this modal or all modals are done, stop processing
            # remaining items.  Without this check, a worker that fetched a page
            # with 30 items would still download/verify all 30 even though the
            # target was already met by another worker mid-loop.
            if done_event is not None and done_event.is_set():
                break
            if all_done_event is not None and all_done_event.is_set():
                break
            url = item.get("url", "")
            if not url or self._is_seen(url):
                continue
            meta = item.get("meta", {})

            if eng.media_type == MediaType.IMAGE and infer_queue is not None:
                # Image pipeline: cheap pre-filter -> full download -> infer queue.
                # Pre-filter uses the first 8 KB (peek) for format/size/variance checks.
                # Full content is read only after passing pre-filter to avoid wasted
                # bandwidth on rejected images.
                #
                # Bug fix (response leak): old code left the HTTP response unclosed
                # on every early-return path (bad extension, small image, read error,
                # verify failure, low variance).  Each leaked response held a socket
                # in the connection pool, eventually exhausting it under heavy load.
                # Fix: wrap the entire block in try/finally with resp.close().
                resp = None
                try:
                    peek, resp = self._http.get_stream(url, engine=engine_name, peek_bytes=8192)
                    ext = _detect_ext(peek)
                    if ext not in (".jpg", ".png", ".webp"):
                        continue
                    # Cheap pre-filter: use peek header to check format / dimensions.
                    # PIL can read image dimensions from the header alone (no full decode).
                    # NOTE: we do NOT store this img for CLIP -- peek is truncated and the
                    # decoded pixels would be garbage for large images.  The full-body img
                    # is built below after we have downloaded all bytes.
                    try:
                        peek_img = Image.open(BytesIO(peek))
                        if peek_img.width < 100 or peek_img.height < 100:
                            continue
                    except Exception:
                        continue
                    # Pre-filter passed: now read the complete response body.
                    # On ANY network error we drop this URL -- never write partial data.
                    try:
                        rest = b"".join(resp.iter_content(chunk_size=65536))
                        full_content = peek + rest
                    except Exception as read_err:
                        logger.warning(
                            f"Image body read failed, dropping {url[:55]}: {read_err}"
                        )
                        continue  # drop -- do NOT fall back to peek-only
                    # Decode the FULL image for downstream checks and CLIP inference.
                    # Using full_content here (not peek) fixes the Bug where CLIP was
                    # operating on truncated pixel data from the 8 KB peek window.
                    try:
                        buf = BytesIO(full_content)
                        img = Image.open(buf)
                        img.verify()          # detect truncation / corruption early
                        buf.seek(0)           # rewind instead of allocating a new BytesIO
                        img = Image.open(buf).convert("RGB")  # re-open after verify
                    except Exception:
                        logger.warning(f"Image verification failed (truncated?), dropping {url[:55]}")
                        continue
                    # Low-variance filter (blank / solid-color images) on full image
                    arr = np.array(img.resize((32, 32)))
                    if np.std(arr) < 10:
                        continue
                    try:
                        infer_queue.put((img, keyword, url, meta, full_content), timeout=_QUEUE_PUT_TIMEOUT)
                    except queue.Full:
                        logger.warning(f"infer_queue full, dropping {url[:55]}")
                except Exception as e:
                    logger.warning(f"Image pre-filter error {url[:55]}: {e}")
                finally:
                    # Guarantee the streaming response is always closed, preventing
                    # socket/connection leaks regardless of which continue path was taken.
                    if resp is not None:
                        try:
                            resp.close()
                        except Exception:
                            pass

            elif eng.media_type == MediaType.VIDEO and video_queue is not None:
                try:
                    video_queue.put((url, keyword, meta), timeout=_QUEUE_PUT_TIMEOUT)
                except queue.Full:
                    logger.warning(f"video_queue full, dropping {url[:55]}")

            elif eng.media_type == MediaType.TEXT and text_queue is not None:
                try:
                    text_queue.put((url, keyword, meta), timeout=_QUEUE_PUT_TIMEOUT)
                except queue.Full:
                    logger.warning(f"text_queue full, dropping {url[:55]}")

    # ──────────────────────────────────────────────────────────────
    # 视频下载线程
    # ──────────────────────────────────────────────────────────────

    def _video_worker(
        self,
        video_queue: queue.Queue,
        save_dir: str,
        pbar: tqdm,
        done_event: threading.Event,
        target: int,
        modal_tracker: Optional["_ModalDoneEvents"] = None,
    ):
        """视频下载消费线程，支持并行下载。"""
        saved = 0
        lock = threading.Lock()

        def _download_one(item):
            """单个视频下载任务（在线程池中执行）。"""
            nonlocal saved
            if done_event.is_set() or self._should_stop():
                return
            url, keyword, meta = item
            title = meta.get("title", "")
            logger.info(f"视频: {title[:40]}  {url[:55]}")
            success = self._video_dl.download(url, save_dir)
            with lock:
                if not success:
                    self.stats.inc_failed("video")
                    self._emit_callback(CrawlEvent(
                        event_type="item_failed", keyword=keyword,
                        media_type="video", url=url,
                    ))
                    return
                saved += 1
                with self._pbar_lock:
                    pbar.update(1)
                if saved >= target:
                    if modal_tracker is not None:
                        modal_tracker.mark_done("video")
                    else:
                        done_event.set()
            # stats/emit 在 lock 外执行（不依赖 saved 计数一致性）
            self.stats.inc_saved("video")
            self._emit_callback(CrawlEvent(
                event_type="item_saved", keyword=keyword,
                media_type="video", url=url,
                detail={"title": title},
            ))

        with ThreadPoolExecutor(max_workers=self._video_concurrency) as dl_pool:
            while True:
                if self._should_stop():
                    break
                try:
                    item = video_queue.get(timeout=5.0)
                except queue.Empty:
                    continue  # spurious wakeup or slow producer; keep waiting
                if item is _SENTINEL:
                    break
                if done_event.is_set():
                    continue  # 已达目标，消费队列但不处理
                dl_pool.submit(_download_one, item)

            # 等待所有已提交的下载任务完成
            dl_pool.shutdown(wait=True)

    # ──────────────────────────────────────────────────────────────
    # 文本提取线程
    # ──────────────────────────────────────────────────────────────

    def _text_worker(
        self,
        text_queue: queue.Queue,
        save_dir: str,
        pbar: tqdm,
        done_event: threading.Event,
        target: int,
        modal_tracker: Optional["_ModalDoneEvents"] = None,
    ):
        saved = 0
        while True:
            if self._should_stop():
                break
            try:
                item = text_queue.get(timeout=5.0)
            except queue.Empty:
                continue  # spurious wakeup or slow producer; keep waiting
            if item is _SENTINEL:
                break
            if done_event.is_set():
                continue  # 已达目标，消费队列但不处理
            url, keyword, meta = item
            result = self._text_extractor.extract(url)
            if not result or not result.get("text"):
                self.stats.inc_failed("text")
                continue
            # 内容去重：防止不同 URL 提取到相同正文
            if self._dedup.is_content_seen(result["text"].encode("utf-8")):
                logger.debug(f"Text content duplicate, skipping {url[:55]}")
                continue
            name = hashlib.md5(url.encode()).hexdigest()
            fp = os.path.join(save_dir, f"{name}.json")
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            logger.info(f"文本保存: {result['title'][:40]}")
            with self._pbar_lock:
                pbar.update(1)
            saved += 1
            self.stats.inc_saved("text")
            self._emit_callback(CrawlEvent(
                event_type="item_saved", keyword=keyword,
                media_type="text", url=url,
                detail={"title": result.get("title", "")},
            ))
            if saved >= target:
                if modal_tracker is not None:
                    modal_tracker.mark_done("text")
                else:
                    done_event.set()

    # ──────────────────────────────────────────────────────────────
    # 翻页计数（并行探测版）
    # ──────────────────────────────────────────────────────────────

    def _count_pages(self, engine_name: str, keyword: str) -> int:
        """Probe effective page count in parallel; return enough pages for max_items.

        Bug fix (insufficient page probe):
          Old version returned found_pages directly, which could be 0 or 1 when
          probe hit an empty page early, causing only 1-2 pages to be fetched.

        Fix strategy:
          1. Keep parallel probe logic (quickly find real upper bound of valid pages).
          2. Take the max of found_pages and the theoretically needed pages:
                 min_needed = ceil(max_items / page_step) + 2  (safety margin)
             This ensures enough crawl tasks even when probe stops early.
          3. Cap at MAX_CHECK_PAGES to avoid unbounded pagination.

        Other improvements vs old serial version:
          - Batch-parallel probing reduces latency by ~_PAGE_PROBE_WORKERS x
          - Stop probe immediately on first empty page (fewer wasted requests)
          - Dynamic engines skip probing (too costly), use estimated page count
        """
        eng = get_engine(engine_name)

        # dynamic engines are too expensive to probe; use estimated page count
        if eng.render_mode == RenderMode.DYNAMIC:
            pages = max(1, self.max_items // max(eng.page_step, 1))
            logger.debug(f"[{engine_name}] dynamic engine, skip probe -> {pages} pages")
            return pages

        # theoretical lower bound: how many pages do we need at minimum?
        min_needed_pages = self.max_items // max(eng.page_step, 1) + 2

        # candidate page offsets to probe (at most MAX_CHECK_PAGES pages)
        candidate_pages = list(range(0, MAX_CHECK_PAGES * eng.page_step, eng.page_step))
        found_pages = 0
        total_items = 0

        def _probe(page_offset: int) -> tuple[int, int]:
            """Probe a single page; return (page_offset, item_count)."""
            url = eng.build_check_url(keyword, page_offset)
            try:
                html = self._http.get_text(url, engine=engine_name)
                items = eng.extract_items(html)
                return page_offset, len(items)
            except Exception as e:
                logger.debug(f"Page probe failed [{engine_name}] p={page_offset}: {e}")
                return page_offset, 0

        # probe in parallel batches; stop on first empty page
        with ThreadPoolExecutor(max_workers=_PAGE_PROBE_WORKERS) as pool:
            batch_start = 0
            while total_items < self.max_items and batch_start < len(candidate_pages):
                batch = candidate_pages[batch_start: batch_start + _PAGE_PROBE_WORKERS]
                futures = {pool.submit(_probe, p): p for p in batch}
                results: dict[int, int] = {}
                for f in as_completed(futures):
                    offset, cnt = f.result()
                    results[offset] = cnt

                # accumulate in page order; stop probe on first empty page
                hit_empty = False
                for p in batch:
                    cnt = results.get(p, 0)
                    if cnt <= 0:
                        hit_empty = True
                        break
                    total_items += cnt
                    found_pages += 1
                    if total_items >= self.max_items:
                        break

                if hit_empty or total_items >= self.max_items:
                    break
                batch_start += _PAGE_PROBE_WORKERS

        # Use the larger of found_pages and min_needed_pages, capped at MAX_CHECK_PAGES.
        # This prevents the bug where probe stops at page 1 and only 1 page gets crawled.
        result = min(max(found_pages, min_needed_pages), MAX_CHECK_PAGES)
        logger.info(
            f"[{engine_name}] page probe: found={found_pages} "
            f"needed>={min_needed_pages} -> use {result} pages / ~{total_items} items"
        )
        return result

    # ──────────────────────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────────────────────

    def _crawl_keyword(self, keyword: str) -> None:
        """执行单个关键词的采集任务。

        从 download() 中提取，降低主方法复杂度。
        """
        keyword_dir = os.path.join(self.output_dir, keyword)

        # 按模态创建子目录
        dirs = {}
        for mt in self.media_types:
            d = os.path.join(keyword_dir, mt)
            os.makedirs(d, exist_ok=True)
            dirs[mt] = d

        logger.info(f"关键字: [{keyword}]  模态: {self.media_types}")

        # 队列
        infer_queue: Optional[queue.Queue] = queue.Queue(maxsize=512) if "image" in self.media_types else None
        video_queue: Optional[queue.Queue] = queue.Queue(maxsize=128) if "video" in self.media_types else None
        text_queue:  Optional[queue.Queue] = queue.Queue(maxsize=256) if "text"  in self.media_types else None

        # ── 各模态独立的完成事件（通过 _ModalDoneEvents 统一管理）──────
        active_media = list(self.media_types)
        tracker = _ModalDoneEvents(active_media)
        # 每个关键词循环开始时重置，确保上一轮的 done 状态不污染下一轮
        tracker.reset()

        with tqdm(total=self.max_items * len(active_media),
                  desc=f"[{keyword}]") as pbar:
            # 启动消费线程
            consumer_threads = []
            if infer_queue is not None:
                t = threading.Thread(
                    target=self._infer_worker,
                    args=(infer_queue, pbar, dirs["image"],
                          tracker.modal_event("image"), self.max_items,
                          tracker),
                    daemon=True,
                )
                t.start()
                consumer_threads.append(("image", t, infer_queue))

            if video_queue is not None:
                t = threading.Thread(
                    target=self._video_worker,
                    args=(video_queue, dirs["video"], pbar,
                          tracker.modal_event("video"), self.max_items,
                          tracker),
                    daemon=True,
                )
                t.start()
                consumer_threads.append(("video", t, video_queue))

            if text_queue is not None:
                t = threading.Thread(
                    target=self._text_worker,
                    args=(text_queue, dirs["text"], pbar,
                          tracker.modal_event("text"), self.max_items,
                          tracker),
                    daemon=True,
                )
                t.start()
                consumer_threads.append(("text", t, text_queue))

            # 提交搜索任务到下载线程池
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                for engine_name in self.search_engines:
                    eng = get_engine(engine_name)
                    eng_media = eng.media_type.value
                    if eng_media not in self.media_types:
                        continue
                    # 该引擎对应的模态已完成，跳过
                    if tracker.is_done(eng_media):
                        logger.info(
                            f"[{engine_name}] modal {eng_media} already done, skip"
                        )
                        continue
                    page_num = self._count_pages(engine_name, keyword)
                    logger.info(f"[{engine_name}] estimated {page_num} pages")
                    for j in range(0, page_num * eng.page_step, eng.page_step):
                        # 本模态已满足，停止向该引擎继续提交
                        if tracker.is_done(eng_media):
                            logger.info(
                                f"[{engine_name}] modal {eng_media} done, "
                                f"stop submitting tasks"
                            )
                            break
                        url = eng.build_search_url(keyword, j)
                        executor.submit(
                            self._search_worker,
                            engine_name, url, keyword,
                            infer_queue, video_queue, text_queue,
                            tracker.modal_event(eng_media),
                            tracker.all_done_event,
                        )

            # 发送毒丸，等待消费线程结束（设超时防止 worker 意外挂起时永久阻塞）
            for _, t, q in consumer_threads:
                q.put(_SENTINEL)
            for mt, t, _ in consumer_threads:
                t.join(timeout=60)
                if t.is_alive():
                    logger.warning(f"Consumer thread for '{mt}' did not exit within 60 s")

    def download(self):
        """执行多模态采集任务。

        修复说明（done_event 跨模态共享 Bug）
        -------------------------------------
        旧版用单个 done_event 全局共享，任意一种模态先达到 max_items 就
        触发整体停止，导致其他模态采集数量严重不足。

        新版为每种模态维护独立的 done_event（模态自治）：
          - done_events["image"] : image 模态达标时置位
          - done_events["video"] : video 模态达标时置位
          - done_events["text"]  : text  模态达标时置位

        同时引入 all_done_event（所有启用模态均达标时才触发全局停止）：
          - 消费线程在各自模态 done 时额外置位 all_done_event
          - all_done_event 仅在所有模态 done_event 均已置位时才 set()
          - 搜索线程同时检查本模态 done_event 与 all_done_event，
            提前退出时只有二者都满足的引擎才跳过提交
        """
        try:
            self.stats.start_time = time.monotonic()
            for keyword in self.keywords:
                if self._should_stop():
                    logger.warning("Shutdown requested, skipping remaining keywords")
                    break
                self._crawl_keyword(keyword)

            logger.info("全部任务完成！")

            # 生成采集报告
            self.stats.end_time = time.monotonic()
            report = self.stats.summary()
            report["keywords"] = self.keywords
            report["media_types"] = list(self.media_types)
            report["engines"] = self.search_engines
            report["shutdown"] = self._shutdown_requested.is_set()
            logger.info(f"Crawl stats: {json.dumps(report, ensure_ascii=False)}")

            # 保存报告到输出目录
            report_path = os.path.join(self.output_dir, "_crawl_report.json")
            try:
                os.makedirs(self.output_dir, exist_ok=True)
                with open(report_path, "w", encoding="utf-8") as f:
                    json.dump(report, f, ensure_ascii=False, indent=2)
                logger.info(f"Crawl report saved to {report_path}")
            except Exception as e:
                logger.warning(f"Failed to save crawl report: {e}")

            self._emit_callback(CrawlEvent(
                event_type="crawl_done", keyword="",
                media_type="", detail=report,
            ))
        finally:
            # 无论正常退出还是 KeyboardInterrupt / 异常，都确保资源被释放。
            # Bug fix: old code called close() only on the normal path, so a
            # Ctrl+C or unexpected exception would leak the browser process and
            # the dedup file handle.
            if self._renderer:
                try:
                    self._renderer.close()
                except Exception:
                    pass
            self._dedup.close()
            # 恢复原始信号处理器
            signal.signal(signal.SIGINT, self._original_sigint)
            signal.signal(signal.SIGTERM, self._original_sigterm)

    # 向下兼容旧接口
    def download_images(self):
        self.download()

    # ──────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────

    def clean_output(self, father_path: str):
        """删除损坏或格式不支持的图片文件。"""
        for dirpath, _, filenames in os.walk(father_path):
            for fname in filenames:
                if not fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".avif")):
                    continue
                fpath = os.path.join(dirpath, fname)
                try:
                    with Image.open(fpath) as img:
                        img.verify()
                    with Image.open(fpath) as img:
                        if img.format not in ("JPEG", "PNG", "WEBP"):
                            os.remove(fpath)
                            logger.info(f"已删除（格式 {img.format}）: {fpath}")
                except Exception:
                    try:
                        os.remove(fpath)
                        logger.info(f"已删除（损坏）: {fpath}")
                    except OSError as e:
                        logger.error(f"无法删除: {fpath}: {e}")
