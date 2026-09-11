# coding=utf-8
"""SmartSpider - 基于 CLIP 的多模态智能爬虫。

核心组件
--------
- SmartSpider : 主类，串联采集流水线
- UrlDeduplicator : URL + 内容双重去重（BloomFilter 优先，降级 set）
- VideoDownloader : yt-dlp 视频下载（代理池轮换）
- TextExtractor : trafilatura 正文提取
- CrawlStats : 线程安全采集统计
- CrawlEvent : 回调钩子
- SiteCrawler : 列表页→详情页→资源 的深度爬取编排器
- SiteParser : 列表页/详情页解析标准接口
- DynamicRenderer : Playwright 动态渲染器

子模块
------
- smart_spider : 核心采集逻辑
- smart_spider.browser : 反爬浏览器层
- smart_spider.http_client : 反爬 HTTP 客户端
- smart_spider.site_crawler : 列表页→详情页深度爬取
- smart_spider.site_parser : 页面解析器
- smart_spider.utils : 工具函数

版本
----
2.2.0
"""
__version__ = "2.2.0"

from .smart_spider import SmartSpider, UrlDeduplicator, VideoDownloader, TextExtractor, CrawlEvent, CrawlStats

# 站点深度爬取扩展
from .site_crawler import SiteCrawler
from .site_parser import SiteParser, SITE_PARSER_REGISTRY, get_site_parser, register_site_parser

# 多模态感知层
from . import perception
from .perception import CLIPInference, OCRModule, PagePerception, PageState

# ReAct 决策核心
from . import decision
from .decision import ReActAgent, LLMBackend, OpenAIBackend, QwenBackend, ToolRegistry, Action, StepRecord, AgentResult

# 工具函数
from . import tools
from .tools import (
    navigate, click, type_text, scroll, screenshot,
    download_image, extract_text, extract_links, extract_images,
    clip_filter, content_dedup, save_result, save_metadata,
    register_tool, get_tool, list_tools, format_tools_prompt,
)

# Browser-Use Agent
from .agent import BrowserUseAgent

# 浏览器控制器
from .browser_controller import BrowserController
from .browser import DynamicRenderer, PersistentBrowserSession

# 数据集爬取
from .dataset_layout import DatasetDirManager, ManifestWriter, MetadataWriter, ProgressManager
from .dataset_crawler import DatasetCrawler
from .dataset_config import DatasetCrawlConfig
from .dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelMode,
    LabelPolicy,
    LabelResolution,
    Modality,
    ModalityAsset,
    ModalityRelation,
    QualityMetrics,
    SampleRecord,
    CONTRACT_FORMAT_VERSION,
    URL_NORMALIZE_VERSION,
)
from .dataset_lineage import (
    DatasetLineage,
    build_lineage,
    config_fingerprint,
    load_lineage,
    publish_dataset_artifacts,
    verify_manifest_checksum,
    write_manifest_checksum,
)
from .dataset_state import DatasetStateStore
from .dataset_repository import DatasetCommit, DatasetRepository
from .http_client import HttpMetrics, ResponseTooLargeError
from .url_policy import URLPolicy, UnsafeURLError
from .image_safety import UnsafeImageError, decode_image_bytes, probe_image_header, assert_header_within_budget
from .multimodal_pipeline import (
    AdaptiveSourceRouter,
    BatchAnnotationBackend,
    AnnotationResult,
    AnnotationRouter,
    BrowserPageSource,
    DiscoveryResult,
    DiscoveryTask,
    PageSampleExtractor,
    PlaybookBrowserSource,
    RouteAction,
    RouteDecision,
    SourceResponse,
    StaticPageSource,
)
from .multimodal_job import (
    AssetStore,
    MultimodalDatasetOrchestrator,
    MultimodalJobConfig,
    MultimodalJobReport,
    MultimodalManifestWriter,
    StagedAsset,
)
from .multimodal_repository import MultimodalRepository
from .multimodal_sources import SearchDiscoverySource, SiteDiscoverySource, SourceMetrics
from .multimodal_scale import QualityReport, ShardedManifestWriter
from .image_retrieval import (
    IMAGE_SUFFIXES,
    ImageSearchResult,
    ImageSimilarityIndex,
    IndexBuildReport,
    iter_image_files,
)
from .dataset_filter import (
    CLIPPromptScorer,
    DatasetImageFilter,
    DecisionPolicy,
    FilterDecision,
    FilterThresholds,
    PromptSet,
    SemanticScores,
    VisualSignalAnalyzer,
    VisualSignals,
)
from .scene_quality import (
    SceneCalibration,
    SceneQualityProfile,
    SignalRequirement,
    get_scene_quality_profile,
    list_scene_quality_profiles,
    load_scene_quality_profile,
    resolve_scene_quality_profile,
)
from .scene_quality_gate import GateDecision, JsonlSceneReviewQueue, SceneQualityGate, SceneSignalDetector
from .scene_signal_detector import (
    HeuristicSceneSignalDetector,
    YoloSceneSignalDetector,
    default_scene_signal_detector,
)
from .synthetic_image_detector import (
    CLIPPhotographicStyleDetector,
    CompositeSceneSignalDetector,
    OnnxSyntheticImageDetector,
    download_synthetic_image_detector,
)
from .dataset_governance import (
    LeakageSafeSplitter,
    PerceptualFingerprint,
    QuotaLedger,
    SceneQuotaLedger,
    SplitItem,
    SplitPlan,
    cluster_embeddings,
    hash_distance,
    perceptual_fingerprint,
)
from .reverse_image_search import (
    BaiduReverseImageProvider,
    BingVisualSearchProvider,
    BrowserDependencyError,
    GoogleLensProvider,
    ProviderSearchResponse,
    ProviderBlockedError,
    RemoteImageSearchResult,
    ReverseImageSearchError,
    ReverseImageSearchResponse,
    ReverseImageSearcher,
)

