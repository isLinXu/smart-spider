# coding=utf-8
"""数据集采集执行器：当前以图片下载为兼容入口，输出通用多模态 manifest。

架构总览
--------
                    ┌─────────────────────────────────────┐
                    │        DatasetCrawler                │
                    │  keywords × sources × total_count    │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ▼                    ▼                    ▼
     ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
     │  SearchSource    │  │  SiteSource      │  │  URLListSource   │
     │  (SmartSpider)  │  │  (SiteCrawler)   │  │  (直接URL列表)   │
     └────────┬────────┘  └────────┬─────────┘  └────────┬────────┘
              │                    │                      │
              └──────────┬─────────┘                      │
                         ▼                                │
              ┌──────────────────────┐                    │
              │  ImageDownloadQueue  │◄───────────────────┘
              │  (URL + meta)       │
              └──────────┬──────────┘
                         ▼
              ┌──────────────────────┐
              │  DownloadWorker       │
              │  - 下载图片            │
              │  - CLIP 过滤（可选）    │
              │  - 尺寸/方差过滤       │
              │  - 内容去重            │
              └──────────┬───────────┘
                         ▼
              ┌──────────────────────┐
              │  DatasetDirManager   │
              │  batch_0000/ (0-99)  │
              │  batch_0100/ (100-199)│
              │  batch_0200/ (200-299)│
              │  ...                 │
              └──────────────────────┘

输出目录结构
-----------
output_dir/
├── batch_0000/
│   ├── 0000_a1b2c3d4.jpg
│   ├── 0001_e5f6g7h8.png
│   ├── ...
│   └── 0099_z0y9x8w7.webp
├── batch_0100/
│   ├── 0100_m3n4o5p6.jpg
│   ├── ...
├── batch_0200/
│   └── ...
├── metadata.jsonl        # 旧图片采集元数据（每行一条）
├── manifest.jsonl        # 通用多模态样本事实来源
├── _dataset_report.json  # 采集报告
└── .dataset_progress.json # 断点续传状态

与 SmartSpider 的区别
--------------------
| 维度         | SmartSpider          | DatasetCrawler              |
|-------------|---------------------|----------------------------|
| 输出结构     | output/{kw}/image/  | batch_XXXX/ (每100张分桶)   |
| 目标数量     | max_items/关键词     | total_count (全局总量)       |
| CLIP 过滤    | 必须启用             | 可选（可关闭 CLIP 做全量采集）|
| 元数据       | 单独 .json 文件      | 统一 metadata.jsonl          |
| 断点续传     | URL 去重文件          | 进度文件 + URL 去重          |
| 多源         | 仅搜索引擎           | 搜索 + 站点 + URL 列表       |
| 编号         | MD5 哈希             | 全局递增序号 (0000-9999)     |
"""
import hashlib
import json
import os
import queue
import re
import signal
import tempfile
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

from .http_client import SmartHttpClient, ProxyPool
from .smart_spider import (
    CrawlEvent,
    CrawlStats,
    UrlDeduplicator,
    VideoDownloader,
    TextExtractor,
    _detect_ext,
    _SENTINEL,
)
from .engines import ENGINE_REGISTRY, MediaType, RenderMode, get_engine
from .site_crawler import SiteCrawler
from .site_parser import SiteParser, get_site_parser
from .dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelPolicy,
    Modality,
    ModalityAsset,
    QualityMetrics,
    SampleRecord,
)
from .dataset_state import DatasetStateStore

# torch / clip 延迟导入
try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False
    torch = None

try:
    import clip
    _CLIP_AVAILABLE = True
except ImportError:
    _CLIP_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

_BATCH_SIZE = 100          # 每个子目录存放的图片数量
_DEFAULT_DISK_GUARD_MB = 100
_QUEUE_PUT_TIMEOUT = 5.0
_DEFAULT_IMAGE_OUTPUT_FORMAT = "jpg"
_DEFAULT_JPEG_QUALITY = 95


# ──────────────────────────────────────────────────────────────────────────────
# 数据集目录管理器
# ──────────────────────────────────────────────────────────────────────────────

