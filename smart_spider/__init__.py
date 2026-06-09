# coding=utf-8
"""SmartSpider - 基于 CLIP 的多模态智能爬虫。"""
__version__ = "2.2.0"

from .smart_spider import SmartSpider, UrlDeduplicator, VideoDownloader, TextExtractor, CrawlEvent, CrawlStats
from .engines import (
    get_engine, get_engines_by_media,
    check_engine_health, check_all_engines,
    ENGINE_REGISTRY, MediaType, RenderMode,
    GoogleImageEngine, WeiboPicEngine,
)
from .http_client import SmartHttpClient, ProxyPool, RateLimiter
from .site_parser import SiteParser, SITE_PARSER_REGISTRY, get_site_parser, register_site_parser
from .site_crawler import SiteCrawler

__all__ = [
    "SmartSpider",
    "UrlDeduplicator",
    "VideoDownloader",
    "TextExtractor",
    "get_engine",
    "get_engines_by_media",
    "check_engine_health",
    "check_all_engines",
    "ENGINE_REGISTRY",
    "MediaType",
    "RenderMode",
    "GoogleImageEngine",
    "WeiboPicEngine",
    "SmartHttpClient",
    "ProxyPool",
    "RateLimiter",
    "CrawlEvent",
    "CrawlStats",
    # 站点深度爬取
    "SiteParser",
    "SiteCrawler",
    "SITE_PARSER_REGISTRY",
    "get_site_parser",
    "register_site_parser",
]