# spider_tools 桥接
from .spider_tools_bridge import SpiderToolsBridge, SpiderToolsURL, integrate_with_dataset_crawler, parse_page_spec, list_available_sites

__all__ = [
    "SmartSpider",
    "UrlDeduplicator",
    "VideoDownloader",
    "TextExtractor",
    "CrawlEvent",
    "CrawlStats",
    # 站点深度爬取
    "SiteCrawler",
    "SiteParser",
    "SITE_PARSER_REGISTRY",
    "get_site_parser",
    "register_site_parser",
    # 多模态感知层
    "perception",
    "CLIPInference",
    "OCRModule",
    "PagePerception",
    "PageState",
    # ReAct 决策核心
    "decision",
    "ReActAgent",
    "LLMBackend",
    "OpenAIBackend",
    "QwenBackend",
    "ToolRegistry",
    "Action",
    "StepRecord",
    "AgentResult",
    # 工具函数
    "tools",
    "navigate",
    "click",
    "type_text",
    "scroll",
    "screenshot",
    "download_image",
    "extract_text",
    "extract_links",
    "extract_images",
    "clip_filter",
    "content_dedup",
    "save_result",
    "save_metadata",
    "register_tool",
    "get_tool",
    "list_tools",
    "format_tools_prompt",
    # Browser-Use Agent
    "BrowserUseAgent",
    # 浏览器控制器
    "BrowserController",
    "DynamicRenderer",
    "PersistentBrowserSession",
    # 数据集爬取
    "DatasetCrawler",
    "DatasetCrawlConfig",
    "DatasetDirManager",
    "ProgressManager",
    "MetadataWriter",
    "ManifestWriter",
    "CandidateResource",
    "LabelDecision",
    "LabelMode",
    "LabelPolicy",
    "LabelResolution",
    "Modality",
    "ModalityAsset",
    "ModalityRelation",
    "QualityMetrics",
    "SampleRecord",
    "CONTRACT_FORMAT_VERSION",
    "URL_NORMALIZE_VERSION",
    "DatasetLineage",
    "build_lineage",
    "config_fingerprint",
    "load_lineage",
    "publish_dataset_artifacts",
    "verify_manifest_checksum",
    "write_manifest_checksum",
    "DatasetStateStore",
    "DatasetCommit",
    "DatasetRepository",
    "HttpMetrics",
    "ResponseTooLargeError",
    "URLPolicy",
    "UnsafeURLError",
    "UnsafeImageError",
    "decode_image_bytes",
    "probe_image_header",
    "assert_header_within_budget",
    "PageSampleExtractor",
    "AdaptiveSourceRouter",
    "AnnotationRouter",
    "BatchAnnotationBackend",
    "AnnotationResult",
    "StaticPageSource",
    "BrowserPageSource",
    "PlaybookBrowserSource",
    "DiscoveryTask",
    "DiscoveryResult",
    "SourceResponse",
    "RouteAction",
    "RouteDecision",
    "AssetStore",
    "MultimodalDatasetOrchestrator",
    "MultimodalJobConfig",
    "MultimodalJobReport",
    "MultimodalManifestWriter",
    "MultimodalRepository",
    "StagedAsset",
    "ShardedManifestWriter",
    "QualityReport",
    "SearchDiscoverySource",
    "SiteDiscoverySource",
    "SourceMetrics",
    "IMAGE_SUFFIXES",
    "ImageSearchResult",
    "ImageSimilarityIndex",
    "IndexBuildReport",
    "iter_image_files",
    "CLIPPromptScorer",
    "DatasetImageFilter",
    "DecisionPolicy",
    "FilterDecision",
    "FilterThresholds",
    "PromptSet",
    "SemanticScores",
    "VisualSignalAnalyzer",
    "VisualSignals",
    "SceneQualityProfile",
    "SceneCalibration",
    "SignalRequirement",
    "get_scene_quality_profile",
    "list_scene_quality_profiles",
    "load_scene_quality_profile",
    "resolve_scene_quality_profile",
    "GateDecision",
    "JsonlSceneReviewQueue",
    "SceneQualityGate",
    "SceneSignalDetector",
    "HeuristicSceneSignalDetector",
    "YoloSceneSignalDetector",
    "default_scene_signal_detector",
    "CompositeSceneSignalDetector",
    "CLIPPhotographicStyleDetector",
    "OnnxSyntheticImageDetector",
    "download_synthetic_image_detector",
    "LeakageSafeSplitter",
    "PerceptualFingerprint",
    "QuotaLedger",
    "SceneQuotaLedger",
    "SplitItem",
    "SplitPlan",
    "cluster_embeddings",
    "hash_distance",
    "perceptual_fingerprint",
    "BaiduReverseImageProvider",
    "BingVisualSearchProvider",
    "BrowserDependencyError",
    "GoogleLensProvider",
    "ProviderSearchResponse",
    "ProviderBlockedError",
    "RemoteImageSearchResult",
    "ReverseImageSearchError",
    "ReverseImageSearchResponse",
    "ReverseImageSearcher",
    # spider_tools 桥接
    "SpiderToolsBridge",
    "SpiderToolsURL",
    "integrate_with_dataset_crawler",
    "parse_page_spec",
    "list_available_sites",
]
