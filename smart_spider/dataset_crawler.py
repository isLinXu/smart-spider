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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from loguru import logger
from PIL import Image
from tqdm import tqdm

from .http_client import SmartHttpClient, ProxyPool
from .smart_spider import (
    CrawlEvent,
    CrawlStats,
    UrlDeduplicator,
)
from .engines import ENGINE_REGISTRY, MediaType, get_engine
from .site_crawler import SiteCrawler
from .site_parser import get_site_parser
from .dataset_contracts import (
    LabelDecision,
    LabelMode,
    LabelPolicy,
)
from .dataset_lineage import build_lineage
from .dataset_job_report import (
    attach_dataset_lineage,
    attach_dataset_publish_bundle,
    merge_dataset_crawl_stats,
    write_dataset_report_json,
)
from .dataset_state import DatasetStateStore
from .dataset_repository import DatasetRepository
from .dataset_governance import QuotaLedger, SceneQuotaLedger
from .dataset_config import DatasetCrawlConfig
from .dataset_layout import (
    DatasetDirManager,
    ManifestWriter,
    MetadataWriter,
    ProgressManager,
)
from .dataset_discovery import (
    DiscoveredImage,
    collection_limit_reached,
    discover_page_images,
    drain_inflight_window,
    estimate_pages_needed,
    order_search_engines,
    page_offsets,
    urls_to_discovered,
)
from .dataset_crawl_plan import (
    keyword_progress_snapshot,
    remaining_keywords,
    remaining_quota,
    site_crawler_kwargs,
    spider_tools_to_discovered,
)
from .dataset_download_window import DownloadWindow
from .dataset_download_session import ImageDownloadSession
from .dataset_scene_admission import (
    admit_ingested_image,
    evaluate_scene_quality,
    normalize_scene_targets,
    scene_key,
    scene_semantic_threshold,
)
from .dataset_image_ingest import ingest_remote_image, prepare_output_image
from .dataset_image_commit import materialize_accepted_image, reserve_ingest_quotas
from .dataset_semantic_gate import evaluate_semantic_filters
from .dataset_sample_builder import (
    AcceptedImageFacts,
    bind_record_builder,
    build_image_quality_metrics,
)
from .scene_quality import get_scene_quality_profile
from .scene_quality_gate import (
    GateDecision,
    JsonlSceneReviewQueue,
    SceneQualityGate,
    SceneSignalDetector,
)
from .scene_signal_detector import default_scene_signal_detector

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
        self.scene_targets = normalize_scene_targets(scene_targets)
        if self.scene_targets and sum(self.scene_targets.values()) > total_count:
            raise ValueError("total_count cannot be lower than the sum of scene_targets")
        self.max_source_share = float(max_source_share)
        self.max_domain_share = float(max_domain_share)
        self.scene_quality_gate_enabled = bool(scene_quality_gate_enabled or scene_quality_gate)
        self.scene_signal_detector = scene_signal_detector
        self.scene_quality_gate = scene_quality_gate
        if self.scene_quality_gate_enabled and self.scene_quality_gate is None:
            scene_names = set(self.scene_targets)
            scene_names.update(
                scene_key(keyword) for keyword in (keywords or [])
            )
            unknown_scenes = []
            for scene_name in scene_names:
                try:
                    get_scene_quality_profile(scene_name)
                except ValueError:
                    unknown_scenes.append(str(scene_name))
            if unknown_scenes:
                raise ValueError(
                    "scene quality gate requires a built-in scene profile or an "
                    "injected SceneQualityGate; unknown scenes: "
                    + ", ".join(sorted(unknown_scenes))
                )
            if self.scene_signal_detector is None:
                detector_only_profiles = {"pedestrian", "road_vehicle"}
                prefer_yolo = bool(scene_names & detector_only_profiles)
                self.scene_signal_detector = default_scene_signal_detector(
                    prefer_yolo=prefer_yolo,
                    yolo_model=os.environ.get(
                        "SMART_SPIDER_YOLO_MODEL", "yolo11n.pt"
                    ),
                    include_synthetic=prefer_yolo,
                    include_style=prefer_yolo,
                    synthetic_model=(
                        os.environ.get("SMART_SPIDER_SYNTHETIC_MODEL") or None
                    ),
                    synthetic_config=(
                        os.environ.get("SMART_SPIDER_SYNTHETIC_CONFIG") or None
                    ),
                    synthetic_cache_dir=(
                        os.environ.get("SMART_SPIDER_SYNTHETIC_CACHE_DIR") or None
                    ),
                    style_device="auto",
                    style_cache_dir=(
                        os.environ.get("SMART_SPIDER_STYLE_CACHE_DIR") or ".filter_cache"
                    ),
                )
                if prefer_yolo:
                    versions = self.scene_signal_detector.model_versions
                    if "yolo_model" not in versions:
                        raise RuntimeError(
                            "pedestrian/road_vehicle scene gates require a runnable "
                            "YOLO detector; install ultralytics and provide local weights"
                        )
        self.scene_review_queue: Optional[JsonlSceneReviewQueue] = None
        self._scene_quality_stats = {"accept": 0, "review": 0, "reject": 0}
        self.last_publish_checklist = None
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
        self._window: Optional[DownloadWindow] = None
        self._callbacks = callbacks or []
        self._disk_guard_mb = disk_guard_mb
        self.job_id = job_id or hashlib.sha256(
            os.path.abspath(output_dir).encode("utf-8")
        ).hexdigest()[:16]
        snapshot = dict(self.config.to_dict())
        snapshot["job_id"] = self.job_id
        self._config_snapshot = snapshot
        bootstrap_lineage = build_lineage(
            job_id=self.job_id,
            output_dir=output_dir,
            config_snapshot=snapshot,
            clip_model=clip_model,
        )
        self._config_fingerprint = bootstrap_lineage.config_fingerprint
        self._dataset_id = bootstrap_lineage.dataset_id
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

        self._window = DownloadWindow(
            max_inflight_downloads=self.max_inflight_downloads,
            max_pending_candidates=self.max_pending_candidates,
            per_domain_concurrency=self.per_domain_concurrency,
            should_stop=self._should_stop,
            on_change=lambda: self._record_backpressure(),
        )

        # 采集统计
        self.stats = CrawlStats()
        self._record_backpressure()

        # 目录管理器
        self._dir_manager = DatasetDirManager(output_dir, batch_size)

        # JSONL compatibility writers are opened only after repository recovery;
        # DatasetRepository may atomically replace these files on startup.
        self._metadata_writer = None
        self._manifest_writer = None

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
                    "scene_review_queue_path": self.scene_review_queue_path,
                    "label_policy": self.label_policy.to_dict(),
                },
            )
            self._repository = DatasetRepository(
                output_dir,
                self._state_store,
                self.job_id,
                batch_size=batch_size,
            )
            self._lease_recoveries = int(
                self._state_store.recover_expired_leases(self.job_id)
            )
        else:
            self._lease_recoveries = 0

        self._metadata_writer = MetadataWriter(output_dir)
        self._manifest_writer = ManifestWriter(output_dir)

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

    def _collection_limit_reached(self, keyword: Optional[str] = None) -> bool:
        return collection_limit_reached(
            stopped=self._should_stop(),
            saved_count=self._dir_manager.saved_count,
            total_count=self.total_count,
            scene_target_reached=(
                self._scene_target_reached(keyword) if keyword is not None else False
            ),
        )

    def _save_discovered_images(
        self,
        discovered: Iterable[DiscoveredImage],
        pbar: tqdm,
        *,
        check_scene: bool = True,
    ) -> None:
        for item in discovered:
            halt_keyword = item.keyword if check_scene else None
            if self._collection_limit_reached(halt_keyword):
                break
            if self._download_and_save(item.url, item.keyword, item.source):
                with self._dir_manager._lock:
                    pbar.update(1)

    @staticmethod
    def _normalize_scene_targets(
        scene_targets: Optional[Mapping[str, int]],
    ) -> dict[str, int]:
        return normalize_scene_targets(scene_targets)

    @staticmethod
    def _scene_key(keyword: str) -> str:
        return scene_key(keyword)

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
        return evaluate_scene_quality(
            enabled=self.scene_quality_gate_enabled,
            gate=self.scene_quality_gate,
            scene_profile=scene_profile,
            detector=self.scene_signal_detector,
            image=image,
            semantic_score=semantic_score,
        )

    def _scene_semantic_threshold(self, scene_profile: Any) -> float:
        return scene_semantic_threshold(
            gate_enabled=self.scene_quality_gate_enabled,
            gate=self.scene_quality_gate,
            scene_profile=scene_profile,
            default_threshold=self.similarity_threshold,
        )

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
        window = self._window
        active_downloads = 0 if window is None else window.inflight_downloads
        active_candidates = 0 if window is None else window.inflight_candidates
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
        return self._window.acquire_candidate_slot()

    def _release_candidate_slot(self) -> None:
        self._window.release_candidate_slot()

    def _domain_slot(self, url: str) -> threading.BoundedSemaphore:
        return self._window.domain_slot(url)

    def _acquire_download_slot(self, url: str) -> Optional[threading.BoundedSemaphore]:
        return self._window.acquire_download_slot(url)

    def _release_download_slot(self, domain_slot: threading.BoundedSemaphore) -> None:
        self._window.release_download_slot(domain_slot)

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

    def _clip_cosine_similarity(self, left, right) -> float:
        return float(torch.nn.functional.cosine_similarity(left, right).item())

    def _evaluate_semantic_filters(self, image, keyword: str, *, scene_profile: Any, threshold: float):
        return evaluate_semantic_filters(
            clip_enabled=bool(self.use_clip and self.model is not None),
            encode_image=self._encode_image,
            get_text_feature=self._get_text_feature,
            cosine_similarity=self._clip_cosine_similarity,
            image=image,
            keyword=keyword,
            clip_threshold=threshold,
            scene_profile_name=scene_profile.name if scene_profile else None,
            image_query_enabled=self._image_query_feature is not None,
            image_query_similarity=self._image_query_similarity,
            image_query_threshold=self.image_similarity_threshold,
        )

    def _admit_ingested_image(
        self,
        image: Image.Image,
        keyword: str,
        *,
        scene_profile: Any,
        threshold: float,
    ):
        semantic = self._evaluate_semantic_filters(
            image,
            keyword,
            scene_profile=scene_profile,
            threshold=threshold,
        )
        return admit_ingested_image(
            semantic,
            scene_evaluator=lambda score: self._evaluate_scene_quality(
                scene_profile, image, score
            ),
        )

    def _apply_admission_events(self, admission, *, keyword: str, url: str) -> None:
        if admission.inc_filtered:
            self.stats.inc_filtered("image")
            self._emit_callback(CrawlEvent(
                event_type="item_filtered",
                keyword=keyword,
                media_type="image",
                url=url,
                detail=admission.event_detail,
            ))
        if admission.scene_decision is not None:
            self._record_scene_quality_review(url, admission.scene_decision)
            self._emit_callback(CrawlEvent(
                event_type="scene_quality_decision",
                keyword=keyword,
                media_type="image",
                url=url,
                detail=admission.scene_decision.to_dict(),
            ))

    # Download-session steps consumed by `_download_and_save`.
    def _begin_image_session(
        self, url: str, keyword: str, source: str
    ) -> Optional[ImageDownloadSession]:
        session = ImageDownloadSession(
            url=url,
            keyword=keyword,
            source=source,
            job_id=self.job_id,
            dedup=self._dedup,
            window=self._window,
            state_store=self._state_store,
        )
        if not session.claim_url():
            return None
        session.register_candidate()
        return session

    def _scene_context(self, keyword: str) -> tuple[str, Any, float]:
        key = self._scene_key(keyword)
        profile = None
        try:
            profile = get_scene_quality_profile(key)
        except ValueError:
            pass
        return key, profile, self._scene_semantic_threshold(profile)

    def _ingest_session_image(self, session: ImageDownloadSession):
        ingested = ingest_remote_image(
            self._http,
            session.url,
            max_file_size=self.max_file_size,
            max_image_pixels=self.max_image_pixels,
            min_file_size=self.min_file_size,
            min_width=self.min_width,
            min_height=self.min_height,
            min_variance=self.min_variance,
        )
        session.attach_response(ingested.response)
        return ingested

    def _accepted_ingest(self, session: ImageDownloadSession, ingested):
        if not ingested.ok:
            if ingested.inc_filtered:
                self.stats.inc_filtered("image")
            if ingested.inc_failed:
                self.stats.inc_failed("image")
            if ingested.status == "reject":
                session.reject(ingested.reason)
            else:
                session.fail(ingested.reason)
            return None
        if ingested.image is None or ingested.thumbnail is None:
            session.fail("ingest_missing_image")
            return None
        return ingested

    def _commit_admitted_image(
        self,
        session: ImageDownloadSession,
        ingested,
        admission,
        *,
        scene_key: str,
        scene_profile: Any,
        threshold: float,
    ):
        img = ingested.image
        arr = ingested.thumbnail
        ext = ingested.ext
        output_content, output_ext, output_format, format_conversion = (
            self._prepare_output_image(img, ingested.content, ext)
        )
        quota_hold, quota_reason = reserve_ingest_quotas(
            scene_quotas=self._scene_quotas,
            source_quotas=self._source_quotas,
            domain_quotas=self._domain_quotas,
            scene_key=scene_key,
            source_key=str(session.source).strip(),
            domain_key=(urlsplit(session.url).hostname or "").casefold(),
            track_scene=scene_key in self.scene_targets,
        )
        if quota_hold is None:
            session.reject(quota_reason)
            return None
        session.quota_hold = quota_hold
        quality = build_image_quality_metrics(
            image=img,
            thumbnail=arr,
            output_content=output_content,
            output_format=output_format,
            scene_profile=scene_profile,
            semantic_threshold=threshold,
            scene_decision=admission.scene_decision,
        )
        facts = AcceptedImageFacts(
            url=session.url,
            keyword=session.keyword,
            source=session.source,
            job_id=self.job_id,
            dataset_id=getattr(self, "_dataset_id", ""),
            config_fingerprint=getattr(self, "_config_fingerprint", ""),
            clip_model_name=self.clip_model_name if self.use_clip else None,
            query_image=self.query_image or "",
            image_similarity_threshold=self.image_similarity_threshold,
            sim=admission.sim,
            image_sim=admission.image_sim,
            output_ext=output_ext,
            source_ext=ext,
            width=img.width,
            height=img.height,
            format_conversion=format_conversion,
            scene_quality_decision=admission.scene_decision,
        )
        materialized = materialize_accepted_image(
            repository=self._repository,
            dir_manager=self._dir_manager,
            state_store=self._state_store,
            job_id=self.job_id,
            output_dir=self.output_dir,
            manifest_writer=self._manifest_writer,
            metadata_writer=self._metadata_writer,
            output_content=output_content,
            source_content=ingested.content,
            url=session.url,
            output_ext=output_ext,
            candidate_id=session.candidate_id,
            max_count=self.total_count,
            build_records=bind_record_builder(
                facts=facts,
                quality=quality,
                label_resolution=self.label_policy.resolve(
                    self._query_label_decisions(session.keyword)
                ),
                label_policy=self.label_policy,
                compliance_policy=self.config.compliance_policy(),
            ),
            source_hash=ingested.content_hash,
            content_hash=hashlib.sha256(output_content).hexdigest(),
        )
        if not materialized.ok:
            if materialized.fail:
                session.fail(materialized.reason)
            else:
                session.reject(materialized.reason)
            return None
        return materialized

    def _record_saved(self, *, url: str, keyword: str, admission, materialized) -> None:
        self.stats.inc_saved("image")
        self._emit_callback(CrawlEvent(
            event_type="item_saved",
            keyword=keyword,
            media_type="image",
            url=url,
            detail={
                "sim": round(admission.sim, 4),
                "image_sim": (
                    round(admission.image_sim, 4)
                    if admission.image_sim is not None
                    else None
                ),
                "file": materialized.path,
                "index": materialized.index,
            },
        ))
        if materialized.index % 100 == 0 and materialized.index > 0:
            logger.info(f"已保存 {materialized.index} 张图片 (目标: {self.total_count})")

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
        """在图片通过过滤后，按配置生成最终落盘内容。"""
        return prepare_output_image(
            image,
            source_content,
            source_ext,
            output_format=self.image_output_format,
            jpeg_quality=self.jpeg_quality,
        )

    # ──────────────────────────────────────────────────────────────
    # 图片下载 + 过滤 + 保存
    # ──────────────────────────────────────────────────────────────

    def _download_and_save(self, url: str, keyword: str, source: str) -> bool:
        """下载单张图片，经过过滤后保存到分桶目录。

        过滤链：URL去重 → 下载 → 格式检测 → 尺寸过滤 → 方差过滤 → 内容去重 → CLIP过滤 → 保存

        Returns:
            True 表示成功保存，False 表示跳过或失败
        """
        if self._dir_manager.saved_count >= self.total_count:
            return False
        session = self._begin_image_session(url, keyword, source)
        if session is None:
            return False
        scene_key, scene_profile, threshold = self._scene_context(keyword)
        try:
            slot_error = session.acquire_slots()
            if slot_error:
                return session.fail(slot_error)
            ingested = self._accepted_ingest(
                session, self._ingest_session_image(session)
            )
            if ingested is None:
                return False
            if not session.claim_content(ingested.content):
                return session.reject("duplicate_content")
            admission = self._admit_ingested_image(
                ingested.image,
                keyword,
                scene_profile=scene_profile,
                threshold=threshold,
            )
            self._apply_admission_events(admission, keyword=keyword, url=url)
            if not admission.accepted:
                return session.reject(admission.reason)
            materialized = self._commit_admitted_image(
                session,
                ingested,
                admission,
                scene_key=scene_key,
                scene_profile=scene_profile,
                threshold=threshold,
            )
            if materialized is None:
                return False
            session.commit_success()
            self._record_saved(
                url=url,
                keyword=keyword,
                admission=admission,
                materialized=materialized,
            )
            return True
        except Exception as e:
            logger.warning(f"Download error {url[:55]}: {e}")
            self.stats.inc_failed("image")
            return session.fail(str(e))
        finally:
            session.close()

    # ──────────────────────────────────────────────────────────────
    # 搜索引擎爬取
    # ──────────────────────────────────────────────────────────────

    def _ordered_search_engines(self) -> list[str]:
        """Prioritize engines with observed page success and candidate yield."""
        return order_search_engines(
            self.search_engines,
            self.stats.summary().get("source_metrics", {}),
        )

    def _crawl_search_engines(self, keyword: str, pbar: tqdm) -> int:
        """通过搜索引擎爬取指定关键词的图片。

        Returns:
            本次爬取保存的图片数量
        """
        saved_before = self._dir_manager.saved_count

        for engine_name in self._ordered_search_engines():
            if self._collection_limit_reached(keyword):
                break

            eng = get_engine(engine_name)
            if eng.media_type != MediaType.IMAGE:
                continue

            remaining = self.total_count - self._dir_manager.saved_count
            pages_needed = estimate_pages_needed(remaining, eng.page_step)
            logger.info(f"[{engine_name}] keyword='{keyword}', pages={pages_needed}")

            offsets = iter(page_offsets(eng.page_step, pages_needed))
            page_workers = min(self.max_workers, self.max_inflight_pages)
            with ThreadPoolExecutor(max_workers=page_workers) as executor:

                def spawn():
                    if self._collection_limit_reached(keyword):
                        return None
                    try:
                        page_offset = next(offsets)
                    except StopIteration:
                        return None
                    url = eng.build_search_url(keyword, page_offset)
                    return executor.submit(
                        self._fetch_and_process_page, url, keyword, engine_name, pbar
                    )

                drain_inflight_window(
                    spawn,
                    window=page_workers,
                    on_error=lambda exc: logger.debug(
                        f"Page processing error: {exc}"
                    ),
                    on_inflight=lambda n: self._record_backpressure(
                        in_flight_pages=n
                    ),
                )

        return self._dir_manager.saved_count - saved_before

    def _fetch_and_process_page(self, url: str, keyword: str, engine_name: str, pbar: tqdm):
        """获取搜索结果页并处理其中的图片。"""
        if self._collection_limit_reached(keyword):
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

        raw_count, discovered = discover_page_images(
            eng.extract_items(html),
            keyword=keyword,
            source=engine_name,
            max_pending=self.max_pending_candidates,
        )
        self.stats.observe_source(
            engine_name,
            success=True,
            candidates=raw_count,
            elapsed_ms=(time.monotonic() - fetch_started) * 1000,
        )
        self._save_discovered_images(discovered, pbar)

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
        site_crawler = self._build_site_crawler(parser, parser_name)

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
            if self._collection_limit_reached():
                break
            self._save_discovered_images(
                urls_to_discovered(
                    site_crawler.fetch_article_images(article),
                    keyword=parser_name,
                    source=f"site:{parser_name}",
                ),
                pbar,
                check_scene=False,
            )

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

        self._save_discovered_images(
            spider_tools_to_discovered(
                urls,
                default_keyword=self._st_tags or self._st_query or "",
            ),
            pbar,
            check_scene=False,
        )

        return self._dir_manager.saved_count - saved_before

    def _build_site_crawler(self, parser, parser_name: str) -> SiteCrawler:
        return SiteCrawler(
            site_parser=parser,
            **site_crawler_kwargs(
                parser_name=parser_name,
                output_dir=self.output_dir,
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
            ),
        )

    def _log_crawl_start(self, already_saved: int, remaining: int) -> None:
        logger.info(
            f"数据集爬取开始: 目标={self.total_count}, 已有={already_saved}, "
            f"剩余={remaining}, 关键词={self.keywords}"
        )
        logger.info(f"搜索引擎: {self.search_engines}")
        logger.info(f"站点: {self._site_parsers}")
        logger.info(f"CLIP过滤: {'启用' if self.use_clip else '禁用'}")
        logger.info(f"分桶大小: {self.batch_size}")

    def _run_keyword_sources(self, pbar: tqdm) -> None:
        done = list(self._progress.keywords_done)
        for keyword in remaining_keywords(self.keywords, done):
            if self._collection_limit_reached():
                break
            logger.info(
                f"开始爬取关键词: '{keyword}' "
                f"(进度: {self._dir_manager.saved_count}/{self.total_count})"
            )
            self._crawl_search_engines(keyword, pbar)
            done.append(keyword)
            self._progress.save(
                **keyword_progress_snapshot(
                    keywords=self.keywords,
                    done=done,
                    saved_count=self._dir_manager.saved_count,
                    total_target=self.total_count,
                )
            )

    def _run_site_sources(self, pbar: tqdm) -> None:
        for parser_name in self._site_parsers:
            if self._collection_limit_reached():
                break
            logger.info(
                f"开始站点爬取: '{parser_name}' "
                f"(进度: {self._dir_manager.saved_count}/{self.total_count})"
            )
            self._crawl_site(parser_name, pbar)

    def _run_spider_tools_source(self, pbar: tqdm) -> None:
        if self._st_sites:
            self._crawl_spider_tools(pbar)

    def _finish_job(self):
        if self._state_store is not None:
            self._lease_recoveries += int(
                self._state_store.recover_expired_leases(self.job_id)
            )
        return self._generate_report()

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
        if self._metadata_writer is not None:
            self._metadata_writer.close()
        if self._manifest_writer is not None:
            self._manifest_writer.close()
        self._dedup.close()
        if self._state_store is not None:
            self._state_store.close()
            self._state_store = None

    def crawl(self):
        """执行数据集爬取任务。"""
        try:
            self.stats.start_time = time.monotonic()
            already_saved = self._dir_manager.saved_count
            remaining = remaining_quota(already_saved, self.total_count)
            if remaining <= 0:
                logger.info(f"已达到目标数量 {self.total_count}，无需继续爬取")
                return self._generate_report()

            self._log_crawl_start(already_saved, remaining)
            with tqdm(
                total=self.total_count,
                initial=already_saved,
                desc="DatasetCrawler",
            ) as pbar:
                self._run_keyword_sources(pbar)
                self._run_site_sources(pbar)
                self._run_spider_tools_source(pbar)
            return self._finish_job()
        finally:
            self.close()
            signal.signal(signal.SIGINT, self._original_sigint)
            signal.signal(signal.SIGTERM, self._original_sigterm)

    def _collect_job_report_stats(self) -> dict[str, Any]:
        db_stats = None
        db_stats_error = None
        if self._state_store is not None:
            try:
                db_stats = self._state_store.collect_stats()
            except Exception as exc:
                db_stats_error = str(exc)
        return merge_dataset_crawl_stats(
            self.stats.summary(),
            total_target=self.total_count,
            keywords=self.keywords,
            search_engines=self.search_engines,
            site_parsers=self._site_parsers,
            use_clip=self.use_clip,
            image_output_format=self.image_output_format,
            jpeg_quality=self.jpeg_quality,
            max_file_size=self.max_file_size,
            scene_targets=self._scene_quotas.snapshot(),
            scene_quality_gate={
                "enabled": self.scene_quality_gate_enabled,
                "review_queue_path": self.scene_review_queue_path,
                "decisions": dict(self._scene_quality_stats),
            },
            source_quotas=self._source_quotas.snapshot(),
            domain_quotas=self._domain_quotas.snapshot(),
            batch_size=self.batch_size,
            batches=self._dir_manager.list_batches(),
            shutdown=self._shutdown_requested.is_set(),
            http_metrics=self._http.metrics.snapshot(),
            lease_recoveries=int(self._lease_recoveries),
            repository_recovery=(
                dict(self._repository.recovery_report)
                if self._repository is not None
                else None
            ),
            db_stats=db_stats,
            db_stats_error=db_stats_error,
        )

    def _persist_job_report(self, report: dict[str, Any]) -> None:
        try:
            report_path = write_dataset_report_json(report, self.output_dir)
            logger.info(f"Dataset report saved to {report_path}")
        except Exception as e:
            logger.warning(f"Failed to save report: {e}")
        logger.info(f"数据集爬取完成! 共保存 {self._dir_manager.saved_count} 张图片")
        logger.info(f"分桶目录: {self._dir_manager.list_batches()}")
        logger.info(f"统计: {json.dumps(report, ensure_ascii=False)}")

    def _generate_report(self):
        """生成数据集采集报告。"""
        self.stats.end_time = time.monotonic()
        report = self._collect_job_report_stats()
        policy = self.config.compliance_policy()
        attach_dataset_lineage(
            report,
            job_id=self.job_id,
            output_dir=self.output_dir,
            config_snapshot=getattr(self, "_config_snapshot", {}),
            clip_model=self.clip_model_name if self.use_clip else None,
            extra_provenance={
                "saved_count": self._dir_manager.saved_count,
                "scene_quality_gate_enabled": self.scene_quality_gate_enabled,
                "compliance": policy.to_dict(),
            },
        )
        checklist = attach_dataset_publish_bundle(
            report,
            job_id=self.job_id,
            output_dir=self.output_dir,
            policy=policy,
            config_snapshot=getattr(self, "_config_snapshot", {}),
        )
        if checklist is not None:
            self.last_publish_checklist = checklist
        self._persist_job_report(report)
        self._emit_callback(CrawlEvent(
            event_type="crawl_done", keyword="",
            media_type="image", detail=report,
        ))
        return report