class DatasetDirManager:
    """管理数据集分桶目录，每 100 张图片一个子目录。

    目录命名规则：batch_0000/, batch_0100/, batch_0200/, ...
    文件命名规则：{全局序号:04d}_{url_hash[:8]}{ext}

    线程安全：所有公共方法通过内部锁保护。
    """

    def __init__(self, output_dir: str, batch_size: int = _BATCH_SIZE):
        self.output_dir = output_dir
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._saved_count = 0
        os.makedirs(output_dir, exist_ok=True)

    def get_save_path(self, url: str, ext: str) -> str:
        """获取图片保存路径，自动分配到正确的分桶目录。

        Returns:
            完整文件路径，如 /output/batch_0100/0123_a1b2c3d4.jpg
        """
        with self._lock:
            idx = self._saved_count
            self._saved_count += 1

        batch_dir = os.path.join(
            self.output_dir,
            f"batch_{(idx // self.batch_size) * self.batch_size:04d}",
        )
        os.makedirs(batch_dir, exist_ok=True)

        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        filename = f"{idx:04d}_{url_hash}{ext}"
        return os.path.join(batch_dir, filename)

    def save_content(
        self,
        url: str,
        ext: str,
        content: bytes,
        max_count: Optional[int] = None,
    ) -> Optional[tuple[int, str]]:
        """原子保存内容并返回 ``(index, path)``。

        与历史上的 ``get_save_path`` 不同，这个方法把目标数量检查、编号
        分配和文件提交放在同一把锁中。只有 ``os.replace`` 成功后计数器才
        增加，因此并发 worker 不会因为失败写入消耗编号，也不会超过全局
        目标数量。
        """
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")

        with self._lock:
            if max_count is not None and self._saved_count >= max_count:
                return None

            idx = self._saved_count
            batch_dir = os.path.join(
                self.output_dir,
                f"batch_{(idx // self.batch_size) * self.batch_size:04d}",
            )
            os.makedirs(batch_dir, exist_ok=True)
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            final_path = os.path.join(batch_dir, f"{idx:04d}_{url_hash}{ext}")
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=batch_dir,
                    prefix=f".{idx:04d}_",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = temp_file.name
                    temp_file.write(bytes(content))
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, final_path)
            except Exception:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise

            self._saved_count += 1
            return idx, final_path

    def increment(self) -> int:
        """递增计数器并返回当前值（线程安全）。"""
        with self._lock:
            idx = self._saved_count
            self._saved_count += 1
            return idx

    @property
    def saved_count(self) -> int:
        with self._lock:
            return self._saved_count

    def current_batch_dir(self) -> str:
        """返回当前分桶目录路径。"""
        with self._lock:
            idx = self._saved_count
        batch_name = f"batch_{(idx // self.batch_size) * self.batch_size:04d}"
        return os.path.join(self.output_dir, batch_name)

    def list_batches(self) -> list[str]:
        """列出所有已创建的分桶目录。"""
        batches = []
        for name in sorted(os.listdir(self.output_dir)):
            if name.startswith("batch_") and os.path.isdir(
                os.path.join(self.output_dir, name)
            ):
                batches.append(name)
        return batches

    def count_existing_images(self) -> int:
        """统计已保存的图片数量（扫描磁盘）。"""
        count = 0
        for name in os.listdir(self.output_dir):
            batch_dir = os.path.join(self.output_dir, name)
            if not os.path.isdir(batch_dir) or not name.startswith("batch_"):
                continue
            for fname in os.listdir(batch_dir):
                if fname.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")):
                    count += 1
        return count


# ──────────────────────────────────────────────────────────────────────────────
# 断点续传进度管理器
# ──────────────────────────────────────────────────────────────────────────────

class ProgressManager:
    """管理数据集爬取的断点续传状态。

    状态文件：{output_dir}/.dataset_progress.json
    格式：
    {
        "saved_count": 1234,
        "total_target": 5000,
        "keywords_done": ["cat", "dog"],
        "keywords_remaining": ["bird", "fish"],
        "last_update": "2026-06-16T17:00:00"
    }
    """

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, ".dataset_progress.json")
        self._lock = threading.Lock()
        self._data: dict = {}

    def load(self) -> dict:
        """加载进度文件。"""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(f"ProgressManager: loaded from {self._path}, "
                           f"saved_count={self._data.get('saved_count', 0)}")
            except Exception as e:
                logger.warning(f"ProgressManager: failed to load {self._path}: {e}")
                self._data = {}
        return self._data

    def save(self, saved_count: int, total_target: int,
             keywords_done: list[str], keywords_remaining: list[str]):
        """保存进度。"""
        from datetime import datetime
        with self._lock:
            self._data = {
                "saved_count": saved_count,
                "total_target": total_target,
                "keywords_done": keywords_done,
                "keywords_remaining": keywords_remaining,
                "last_update": datetime.now().isoformat(),
            }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"ProgressManager: failed to save: {e}")

    @property
    def saved_count(self) -> int:
        return self._data.get("saved_count", 0)

    @property
    def keywords_done(self) -> list[str]:
        return self._data.get("keywords_done", [])


# ──────────────────────────────────────────────────────────────────────────────
# 元数据写入器
# ──────────────────────────────────────────────────────────────────────────────

class MetadataWriter:
    """线程安全的元数据追加写入器。

    输出格式：JSONL（每行一条 JSON 记录）
    字段：index, url, file_path, batch, keyword, source, sim, width, height, ext
    """

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, "metadata.jsonl")
        self._lock = threading.Lock()
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, record: dict):
        """写入一条元数据记录。"""
        with self._lock:
            try:
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"MetadataWriter: write error: {e}")

    def close(self):
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


class ManifestWriter:
    """通用多模态 manifest 写入器。

    ``metadata.jsonl`` 是旧图像采集格式；这里的 ``manifest.jsonl`` 是新
    的任务事实来源，样本可以同时包含文本、图片和其他模态资产。
    """

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, "manifest.jsonl")
        self._lock = threading.Lock()
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, sample: SampleRecord):
        with self._lock:
            try:
                self._file.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"ManifestWriter: write error: {e}")

    def close(self):
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


