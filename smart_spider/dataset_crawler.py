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
import re
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union
from urllib.parse import urlsplit

import numpy as np
from loguru import logger
from PIL import Image
from tqdm import tqdm

from .http_client import SmartHttpClient, ProxyPool
from .image_safety import UnsafeImageError, decode_image_bytes
from .smart_spider import (
    CrawlEvent,
    CrawlStats,
    UrlDeduplicator,
    _detect_ext,
)
from .engines import ENGINE_REGISTRY, MediaType, get_engine
from .site_crawler import SiteCrawler
from .site_parser import get_site_parser
from .dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelMode,
    LabelPolicy,
    Modality,
    ModalityAsset,
    QualityMetrics,
    SampleRecord,
)
from .dataset_state import DatasetStateStore
from .dataset_repository import DatasetRepository
from .dataset_governance import QuotaLedger, SceneQuotaLedger, perceptual_fingerprint
from .dataset_config import DatasetCrawlConfig
from .dataset_layout import (
    DatasetDirManager,
    ManifestWriter,
    MetadataWriter,
    ProgressManager,
)
from .scene_quality import get_scene_quality_profile
from .scene_quality_gate import (
    GateDecision,
    JsonlSceneReviewQueue,
    SceneQualityGate,
    SceneSignalDetector,
)

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

    @classmethod
    def from_config(
        cls,
        config: DatasetCrawlConfig,
        *,
        callbacks: Optional[list[Callable]] = None,
    ) -> "DatasetCrawler":
        """Construct from a validated ``DatasetCrawlConfig``."""
        return cls(config=config, callbacks=callbacks)

    def __init__(
        self,
        keywords: Optional[list[str]] = None,
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
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
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
        # 大规模任务保护（追加到末尾，保持旧位置参数兼容）
        max_file_size: int = 12 * 1024 * 1024,
        max_image_pixels: int = 50_000_000,
        use_curl_cffi: Optional[bool] = None,
        allow_private_hosts: bool = False,
        max_inflight_pages: Optional[int] = None,
        max_inflight_downloads: Optional[int] = None,
        max_pending_candidates: Optional[int] = None,
        per_domain_concurrency: int = 2,
        memory_budget_mb: int = 512,
        scene_targets: Optional[Mapping[str, int]] = None,
        max_source_share: float = 1.0,
        max_domain_share: float = 1.0,
        scene_quality_gate_enabled: bool = False,
        scene_review_queue_path: Optional[str] = None,
        scene_signal_detector: Optional[SceneSignalDetector] = None,
        scene_quality_gate: Optional[SceneQualityGate] = None,
        config: Optional[DatasetCrawlConfig] = None,
    ):
        if config is not None:
            if keywords is not None:
                raise TypeError(
                    "DatasetCrawler() accepts either config=... or keyword args, not both"
                )
            cfg = config
            keywords = list(cfg.keywords)
            total_count = cfg.total_count
            output_dir = cfg.output_dir
            batch_size = cfg.batch_size
            use_clip = cfg.use_clip
            similarity_threshold = cfg.similarity_threshold
            clip_model = cfg.clip_model
            proxies = cfg.proxies
            rate = cfg.rate
            max_retries = cfg.max_retries
            timeout = cfg.timeout
            connect_timeout = cfg.connect_timeout
            read_timeout = cfg.read_timeout
            max_workers = cfg.max_workers
            min_width = cfg.min_width
            min_height = cfg.min_height
            min_variance = cfg.min_variance
            min_file_size = cfg.min_file_size
            search_engines = cfg.search_engines
            site_parsers = cfg.site_parsers
            site_start_page = cfg.site_start_page
            site_end_page = cfg.site_end_page
            st_sites = cfg.st_sites
            st_tags = cfg.st_tags
            st_query = cfg.st_query
            st_pages = cfg.st_pages
            st_limit_per_site = cfg.st_limit_per_site
            resume = cfg.resume
            label_mode = cfg.label_mode
            labels = cfg.labels
            label_policy = cfg.label_policy
            state_db = cfg.state_db
            job_id = cfg.job_id
            disk_guard_mb = cfg.disk_guard_mb
            query_image = cfg.query_image
            image_similarity_threshold = cfg.image_similarity_threshold
            image_output_format = cfg.image_output_format
            jpeg_quality = cfg.jpeg_quality
            max_file_size = cfg.max_file_size
            max_image_pixels = cfg.max_image_pixels
            use_curl_cffi = cfg.use_curl_cffi
            allow_private_hosts = cfg.allow_private_hosts
            max_inflight_pages = cfg.max_inflight_pages
            max_inflight_downloads = cfg.max_inflight_downloads
            max_pending_candidates = cfg.max_pending_candidates
            per_domain_concurrency = cfg.per_domain_concurrency
            memory_budget_mb = cfg.memory_budget_mb
            scene_targets = cfg.scene_targets
            max_source_share = cfg.max_source_share
            max_domain_share = cfg.max_domain_share
            scene_quality_gate_enabled = cfg.scene_quality_gate_enabled
            scene_review_queue_path = cfg.scene_review_queue_path
        else:
            if keywords is None:
                raise TypeError("keywords or config is required")
            cfg = DatasetCrawlConfig(
                keywords=list(keywords),
                total_count=total_count,
                output_dir=output_dir,
                batch_size=batch_size,
                use_clip=use_clip,
                similarity_threshold=similarity_threshold,
                clip_model=clip_model,
                proxies=proxies,
                rate=rate,
                max_retries=max_retries,
                timeout=timeout,
                connect_timeout=connect_timeout,
                read_timeout=read_timeout,
                max_workers=max_workers,
                min_width=min_width,
                min_height=min_height,
                min_variance=min_variance,
                min_file_size=min_file_size,
                search_engines=search_engines,
                site_parsers=site_parsers,
                site_start_page=site_start_page,
                site_end_page=site_end_page,
                st_sites=st_sites,
                st_tags=st_tags,
                st_query=st_query,
                st_pages=st_pages,
                st_limit_per_site=st_limit_per_site,
                resume=resume,
                label_mode=label_mode,
                labels=labels,
                label_policy=label_policy,
                state_db=state_db,
                job_id=job_id,
                disk_guard_mb=disk_guard_mb,
                query_image=query_image,
                image_similarity_threshold=image_similarity_threshold,
                image_output_format=image_output_format,
                jpeg_quality=jpeg_quality,
                max_file_size=max_file_size,
                max_image_pixels=max_image_pixels,
                use_curl_cffi=use_curl_cffi,
                allow_private_hosts=allow_private_hosts,
                max_inflight_pages=max_inflight_pages,
                max_inflight_downloads=max_inflight_downloads,
                max_pending_candidates=max_pending_candidates,
                per_domain_concurrency=per_domain_concurrency,
                memory_budget_mb=memory_budget_mb,
                scene_targets=dict(scene_targets or {}),
                max_source_share=max_source_share,
                max_domain_share=max_domain_share,
                scene_quality_gate_enabled=scene_quality_gate_enabled,
                scene_review_queue_path=scene_review_queue_path,
            )
        self.config = cfg
        self.keywords = keywords
        self.total_count = total_count
        if total_count <= 0:
            raise ValueError("total_count must be positive")
        self.scene_targets = self._normalize_scene_targets(scene_targets)
        if self.scene_targets and sum(self.scene_targets.values()) > total_count:
            raise ValueError("total_count cannot be lower than the sum of scene_targets")
        self.max_source_share = float(max_source_share)
        self.max_domain_share = float(max_domain_share)
        self.scene_quality_gate_enabled = bool(scene_quality_gate_enabled or scene_quality_gate)
        self.scene_signal_detector = scene_signal_detector
        self.scene_quality_gate = scene_quality_gate
        self.scene_review_queue: Optional[JsonlSceneReviewQueue] = None
        self._scene_quality_stats = {"accept": 0, "review": 0, "reject": 0}
        if self.scene_quality_gate_enabled:
            queue_path = scene_review_queue_path or os.path.join(
                output_dir, "scene_review_queue.jsonl"
            )
            self.scene_review_queue = JsonlSceneReviewQueue(queue_path)
            self.scene_review_queue_path = queue_path
        else:
            self.scene_review_queue_path = scene_review_queue_path
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
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if per_domain_concurrency <= 0:
            raise ValueError("per_domain_concurrency must be positive")
        if memory_budget_mb <= 0:
            raise ValueError("memory_budget_mb must be positive")
        self.max_workers = max_workers
        self.min_width = min_width
        self.min_height = min_height
        self.min_variance = min_variance
        self.min_file_size = min_file_size
        if max_file_size <= 0 or max_file_size < min_file_size:
            raise ValueError("max_file_size must be positive and >= min_file_size")
        if max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be positive")
        self.max_file_size = max_file_size
        self.max_image_pixels = int(max_image_pixels)
        self.memory_budget_bytes = int(memory_budget_mb * 1024 * 1024)
        if self.memory_budget_bytes < max_file_size:
            raise ValueError(
                "memory budget must accommodate at least one max_file_size response"
            )
        memory_limited_downloads = self.memory_budget_bytes // max_file_size
        configured_downloads = (
            max_inflight_downloads
            if max_inflight_downloads is not None
            else min(max_workers, memory_limited_downloads)
        )
        if configured_downloads <= 0:
            raise ValueError("max_inflight_downloads must be positive")
        self.max_inflight_downloads = min(int(configured_downloads), memory_limited_downloads)
        self.max_inflight_pages = int(max_inflight_pages or max_workers)
        if self.max_inflight_pages <= 0:
            raise ValueError("max_inflight_pages must be positive")
        self.max_pending_candidates = int(
            max_pending_candidates or self.max_inflight_downloads * 2
        )
        if self.max_pending_candidates <= 0:
            raise ValueError("max_pending_candidates must be positive")
        self.per_domain_concurrency = int(per_domain_concurrency)
        self._download_slots = threading.BoundedSemaphore(self.max_inflight_downloads)
        self._candidate_slots = threading.BoundedSemaphore(self.max_pending_candidates)
        self._domain_slots: dict[str, threading.BoundedSemaphore] = {}
        self._backpressure_lock = threading.Lock()
        self._inflight_downloads = 0
        self._inflight_candidates = 0
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
        self._record_backpressure()

        # 目录管理器
        self._dir_manager = DatasetDirManager(output_dir, batch_size)

        # 元数据写入器
        self._metadata_writer = MetadataWriter(output_dir)
        self._manifest_writer = ManifestWriter(output_dir)

        # 进度管理器
        self._progress = ProgressManager(output_dir)

        resumed_scene_counts, resumed_source_counts, resumed_domain_counts = (
            self._load_resumed_quota_counts() if resume else ({}, {}, {})
        )
        self._scene_quotas = SceneQuotaLedger(
            self.scene_targets, initial_counts=resumed_scene_counts
        )
        self._source_quotas = QuotaLedger(
            self.total_count,
            max_share=self.max_source_share,
            initial_counts=resumed_source_counts,
        )
        self._domain_quotas = QuotaLedger(
            self.total_count,
            max_share=self.max_domain_share,
            initial_counts=resumed_domain_counts,
        )

        # SQLite 状态库：传入空字符串可显式关闭，默认写入输出目录。
        self._state_store = None
        self._repository = None
        if state_db != "":
            db_path = state_db or os.path.join(output_dir, ".dataset_state.sqlite3")
            self._state_store = DatasetStateStore(db_path)
            self._state_store.create_job(
                self.job_id,
                {
                    "keywords": keywords,
                    "total_count": total_count,
                    "scene_targets": self.scene_targets,
                    "max_source_share": self.max_source_share,
                    "max_domain_share": self.max_domain_share,
                    "scene_quality_gate_enabled": self.scene_quality_gate_enabled,
                    "scene_review_queue_path": scene_review_queue_path,
                    "label_policy": self.label_policy.to_dict(),
                },
            )
            self._repository = DatasetRepository(
                output_dir,
                self._state_store,
                self.job_id,
                batch_size=batch_size,
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
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_response_bytes=max_file_size,
            use_curl_cffi=use_curl_cffi,
            allow_private_hosts=allow_private_hosts,
        )
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

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
                next_index = existing
                if self._repository is not None:
                    next_index = max(existing, self._repository.next_index)
                else:
                    next_index = max(existing, self._dir_manager.infer_next_index())
                self._dir_manager.set_existing_state(existing, next_index)

    def _should_stop(self) -> bool:
        return self._shutdown_requested.is_set() or not self._check_disk_space()

    @staticmethod
    def _normalize_scene_targets(
        scene_targets: Optional[Mapping[str, int]],
    ) -> dict[str, int]:
        if scene_targets is None:
            return {}
        if not isinstance(scene_targets, Mapping):
            raise ValueError("scene_targets must be a mapping of scene names to counts")
        result: dict[str, int] = {}
        for raw_name, raw_target in scene_targets.items():
            try:
                name = get_scene_quality_profile(str(raw_name)).name
            except ValueError:
                name = str(raw_name).strip()
            try:
                target = int(raw_target)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid scene target for {raw_name!r}") from exc
            if not name or target <= 0:
                raise ValueError("scene targets need non-empty names and positive counts")
            result[name] = result.get(name, 0) + target
        return result

    @staticmethod
    def _scene_key(keyword: str) -> str:
        try:
            return get_scene_quality_profile(keyword).name
        except ValueError:
            return keyword.strip()

    def _load_resumed_quota_counts(self) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        scene_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        domain_counts: dict[str, int] = {}
        path = Path(self.output_dir) / "metadata.jsonl"
        if not path.is_file():
            return scene_counts, source_counts, domain_counts
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                record = json.loads(line)
                scene = self._scene_key(str(record.get("keyword") or ""))
                if scene in self.scene_targets:
                    scene_counts[scene] = scene_counts.get(scene, 0) + 1
                source = str(record.get("source") or "").strip()
                if source:
                    source_counts[source] = source_counts.get(source, 0) + 1
                domain = (urlsplit(str(record.get("url") or "")).hostname or "").casefold()
                if domain:
                    domain_counts[domain] = domain_counts.get(domain, 0) + 1
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot restore quota counts from {path}: {exc}") from exc
        return scene_counts, source_counts, domain_counts

    def _scene_target_reached(self, keyword: str) -> bool:
        return self._scene_quotas.reached(self._scene_key(keyword))

    def _evaluate_scene_quality(
        self,
        scene_profile: Any,
        image: Image.Image,
        semantic_score: float,
    ) -> Optional[GateDecision]:
        """Evaluate the optional detector-backed safety gate before commit.

        The legacy image crawler still supports ordinary datasets.  The gate is
        therefore opt-in, but once enabled it is fail-closed: a configured scene
        without a profile or detector evidence can never be accepted silently.
        """
        if not self.scene_quality_gate_enabled:
            return None
        gate = self.scene_quality_gate
        if gate is None:
            if scene_profile is None:
                raise ValueError(
                    "scene quality gate requires a built-in/custom scene profile "
                    "or an injected SceneQualityGate"
                )
            gate = SceneQualityGate(scene_profile, detector=self.scene_signal_detector)
        if gate.detector is not None:
            return gate.evaluate_image(image, semantic_score)
        # Missing detector signals intentionally become a review decision for
        # absence-based safety scenes instead of being treated as acceptance.
        return gate.evaluate(semantic_score, signals={})

    def _record_scene_quality_review(
        self,
        sample_ref: str,
        decision: GateDecision,
    ) -> None:
        action = decision.action
        if action not in self._scene_quality_stats:
            self._scene_quality_stats[action] = 0
        self._scene_quality_stats[action] += 1
        if action == "review":
            if self.scene_review_queue is None:
                raise RuntimeError(
                    "scene quality review requires a configured review queue"
                )
            self.scene_review_queue.append(sample_ref, decision)

    def _record_backpressure(self, *, in_flight_pages: Optional[int] = None) -> None:
        """Expose bounded-work gauges without sampling response payloads."""
        try:
            import shutil
            disk_free_bytes = shutil.disk_usage(self.output_dir).free
        except OSError:
            disk_free_bytes = None
        with self._backpressure_lock:
            active_downloads = self._inflight_downloads
            active_candidates = self._inflight_candidates
        self.stats.set_resource_metrics(
            max_inflight_pages=self.max_inflight_pages,
            in_flight_pages=in_flight_pages,
            max_inflight_downloads=self.max_inflight_downloads,
            in_flight_downloads=active_downloads,
            max_pending_candidates=self.max_pending_candidates,
            in_flight_candidates=active_candidates,
            per_domain_concurrency=self.per_domain_concurrency,
            max_response_bytes=self.max_file_size,
            estimated_download_memory_bytes=active_downloads * self.max_file_size,
            memory_budget_bytes=self.memory_budget_bytes,
            disk_guard_bytes=self._disk_guard_mb * 1024 * 1024,
            disk_free_bytes=disk_free_bytes,
            configured_scene_targets=len(self.scene_targets),
            source_quota_max_share=self.max_source_share,
            domain_quota_max_share=self.max_domain_share,
        )

    def _acquire_candidate_slot(self) -> bool:
        while not self._candidate_slots.acquire(timeout=0.2):
            if self._should_stop():
                return False
        with self._backpressure_lock:
            self._inflight_candidates += 1
        self._record_backpressure()
        return True

    def _release_candidate_slot(self) -> None:
        with self._backpressure_lock:
            self._inflight_candidates = max(0, self._inflight_candidates - 1)
        self._candidate_slots.release()
        self._record_backpressure()

    def _domain_slot(self, url: str) -> threading.BoundedSemaphore:
        hostname = (urlsplit(url).hostname or "_unknown").casefold()
        with self._backpressure_lock:
            slot = self._domain_slots.get(hostname)
            if slot is None:
                slot = threading.BoundedSemaphore(self.per_domain_concurrency)
                self._domain_slots[hostname] = slot
            return slot

    def _acquire_download_slot(self, url: str) -> Optional[threading.BoundedSemaphore]:
        while not self._download_slots.acquire(timeout=0.2):
            if self._should_stop():
                return None
        domain_slot = self._domain_slot(url)
        while not domain_slot.acquire(timeout=0.2):
            if self._should_stop():
                self._download_slots.release()
                return None
        with self._backpressure_lock:
            self._inflight_downloads += 1
        self._record_backpressure()
        return domain_slot

    def _release_download_slot(self, domain_slot: threading.BoundedSemaphore) -> None:
        with self._backpressure_lock:
            self._inflight_downloads = max(0, self._inflight_downloads - 1)
        domain_slot.release()
        self._download_slots.release()
        self._record_backpressure()

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

        if (
            not decisions
            and self.label_policy.mode == LabelMode.FIXED
            and len(self.label_policy.fixed_labels) == 1
        ):
            add(self.label_policy.fixed_labels[0], {
                "query": keyword,
                "fixed_assignment": True,
            })

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
        scene_key = self._scene_key(keyword)
        scene_profile = None
        try:
            scene_profile = get_scene_quality_profile(scene_key)
        except ValueError:
            pass
        scene_semantic_threshold = (
            scene_profile.acceptance_score
            if scene_profile is not None
            else self.similarity_threshold
        )
        source_quota_key = str(source).strip()
        domain_quota_key = (urlsplit(url).hostname or "").casefold()
        scene_reserved = False
        source_reserved = False
        domain_reserved = False

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

        candidate_slot_acquired = self._acquire_candidate_slot()
        if not candidate_slot_acquired:
            return fail("crawl_stopped_before_candidate_processing")
        domain_slot = None
        resp = None
        full_content = None
        try:
            domain_slot = self._acquire_download_slot(url)
            if domain_slot is None:
                return fail("crawl_stopped_before_download")
            # The client is constructed with max_response_bytes=max_file_size.
            # Keep the call signature compatible with injected test/custom
            # clients while retaining the HTTP-level hard limit.
            peek, resp = self._http.get_stream(url, peek_bytes=8192)
            ext = _detect_ext(peek)
            if ext not in (".jpg", ".png", ".webp", ".gif", ".avif", ".avis"):
                return reject("unsupported_image_format")

            response_headers = getattr(resp, "headers", {}) or {}
            content_length = response_headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > self.max_file_size:
                        self.stats.inc_filtered("image")
                        return reject("file_too_large")
                except (TypeError, ValueError):
                    pass

            # 读取完整内容
            try:
                chunks = [peek]
                total_bytes = len(peek)
                for chunk in resp.iter_content(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > self.max_file_size:
                        self.stats.inc_filtered("image")
                        return reject("file_too_large")
                    chunks.append(chunk)
                full_content = b"".join(chunks)
            except Exception as read_err:
                logger.warning(f"Image body read failed: {url[:55]}: {read_err}")
                return fail(f"read_error: {read_err}")

            if len(full_content) < self.min_file_size:
                self.stats.inc_filtered("image")
                return reject("file_too_small")

            # Decode under the shared decompression-bomb and pixel budget.
            try:
                img = decode_image_bytes(
                    full_content, max_pixels=self.max_image_pixels
                )
            except UnsafeImageError:
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
                        if sim < scene_semantic_threshold:
                            self.stats.inc_filtered("image")
                            self._emit_callback(CrawlEvent(
                                event_type="item_filtered", keyword=keyword,
                                media_type="image", url=url,
                                detail={
                                    "sim": round(sim, 4),
                                    "threshold": scene_semantic_threshold,
                                    "reason": "clip_low_sim",
                                    "scene_profile": scene_profile.name if scene_profile else None,
                                },
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

            scene_quality_decision = self._evaluate_scene_quality(
                scene_profile, img, sim
            )
            if scene_quality_decision is not None:
                self._record_scene_quality_review(url, scene_quality_decision)
                self._emit_callback(CrawlEvent(
                    event_type="scene_quality_decision",
                    keyword=keyword,
                    media_type="image",
                    url=url,
                    detail=scene_quality_decision.to_dict(),
                ))
                if scene_quality_decision.action != "accept":
                    return reject(
                        "scene_quality_review"
                        if scene_quality_decision.action == "review"
                        else "scene_quality_reject"
                    )

            output_content, output_ext, output_format, format_conversion = (
                self._prepare_output_image(img, full_content, ext)
            )

            # Reserve every quota immediately before materialization.  The
            # reservation is released in finally on any failed commit.
            if not self._scene_quotas.try_reserve(scene_key):
                return reject("scene_target_reached")
            scene_reserved = scene_key in self.scene_targets
            if not self._source_quotas.try_reserve(source_quota_key):
                return reject("source_quota_reached")
            source_reserved = bool(source_quota_key)
            if not self._domain_quotas.try_reserve(domain_quota_key):
                return reject("domain_quota_reached")
            domain_reserved = bool(domain_quota_key)

            label_resolution = self.label_policy.resolve(
                self._query_label_decisions(keyword)
            )
            content_hash = hashlib.sha256(output_content).hexdigest()
            fingerprint = perceptual_fingerprint(img)
            quality = QualityMetrics(
                modality=Modality.IMAGE.value,
                width=img.width,
                height=img.height,
                file_size=len(output_content),
                format=output_format,
                variance=float(np.var(arr)),
                phash=fingerprint.phash,
                validated=True,
                attributes={
                    "dhash": fingerprint.dhash,
                    "scene_quality": scene_profile.to_dict() if scene_profile else None,
                    "scene_semantic_threshold": scene_semantic_threshold,
                    "scene_quality_gate": (
                        scene_quality_decision.to_dict()
                        if scene_quality_decision is not None
                        else None
                    ),
                },
            )

            def build_records(
                idx: int,
                save_path: str,
                relative_path: str,
                final_hash: str,
            ) -> tuple[SampleRecord, dict[str, Any]]:
                sample = SampleRecord(
                    sample_id=f"sha256:{final_hash}",
                    file=relative_path,
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
                        uri=relative_path,
                        mime_type=(
                            "image/jpeg"
                            if output_ext == ".jpg"
                            else f"image/{output_ext.lstrip('.') }"
                        ),
                    )],
                    task_type="image_classification",
                )
                metadata = {
                    "index": idx,
                    "url": url,
                    "file_path": save_path,
                    "sha256": final_hash,
                    "batch": Path(relative_path).parent.name,
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
                    "scene_quality_gate": (
                        scene_quality_decision.to_dict()
                        if scene_quality_decision is not None
                        else None
                    ),
                }
                return sample, metadata

            if self._repository is not None:
                commit = self._repository.commit_image(
                    output_content,
                    source_content=full_content,
                    url=url,
                    extension=output_ext,
                    candidate_id=candidate_id,
                    max_count=self.total_count,
                    build_records=build_records,
                )
                if commit.status != "committed":
                    return reject(
                        "target_reached" if commit.status == "target_reached" else "duplicate_content"
                    )
                idx = int(commit.index or 0)
                save_path = commit.path
                sample = commit.sample
                if sample is None:
                    return fail("repository_commit_missing_sample")
                self._dir_manager.record_repository_commit(idx)
            else:
                # 兼容无 SQLite 状态库的旧执行模式。
                saved = self._dir_manager.save_content(
                    url,
                    output_ext,
                    output_content,
                    max_count=self.total_count,
                )
                if saved is None:
                    return reject("target_reached")
                idx, save_path = saved
                relative_path = os.path.relpath(save_path, self.output_dir)
                sample, metadata = build_records(
                    idx, save_path, relative_path, content_hash
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
                self._metadata_writer.write(metadata)

            self._dedup.commit_content(full_content)
            content_claimed = False
            self._dedup.commit(url)
            url_claimed = False
            if scene_reserved:
                self._scene_quotas.commit(scene_key)
                scene_reserved = False
            if source_reserved:
                self._source_quotas.commit(source_quota_key)
                source_reserved = False
            if domain_reserved:
                self._domain_quotas.commit(domain_quota_key)
                domain_reserved = False

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
            if domain_reserved:
                self._domain_quotas.release(domain_quota_key)
            if source_reserved:
                self._source_quotas.release(source_quota_key)
            if scene_reserved:
                self._scene_quotas.release(scene_key)
            if url_claimed:
                self._dedup.release(url)
            if domain_slot is not None:
                self._release_download_slot(domain_slot)
            if candidate_slot_acquired:
                self._release_candidate_slot()

    # ──────────────────────────────────────────────────────────────
    # 搜索引擎爬取
    # ──────────────────────────────────────────────────────────────

    def _ordered_search_engines(self) -> list[str]:
        """Prioritize engines with observed page success and candidate yield."""
        names = list(self.search_engines)
        snapshot = self.stats.summary().get("source_metrics", {})
        if not snapshot:
            return names
        position = {name: index for index, name in enumerate(names)}

        def key(name: str) -> tuple[int, float, float, int]:
            row = snapshot.get(name)
            if not row or not row.get("attempts"):
                return (1, 0.0, 0.0, position[name])
            attempts = max(int(row["attempts"]), 1)
            yield_per_attempt = float(row.get("candidates", 0)) / attempts
            return (
                0,
                -float(row.get("success_rate", 0.0)),
                -yield_per_attempt,
                position[name],
            )

        return sorted(names, key=key)

    def _crawl_search_engines(self, keyword: str, pbar: tqdm) -> int:
        """通过搜索引擎爬取指定关键词的图片。

        Returns:
            本次爬取保存的图片数量
        """
        saved_before = self._dir_manager.saved_count

        for engine_name in self._ordered_search_engines():
            if (
                self._should_stop()
                or self._dir_manager.saved_count >= self.total_count
                or self._scene_target_reached(keyword)
            ):
                break

            eng = get_engine(engine_name)
            if eng.media_type != MediaType.IMAGE:
                continue

            # 估算需要的页数
            remaining = self.total_count - self._dir_manager.saved_count
            pages_needed = max(1, remaining // max(eng.page_step, 1) + 2)
            pages_needed = min(pages_needed, 50)

            logger.info(f"[{engine_name}] keyword='{keyword}', pages={pages_needed}")

            page_offsets = iter(range(0, pages_needed * eng.page_step, eng.page_step))
            page_workers = min(self.max_workers, self.max_inflight_pages)
            page_window = page_workers
            with ThreadPoolExecutor(max_workers=page_workers) as executor:
                futures = set()

                def submit_next_page() -> bool:
                    if (
                        self._should_stop()
                        or self._dir_manager.saved_count >= self.total_count
                        or self._scene_target_reached(keyword)
                    ):
                        return False
                    try:
                        page_offset = next(page_offsets)
                    except StopIteration:
                        return False
                    url = eng.build_search_url(keyword, page_offset)
                    futures.add(executor.submit(
                        self._fetch_and_process_page, url, keyword, engine_name, pbar
                    ))
                    self._record_backpressure(in_flight_pages=len(futures))
                    return True

                while len(futures) < page_window and submit_next_page():
                    pass
                while futures:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    futures.difference_update(done)
                    for future in done:
                        try:
                            future.result()
                        except Exception as exc:
                            logger.debug(f"Page processing error: {exc}")
                        submit_next_page()
                    self._record_backpressure(in_flight_pages=len(futures))

        return self._dir_manager.saved_count - saved_before

    def _fetch_and_process_page(self, url: str, keyword: str, engine_name: str, pbar: tqdm):
        """获取搜索结果页并处理其中的图片。"""
        if (
            self._should_stop()
            or self._dir_manager.saved_count >= self.total_count
            or self._scene_target_reached(keyword)
        ):
            return

        eng = get_engine(engine_name)
        fetch_started = time.monotonic()
        try:
            html = self._http.get_text(url, engine=engine_name)
            self.stats.inc_pages()
        except Exception as e:
            logger.debug(f"Fetch error {url[:55]}: {e}")
            self.stats.observe_source(
                engine_name, success=False, error=str(e)
            )
            return

        items = eng.extract_items(html)
        discovered_count = len(items)
        # A malformed search page can advertise thousands of URLs.  Only keep
        # one bounded candidate window; later pages remain available if the
        # current window is filtered out.
        if discovered_count > self.max_pending_candidates:
            items = items[:self.max_pending_candidates]
        self.stats.observe_source(
            engine_name,
            success=True,
            candidates=discovered_count,
            elapsed_ms=(time.monotonic() - fetch_started) * 1000,
        )
        for item in items:
            if (
                self._should_stop()
                or self._dir_manager.saved_count >= self.total_count
                or self._scene_target_reached(keyword)
            ):
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
            self.stats.observe_source(
                f"site:{parser_name}", success=False, error=str(e)
            )
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
            timeout=self.timeout,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            max_image_bytes=self.max_file_size,
            max_image_pixels=self.max_image_pixels,
        )

        # 收集文章
        started = time.monotonic()
        try:
            articles = site_crawler.collect_articles()
        except Exception as exc:
            logger.error(f"[site:{parser_name}] collection failed: {exc}")
            self.stats.observe_source(
                f"site:{parser_name}",
                success=False,
                elapsed_ms=(time.monotonic() - started) * 1000,
                error=str(exc),
            )
            return 0
        self.stats.observe_source(
            f"site:{parser_name}",
            success=True,
            candidates=len(articles),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
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
            self.stats.observe_source(
                "spider_tools", success=False, error=str(e)
            )
            return 0

        self.stats.observe_source(
            "spider_tools", success=True, candidates=len(urls)
        )
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
        try:
            self._http.close()
        except Exception:
            pass
        if self._repository is not None:
            self._repository.close()
            self._repository = None
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
        report["max_file_size"] = self.max_file_size
        report["scene_targets"] = self._scene_quotas.snapshot()
        report["source_quotas"] = self._source_quotas.snapshot()
        report["domain_quotas"] = self._domain_quotas.snapshot()
        report["batch_size"] = self.batch_size
        report["batches"] = self._dir_manager.list_batches()
        report["shutdown"] = self._shutdown_requested.is_set()
        report["http_metrics"] = self._http.metrics.snapshot()
        if self._repository is not None:
            report["repository_recovery"] = dict(self._repository.recovery_report)

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
