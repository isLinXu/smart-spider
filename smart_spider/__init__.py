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
]