# ──────────────────────────────────────────────────────────────────────────────
# 数据集爬取器
# ──────────────────────────────────────────────────────────────────────────────

class DatasetCrawler:
    """大规模数据集采集器的图片资源兼容执行器。

    核心特性
    --------
    1. 分桶存储：每 100 张图片一个子目录（batch_0000/, batch_0100/, ...）
    2. 全局递增编号：文件名包含序号，便于排序和引用
    3. 多源爬取：搜索引擎（SmartSpider）+ 站点深度（SiteCrawler）+ URL 列表
    4. CLIP 可选过滤：可关闭 CLIP 做全量采集，也可启用做精准过滤
    5. 断点续传：进度文件 + URL 去重持久化
    6. 兼容元数据：图片字段写入 metadata.jsonl
    7. 通用 manifest：图片样本以多模态 SampleRecord 写入 manifest.jsonl
    7. 优雅关停：SIGINT/SIGTERM 信号处理

    用法
    ----
    >>> crawler = DatasetCrawler(
    ...     keywords=["cat", "dog", "bird"],
    ...     total_count=1000,
    ...     output_dir="./dataset_animals",
    ...     use_clip=True,
    ...     similarity_threshold=0.22,
    ... )
    >>> crawler.crawl()
    """

    def __init__(
        self,
        keywords: list[str],
        total_count: int = 1000,
        output_dir: str = "./dataset_output",
        # 分桶参数
        batch_size: int = _BATCH_SIZE,
        # CLIP 参数
        use_clip: bool = True,
        similarity_threshold: float = 0.22,
        clip_model: str = "ViT-B/32",
        # 网络参数
        proxies: Optional[list[str]] = None,
        rate: float = 8.0,
        max_retries: int = 3,
        timeout: int = 10,
        max_workers: int = 20,
        # 图片过滤
        min_width: int = 200,
        min_height: int = 200,
        min_variance: float = 10.0,
        min_file_size: int = 1024,
        # 搜索引擎
        search_engines: Optional[list[str]] = None,
        # 站点深度爬取
        site_parsers: Optional[list[str]] = None,
        site_start_page: int = 1,
        site_end_page: int = 10,
        # spider_tools 桥接
        st_sites: Optional[list[str]] = None,
        st_tags: str = "",
        st_query: str = "",
        st_pages: Optional[list[int]] = None,
        st_limit_per_site: int = 0,
        # 断点续传
        resume: bool = False,
        # 数据集任务内核
        label_mode: str = "hybrid",
        labels: Optional[list[str]] = None,
        label_policy: Optional[LabelPolicy] = None,
        state_db: Optional[str] = None,
        job_id: Optional[str] = None,
        # 回调
        callbacks: Optional[list[Callable]] = None,
        # 磁盘保护
        disk_guard_mb: int = _DEFAULT_DISK_GUARD_MB,
        # 以图搜图过滤（追加到末尾，保持旧位置参数兼容）
        query_image: Optional[str] = None,
        image_similarity_threshold: float = 0.75,
        # 图片落盘格式（追加到末尾，保持旧位置参数兼容）
        image_output_format: Optional[str] = _DEFAULT_IMAGE_OUTPUT_FORMAT,
        jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    ):
        self.keywords = keywords
        self.total_count = total_count
        self.output_dir = output_dir
        self.batch_size = batch_size
        self.use_clip = use_clip
        self.similarity_threshold = similarity_threshold
        self.clip_model_name = clip_model
        self.query_image = query_image
        self.image_similarity_threshold = image_similarity_threshold
        if not -1.0 <= image_similarity_threshold <= 1.0:
            raise ValueError("image_similarity_threshold must be between -1 and 1")
        if image_output_format is None:
            self.image_output_format = None
        else:
            normalized_output_format = image_output_format.strip().lower().lstrip(".")
            if normalized_output_format in {"original", "source"}:
                self.image_output_format = None
            elif normalized_output_format in {"jpg", "jpeg"}:
                self.image_output_format = "jpg"
            else:
                raise ValueError(
                    "image_output_format must be 'jpg' (default), 'original', or None"
                )
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be between 1 and 100")
        self.jpeg_quality = jpeg_quality
        if query_image and (not use_clip or not _CLIP_AVAILABLE):
            raise RuntimeError(
                "query_image requires CLIP; remove --no-clip and install openai-clip"
            )
        self.max_workers = max_workers
        self.min_width = min_width
        self.min_height = min_height
        self.min_variance = min_variance
        self.min_file_size = min_file_size
        self._callbacks = callbacks or []
        self._disk_guard_mb = disk_guard_mb
        self.job_id = job_id or hashlib.sha256(
            os.path.abspath(output_dir).encode("utf-8")
        ).hexdigest()[:16]
        self.label_policy = label_policy or LabelPolicy(
            mode=label_mode,
            # 没有显式 labels 时，把当前搜索词作为默认正式标签；
            # 自动发现的额外标签仍按 hybrid 策略进入候选区。
            fixed_labels=labels if labels is not None else keywords,
        )

        # 优雅关停
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

        # 目录管理器
        self._dir_manager = DatasetDirManager(output_dir, batch_size)

        # 元数据写入器
        self._metadata_writer = MetadataWriter(output_dir)
        self._manifest_writer = ManifestWriter(output_dir)

        # 进度管理器
        self._progress = ProgressManager(output_dir)

        # SQLite 状态库：传入空字符串可显式关闭，默认写入输出目录。
        self._state_store = None
        if state_db != "":
            db_path = state_db or os.path.join(output_dir, ".dataset_state.sqlite3")
            self._state_store = DatasetStateStore(db_path)
            self._state_store.create_job(
                self.job_id,
                {
                    "keywords": keywords,
                    "total_count": total_count,
                    "label_policy": self.label_policy.to_dict(),
                },
            )

        # URL 去重
        _dedup_path = None
        if resume:
            os.makedirs(output_dir, exist_ok=True)
            _dedup_path = os.path.join(output_dir, ".seen_urls.jsonl")
        self._dedup = UrlDeduplicator(persist_path=_dedup_path, content_dedup=True)

        # 网络层
        self._proxy_pool = ProxyPool(proxies or [])
        self._http = SmartHttpClient(
            proxies=proxies or [],
            rate=rate,
            max_retries=max_retries,
            timeout=timeout,
        )

        # 搜索引擎
        if search_engines:
            self.search_engines = search_engines
        else:
            self.search_engines = [
                name for name, eng in ENGINE_REGISTRY.items()
                if eng.media_type == MediaType.IMAGE
            ]

        # 站点爬取参数
        self._site_parsers = site_parsers or []
        self._site_start_page = site_start_page
        self._site_end_page = site_end_page

        # spider_tools 桥接参数
        self._st_sites = st_sites or []
        self._st_tags = st_tags
        self._st_query = st_query
        self._st_pages = st_pages
        self._st_limit_per_site = st_limit_per_site

        # CLIP 模型（可选）
        self.model = None
        self.preprocess = None
        self.device = "cpu"
        self._clip_text_cache: dict[str, Optional[Any]] = {}
        self._clip_text_cache_lock = threading.Lock()
        self._clip_infer_lock = threading.Lock()
        self._image_query_feature = None

        if use_clip:
            if not _CLIP_AVAILABLE:
                logger.warning("CLIP not available, disabling CLIP filter. "
                             "Run: pip install git+https://github.com/openai/CLIP.git")
                self.use_clip = False
            else:
                self.device = "cuda" if torch.cuda.is_available() else "cpu"
                self.model, self.preprocess = clip.load(self.clip_model_name, device=self.device)
                self.model.eval()
                logger.info(f"CLIP model '{self.clip_model_name}' loaded on {self.device}")

        if self.query_image:
            if not self.use_clip or self.model is None or self.preprocess is None:
                raise RuntimeError(
                    "query_image requires CLIP; remove --no-clip and install openai-clip"
                )
            self._image_query_feature = self._encode_image_file(self.query_image)

        # 断点续传：恢复已保存数量
        if resume:
            self._progress.load()
            existing = self._dir_manager.count_existing_images()
            if existing > 0:
                logger.info(f"断点续传：已存在 {existing} 张图片，从第 {existing + 1} 张继续")
                # 同步目录管理器的计数器
                self._dir_manager._saved_count = existing

    def _should_stop(self) -> bool:
        return self._shutdown_requested.is_set() or not self._check_disk_space()

    def _check_disk_space(self) -> bool:
        try:
            import shutil
            usage = shutil.disk_usage(self.output_dir)
            free_mb = usage.free / (1024 * 1024)
            if free_mb < self._disk_guard_mb:
                logger.error(f"Disk space guard: only {free_mb:.0f} MB free, pausing")
                return False
        except Exception:
            pass
        return True

    def _emit_callback(self, event: CrawlEvent) -> None:
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception as e:
                logger.warning(f"Callback error: {e}")

    def _get_text_feature(self, keyword: str):
        """获取关键词的 CLIP 文本特征（缓存）。"""
        with self._clip_text_cache_lock:
            if keyword in self._clip_text_cache:
                return self._clip_text_cache[keyword]

        try:
            tok = clip.tokenize([keyword]).to(self.device)
            with torch.no_grad():
                tf = self.model.encode_text(tok)
                tf = tf / tf.norm(dim=-1, keepdim=True)
            result = tf
        except Exception as e:
            logger.error(f"Text encode error '{keyword}': {e}")
            result = None

        with self._clip_text_cache_lock:
            self._clip_text_cache[keyword] = result
        return result

    def _encode_image(self, image: Image.Image):
        """编码单张图片并返回归一化 CLIP 向量。"""
        if self.model is None or self.preprocess is None:
            return None
        img_tensor = self.preprocess(image).unsqueeze(0).to(self.device)
        # CLIP 模型在多个下载线程之间共享；串行化 forward，兼容 CPU/GPU
        # 及自定义模型后端的线程安全边界。
        with self._clip_infer_lock:
            with torch.no_grad():
                img_feat = self.model.encode_image(img_tensor)
                img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
        return img_feat

    def _encode_image_file(self, path: str):
        """读取查询图片并生成归一化 CLIP 向量。"""
        try:
            with Image.open(path) as image:
                return self._encode_image(image.convert("RGB"))
        except Exception as exc:
            raise ValueError(f"cannot encode query image '{path}': {exc}") from exc

    def _image_query_similarity(self, image: Image.Image) -> Optional[float]:
        """计算候选图片与查询图片的余弦相似度。"""
        if self._image_query_feature is None:
            return None
        image_feature = self._encode_image(image)
        if image_feature is None:
            return None
        return float(torch.nn.functional.cosine_similarity(
            image_feature,
            self._image_query_feature,
        ).item())

    # ──────────────────────────────────────────────────────────────
    # 图片下载 + 过滤 + 保存
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _query_contains_term(query: str, term: str) -> bool:
        """判断搜索词是否包含一个标签词或别名。"""
        if not term:
            return False
        if any(ord(char) > 127 for char in term):
            return term in query
        return re.search(
            rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
            query,
        ) is not None

    def _query_label_decisions(self, keyword: str) -> list[LabelDecision]:
        """从组合搜索词解析多个正式标签，未命中时保持旧兼容行为。"""
        query = self.label_policy._key(keyword)
        fixed_by_key = {
            self.label_policy._key(label): label
            for label in self.label_policy.fixed_labels
        }
        decisions: list[LabelDecision] = []
        seen: set[str] = set()

        def add(label: str, evidence: dict[str, Any]):
            key = self.label_policy._key(label)
            if key and key in fixed_by_key and key not in seen:
                seen.add(key)
                decisions.append(LabelDecision(
                    fixed_by_key[key], 1.0, "query", evidence,
                ))

        for key, label in fixed_by_key.items():
            if self._query_contains_term(query, key):
                add(label, {"query": keyword, "match": key})

        for alias, label in self.label_policy.aliases.items():
            if self._query_contains_term(query, alias):
                add(label, {"query": keyword, "alias": alias})

        if not decisions:
            decisions.append(LabelDecision(keyword, 1.0, "query"))
        return decisions

    def _prepare_output_image(
        self,
        image: Image.Image,
        source_content: bytes,
        source_ext: str,
    ) -> tuple[bytes, str, str, dict[str, Any]]:
        """在图片通过过滤后，按配置生成最终落盘内容。

        内容去重仍使用下载到的原始字节；这里只负责生成数据集最终保存的
        字节，因此 WebP/GIF 等源格式默认会以 JPEG 文件落盘。
        """
        source_format = (image.format or source_ext.lstrip(".") or "unknown").lower()
        if self.image_output_format is None:
            return source_content, source_ext, source_format, {
                "enabled": False,
                "from_format": source_format,
                "to_format": source_format,
            }

        output = BytesIO()
        image.save(
            output,
            format="JPEG",
            quality=self.jpeg_quality,
            optimize=True,
        )
        return output.getvalue(), ".jpg", "jpeg", {
            "enabled": True,
            "from_format": source_format,
            "to_format": "jpeg",
            "quality": self.jpeg_quality,
        }

    def _download_and_save(self, url: str, keyword: str, source: str) -> bool:
        """下载单张图片，经过过滤后保存到分桶目录。

        过滤链：URL去重 → 下载 → 格式检测 → 尺寸过滤 → 方差过滤 → 内容去重 → CLIP过滤 → 保存

        Returns:
            True 表示成功保存，False 表示跳过或失败
        """
        if self._dir_manager.saved_count >= self.total_count:
            return False

        if not self._dedup.claim(url):
            return False
        url_claimed = True
        content_claimed = False

        candidate_id = None
        if self._state_store is not None:
            try:
                candidate_id = self._state_store.add_candidate(
                    self.job_id,
                    CandidateResource(
                        url=url,
                        source=source,
                        query=keyword,
                        source_meta={"keyword": keyword},
                    ),
                )
            except Exception as state_err:
                logger.warning(f"State store candidate error: {state_err}")

        def reject(reason: str) -> bool:
            if candidate_id and self._state_store is not None:
                try:
                    self._state_store.reject_candidate(candidate_id, reason)
                except Exception as state_err:
                    logger.warning(f"State store reject error: {state_err}")
            return False

        def fail(error: str) -> bool:
            if candidate_id and self._state_store is not None:
                try:
                    self._state_store.fail_candidate(candidate_id, error)
                except Exception as state_err:
                    logger.warning(f"State store failure error: {state_err}")
            return False

        resp = None
        full_content = None
        try:
            peek, resp = self._http.get_stream(url, peek_bytes=8192)
            ext = _detect_ext(peek)
            if ext not in (".jpg", ".png", ".webp", ".gif", ".avif", ".avis"):
                return reject("unsupported_image_format")

            # 读取完整内容
            try:
                rest = b"".join(resp.iter_content(chunk_size=65536))
                full_content = peek + rest
            except Exception as read_err:
                logger.warning(f"Image body read failed: {url[:55]}: {read_err}")
                return fail(f"read_error: {read_err}")

            if len(full_content) < self.min_file_size:
                self.stats.inc_filtered("image")
                return reject("file_too_small")

            # 解码图片
            try:
                buf = BytesIO(full_content)
                img = Image.open(buf)
                img.verify()
                buf.seek(0)
                img = Image.open(buf).convert("RGB")
            except Exception:
                logger.debug(f"Image verification failed: {url[:55]}")
                self.stats.inc_failed("image")
                return reject("invalid_image")

            # 尺寸过滤
            if img.width < self.min_width or img.height < self.min_height:
                self.stats.inc_filtered("image")
                return reject("image_too_small")

            # 低方差过滤
            arr = np.array(img.resize((32, 32)))
            if np.std(arr) < self.min_variance:
                self.stats.inc_filtered("image")
                return reject("low_variance")

            # 内容去重
            if not self._dedup.claim_content(full_content):
                return reject("duplicate_content")
            content_claimed = True

            # CLIP 过滤（可选）
            sim = 0.0
            image_sim = None
            if self.use_clip and self.model is not None:
                text_feat = self._get_text_feature(keyword)
                if text_feat is not None:
                    try:
                        img_feat = self._encode_image(img)
                        sim = torch.nn.functional.cosine_similarity(
                            img_feat, text_feat
                        ).item()
                        if sim < self.similarity_threshold:
                            self.stats.inc_filtered("image")
                            self._emit_callback(CrawlEvent(
                                event_type="item_filtered", keyword=keyword,
                                media_type="image", url=url,
                                detail={"sim": round(sim, 4), "reason": "clip_low_sim"},
                            ))
                            return reject("clip_low_sim")
                    except Exception as e:
                        logger.debug(f"CLIP inference error: {e}")

            # 以图搜图过滤：关键词/站点负责发现候选，查询图片负责视觉二次筛选。
            if self._image_query_feature is not None:
                try:
                    image_sim = self._image_query_similarity(img)
                    if image_sim is None:
                        return reject("image_query_inference_error")
                    if image_sim < self.image_similarity_threshold:
                        self.stats.inc_filtered("image")
                        self._emit_callback(CrawlEvent(
                            event_type="item_filtered", keyword=keyword,
                            media_type="image", url=url,
                            detail={
                                "image_sim": round(image_sim, 4),
                                "image_similarity_threshold": self.image_similarity_threshold,
                                "reason": "image_query_low_sim",
                            },
                        ))
                        return reject("image_query_low_sim")
                except Exception as e:
                    logger.debug(f"Image query inference error: {e}")
                    return reject("image_query_inference_error")

            output_content, output_ext, output_format, format_conversion = (
                self._prepare_output_image(img, full_content, ext)
            )

            # 原子保存图片；再次在锁内检查全局目标，避免并发超量。
            saved = self._dir_manager.save_content(
                url,
                output_ext,
                output_content,
                max_count=self.total_count,
            )
            if saved is None:
                return reject("target_reached")
            idx, save_path = saved
            self._dedup.commit_content(full_content)
            content_claimed = False
            self._dedup.commit(url)
            url_claimed = False

            label_resolution = self.label_policy.resolve(
                self._query_label_decisions(keyword)
            )
            content_hash = hashlib.sha256(output_content).hexdigest()
            quality = QualityMetrics(
                modality=Modality.IMAGE.value,
                width=img.width,
                height=img.height,
                file_size=len(output_content),
                format=output_format,
                variance=float(np.var(arr)),
                validated=True,
            )

            sample = SampleRecord(
                sample_id=f"sha256:{content_hash}",
                file=os.path.relpath(save_path, self.output_dir),
                labels=label_resolution.labels,
                quality=quality,
                provenance={
                    "source": source,
                    "query": keyword,
                    "url": url,
                },
                pipeline={
                    "job_id": self.job_id,
                    "label_policy": self.label_policy.to_dict(),
                    "format_conversion": format_conversion,
                    "image_query": {
                        "path": self.query_image,
                        "similarity": round(image_sim, 4) if image_sim is not None else None,
                        "threshold": self.image_similarity_threshold,
                    } if self.query_image else None,
                    "status": "accepted",
                },
                modalities=[ModalityAsset(
                    modality=Modality.IMAGE,
                    role="image",
                    uri=os.path.relpath(save_path, self.output_dir),
                    mime_type=(
                        "image/jpeg"
                        if output_ext == ".jpg"
                        else f"image/{output_ext.lstrip('.') }"
                    ),
                )],
                task_type="image_classification",
            )

            if candidate_id and self._state_store is not None:
                try:
                    self._state_store.add_sample(
                        self.job_id,
                        sample,
                        candidate_id=candidate_id,
                        content_hash=content_hash,
                    )
                except Exception as state_err:
                    # 文件已经原子提交，状态库异常不能让样本被误报为下载失败。
                    logger.warning(f"State store sample error: {state_err}")

            self._manifest_writer.write(sample)

            # 写入元数据
            batch_name = f"batch_{(idx // self.batch_size) * self.batch_size:04d}"
            self._metadata_writer.write({
                "index": idx,
                "url": url,
                "file_path": save_path,
                "batch": batch_name,
                "keyword": keyword,
                "source": source,
                "sim": round(sim, 4),
                "image_sim": round(image_sim, 4) if image_sim is not None else None,
                "width": img.width,
                "height": img.height,
                "ext": output_ext,
                "source_ext": ext,
                "format_conversion": format_conversion,
                "labels": [item.to_dict() for item in label_resolution.labels],
                "label_candidates": [item.to_dict() for item in label_resolution.candidates],
                "quality": quality.to_dict(),
            })

            self.stats.inc_saved("image")
            self._emit_callback(CrawlEvent(
                event_type="item_saved", keyword=keyword,
                media_type="image", url=url,
                detail={
                    "sim": round(sim, 4),
                    "image_sim": round(image_sim, 4) if image_sim is not None else None,
                    "file": save_path,
                    "index": idx,
                },
            ))

            if idx % 100 == 0 and idx > 0:
                logger.info(f"已保存 {idx} 张图片 (目标: {self.total_count})")

            return True

        except Exception as e:
            logger.warning(f"Download error {url[:55]}: {e}")
            self.stats.inc_failed("image")
            return fail(str(e))
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
            if content_claimed and full_content is not None:
                self._dedup.release_content(full_content)
            if url_claimed:
                self._dedup.release(url)

    # ──────────────────────────────────────────────────────────────
    # 搜索引擎爬取
    # ──────────────────────────────────────────────────────────────

    def _crawl_search_engines(self, keyword: str, pbar: tqdm) -> int:
        """通过搜索引擎爬取指定关键词的图片。

        Returns:
            本次爬取保存的图片数量
        """
        saved_before = self._dir_manager.saved_count

        for engine_name in self.search_engines:
            if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                break

            eng = get_engine(engine_name)
            if eng.media_type != MediaType.IMAGE:
                continue

            # 估算需要的页数
            remaining = self.total_count - self._dir_manager.saved_count
            pages_needed = max(1, remaining // max(eng.page_step, 1) + 2)
            pages_needed = min(pages_needed, 50)

            logger.info(f"[{engine_name}] keyword='{keyword}', pages={pages_needed}")

            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}
                for page_offset in range(0, pages_needed * eng.page_step, eng.page_step):
                    if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                        break
                    url = eng.build_search_url(keyword, page_offset)
                    future = executor.submit(self._fetch_and_process_page, url, keyword, engine_name, pbar)
                    futures[future] = page_offset

                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        logger.debug(f"Page processing error: {e}")

        return self._dir_manager.saved_count - saved_before

    def _fetch_and_process_page(self, url: str, keyword: str, engine_name: str, pbar: tqdm):
        """获取搜索结果页并处理其中的图片。"""
        if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
            return

        eng = get_engine(engine_name)
        try:
            html = self._http.get_text(url, engine=engine_name)
            self.stats.inc_pages()
        except Exception as e:
            logger.debug(f"Fetch error {url[:55]}: {e}")
            return

        items = eng.extract_items(html)
        for item in items:
            if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                break
            img_url = item.get("url", "")
            if not img_url:
                continue
            if self._download_and_save(img_url, keyword, engine_name):
                with self._dir_manager._lock:
                    pbar.update(1)

    # ──────────────────────────────────────────────────────────────
    # 站点深度爬取
    # ──────────────────────────────────────────────────────────────

    def _crawl_site(self, parser_name: str, pbar: tqdm) -> int:
        """通过 SiteCrawler 爬取指定站点的图片。

        Returns:
            本次爬取保存的图片数量
        """
        saved_before = self._dir_manager.saved_count

        try:
            parser = get_site_parser(parser_name)
        except Exception as e:
            logger.error(f"Unknown site parser '{parser_name}': {e}")
            return 0

        # 使用 SiteCrawler 收集图片 URL
        site_crawler = SiteCrawler(
            site_parser=parser,
            output_dir=os.path.join(self.output_dir, f"_site_tmp_{parser_name}"),
            start_page=self._site_start_page,
            end_page=self._site_end_page,
            max_workers=self.max_workers,
            min_width=self.min_width,
            min_height=self.min_height,
        )

        # 收集文章
        articles = site_crawler.collect_articles()
        logger.info(f"[site:{parser_name}] collected {len(articles)} articles")

        for article in articles:
            if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                break

            image_urls = site_crawler.fetch_article_images(article)
            for img_url in image_urls:
                if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                    break
                if self._download_and_save(img_url, parser_name, f"site:{parser_name}"):
                    with self._dir_manager._lock:
                        pbar.update(1)

        return self._dir_manager.saved_count - saved_before

    # ──────────────────────────────────────────────────────────────
    # spider_tools 站点爬取
    # ──────────────────────────────────────────────────────────────

    def _crawl_spider_tools(self, pbar: tqdm) -> int:
        """通过 spider_tools 桥接爬取站点图片。

        Returns:
            本次爬取保存的图片数量
        """
        saved_before = self._dir_manager.saved_count

        try:
            from .spider_tools_bridge import SpiderToolsBridge
        except ImportError as e:
            logger.error(f"spider_tools bridge unavailable: {e}")
            return 0

        bridge = SpiderToolsBridge()
        try:
            urls = bridge.collect_urls_multi(
                sites=self._st_sites,
                tags=self._st_tags,
                query=self._st_query,
                pages=self._st_pages,
                limit_per_site=self._st_limit_per_site,
            )
        except Exception as e:
            logger.error(f"spider_tools collection failed: {e}")
            return 0

        logger.info(f"[spider_tools] collected {len(urls)} URLs from {self._st_sites}")

        for u in urls:
            if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                break
            keyword = u.tags[0] if u.tags else (self._st_tags or self._st_query or "")
            if self._download_and_save(u.url, keyword, f"st:{u.site}"):
                with self._dir_manager._lock:
                    pbar.update(1)

        return self._dir_manager.saved_count - saved_before

    # ──────────────────────────────────────────────────────────────
    # 主入口
    # ──────────────────────────────────────────────────────────────

    def close(self):
        """关闭元数据、去重和 SQLite 状态资源。"""
        self._metadata_writer.close()
        self._manifest_writer.close()
        self._dedup.close()
        if self._state_store is not None:
            self._state_store.close()
            self._state_store = None

    def crawl(self):
        """执行数据集爬取任务。"""
        try:
            self.stats.start_time = time.monotonic()

            # 计算剩余数量
            already_saved = self._dir_manager.saved_count
            remaining = self.total_count - already_saved
            if remaining <= 0:
                logger.info(f"已达到目标数量 {self.total_count}，无需继续爬取")
                return

            logger.info(f"数据集爬取开始: 目标={self.total_count}, 已有={already_saved}, "
                       f"剩余={remaining}, 关键词={self.keywords}")
            logger.info(f"搜索引擎: {self.search_engines}")
            logger.info(f"站点: {self._site_parsers}")
            logger.info(f"CLIP过滤: {'启用' if self.use_clip else '禁用'}")
            logger.info(f"分桶大小: {self.batch_size}")

            keywords_done = list(self._progress.keywords_done)
            keywords_remaining = [kw for kw in self.keywords if kw not in keywords_done]

            with tqdm(total=self.total_count, initial=already_saved,
                     desc="DatasetCrawler") as pbar:

                # 1. 搜索引擎爬取
                for keyword in keywords_remaining:
                    if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                        break

                    logger.info(f"开始爬取关键词: '{keyword}' "
                               f"(进度: {self._dir_manager.saved_count}/{self.total_count})")
                    self._crawl_search_engines(keyword, pbar)
                    keywords_done.append(keyword)

                    # 定期保存进度
                    self._progress.save(
                        saved_count=self._dir_manager.saved_count,
                        total_target=self.total_count,
                        keywords_done=keywords_done,
                        keywords_remaining=[kw for kw in self.keywords if kw not in keywords_done],
                    )

                # 2. 站点深度爬取
                for parser_name in self._site_parsers:
                    if self._should_stop() or self._dir_manager.saved_count >= self.total_count:
                        break

                    logger.info(f"开始站点爬取: '{parser_name}' "
                               f"(进度: {self._dir_manager.saved_count}/{self.total_count})")
                    self._crawl_site(parser_name, pbar)

                # 3. spider_tools 站点爬取
                if self._st_sites:
                    self._crawl_spider_tools(pbar)

            # 生成报告
            self._generate_report()

        finally:
            # 清理资源
            self.close()
            signal.signal(signal.SIGINT, self._original_sigint)
            signal.signal(signal.SIGTERM, self._original_sigterm)

    def _generate_report(self):
        """生成数据集采集报告。"""
        self.stats.end_time = time.monotonic()
        report = self.stats.summary()
        report["total_target"] = self.total_count
        report["keywords"] = self.keywords
        report["search_engines"] = self.search_engines
        report["site_parsers"] = self._site_parsers
        report["use_clip"] = self.use_clip
        report["image_output_format"] = self.image_output_format or "original"
        report["jpeg_quality"] = self.jpeg_quality
        report["batch_size"] = self.batch_size
        report["batches"] = self._dir_manager.list_batches()
        report["shutdown"] = self._shutdown_requested.is_set()

        report_path = os.path.join(self.output_dir, "_dataset_report.json")
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info(f"Dataset report saved to {report_path}")
        except Exception as e:
            logger.warning(f"Failed to save report: {e}")

        logger.info(f"数据集爬取完成! 共保存 {self._dir_manager.saved_count} 张图片")
        logger.info(f"分桶目录: {self._dir_manager.list_batches()}")
        logger.info(f"统计: {json.dumps(report, ensure_ascii=False)}")

        self._emit_callback(CrawlEvent(
            event_type="crawl_done", keyword="",
            media_type="image", detail=report,
        ))
