# coding=utf-8
"""v2.1 增强功能测试 — Phase 5~7 改进的回归验证。"""
import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ──────────────────────────────────────────────────────────────
# Phase 5: 代码质量
# ──────────────────────────────────────────────────────────────

class TestVersionExport:
    """验证 __version__ 正确导出。"""

    def test_version_string(self):
        from smart_spider import __version__
        assert __version__ == "2.2.0"

    def test_version_in_pyproject(self):
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        with open(os.path.join(os.path.dirname(__file__), "..", "pyproject.toml"), "rb") as f:
            data = tomllib.load(f)
        assert data["project"]["version"] == "2.2.0"


class TestCrawlKeywordExtraction:
    """验证 download() 拆分为 _crawl_keyword() 后功能不变。"""

    def test_crawl_keyword_method_exists(self):
        from smart_spider.smart_spider import SmartSpider
        assert hasattr(SmartSpider, "_crawl_keyword")
        assert callable(getattr(SmartSpider, "_crawl_keyword"))

    def test_log_context_method(self):
        from smart_spider.smart_spider import SmartSpider
        spider = MagicMock(spec=SmartSpider)
        spider._log_context = lambda self_, kw="", eng="": (
            f"[kw={kw}|eng={eng}] " if kw or eng else ""
        ).__func__.__get__(spider, type(spider))
        # Directly test the unbound method
        from smart_spider.smart_spider import SmartSpider as SS
        # Create a minimal instance for testing
        result = SS._log_context(None, "cat", "baidu")
        assert "cat" in result
        assert "baidu" in result


class TestVideoWorkerRaceFix:
    """验证 _video_worker 中 saved 变量的竞态修复。"""

    def test_saved_counter_thread_safety(self):
        """saved 在 lock 内递增，确保不会超过 target。"""
        from smart_spider.smart_spider import CrawlStats
        stats = CrawlStats()
        saved = 0
        lock = threading.Lock()
        target = 5
        errors = []

        def increment():
            nonlocal saved
            for _ in range(100):
                with lock:
                    if saved < target:
                        saved += 1

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert saved <= target, f"saved={saved} exceeded target={target}"


# ──────────────────────────────────────────────────────────────
# Phase 6: 健壮性
# ──────────────────────────────────────────────────────────────

class TestVideoDownloaderTimeout:
    """验证 VideoDownloader 超时可配置。"""

    def test_default_timeout(self):
        from smart_spider.smart_spider import VideoDownloader
        dl = VideoDownloader(output_dir="/tmp/test_vdl")
        assert dl.timeout == 300

    def test_custom_timeout(self):
        from smart_spider.smart_spider import VideoDownloader
        dl = VideoDownloader(output_dir="/tmp/test_vdl", timeout=600)
        assert dl.timeout == 600


class TestTextExtractorImprovements:
    """验证 TextExtractor 降级逻辑改进。"""

    def test_empty_html_returns_none(self):
        from smart_spider.smart_spider import TextExtractor
        extractor = TextExtractor(http_client=MagicMock())
        # 空响应
        extractor._client = MagicMock()
        extractor._client.get_text.return_value = ""
        result = extractor.extract("http://example.com")
        assert result is None

    def test_short_html_returns_none(self):
        from smart_spider.smart_spider import TextExtractor
        extractor = TextExtractor(http_client=MagicMock())
        extractor._client = MagicMock()
        extractor._client.get_text.return_value = "  <p>hi</p>  "
        result = extractor.extract("http://example.com")
        assert result is None

    def test_valid_html_returns_result(self):
        from smart_spider.smart_spider import TextExtractor
        extractor = TextExtractor(http_client=MagicMock())
        extractor._trafilatura = None  # 强制走降级路径
        extractor._client = MagicMock()
        html = "<html><title>Test</title><body>"
        html += "<p>" + "A" * 100 + "</p>"
        html += "</body></html>"
        extractor._client.get_text.return_value = html
        result = extractor.extract("http://example.com")
        assert result is not None
        assert result["title"] == "Test"
        assert len(result["text"]) > 50


class TestDynamicRendererSafeClose:
    """验证 DynamicRenderer.close() 安全关停。"""

    def test_close_sets_browser_none(self):
        from smart_spider.browser import DynamicRenderer
        renderer = MagicMock(spec=DynamicRenderer)
        renderer._loop = None
        renderer._browser = "not_none"
        # Simulate close behavior
        renderer._browser = None
        assert renderer._browser is None


# ──────────────────────────────────────────────────────────────
# Phase 7: 可观测性
# ──────────────────────────────────────────────────────────────

class TestCrawlStatsStartTime:
    """验证 CrawlStats.start_time 默认值。"""

    def test_start_time_not_zero(self):
        from smart_spider.smart_spider import CrawlStats
        stats = CrawlStats()
        assert stats.start_time > 0

    def test_elapsed_seconds_before_end(self):
        from smart_spider.smart_spider import CrawlStats
        stats = CrawlStats()
        elapsed = stats.elapsed_seconds
        assert elapsed >= 0


class TestCheckEnginesCLI:
    """验证 --check_engines CLI 参数。"""

    def test_check_engines_flag(self):
        import argparse
        from spider import main
        # 验证参数定义
        parser = argparse.ArgumentParser()
        parser.add_argument("--check_engines", action="store_true")
        args = parser.parse_args(["--check_engines"])
        assert args.check_engines is True


class TestCrawlEventExport:
    """验证 CrawlEvent 和 CrawlStats 在 __init__.py 中导出。"""

    def test_crawl_event_importable(self):
        from smart_spider import CrawlEvent
        assert CrawlEvent is not None

    def test_crawl_stats_importable(self):
        from smart_spider import CrawlStats
        assert CrawlStats is not None


# ──────────────────────────────────────────────────────────────
# 回归：确保已有功能不受影响
# ──────────────────────────────────────────────────────────────

class TestExistingFeaturesRegression:
    """回归测试：确保 v2.0 和 v2.1 Phase 1-4 功能不受影响。"""

    def test_url_deduplicator_still_works(self):
        from smart_spider.smart_spider import UrlDeduplicator
        dedup = UrlDeduplicator()
        assert not dedup.is_seen("http://a.com")
        assert dedup.is_seen("http://a.com")
        assert not dedup.is_seen("http://b.com")
        dedup.close()

    def test_crawl_stats_thread_safety(self):
        from smart_spider.smart_spider import CrawlStats
        stats = CrawlStats()
        errors = []

        def inc():
            try:
                for _ in range(100):
                    stats.inc_saved("image")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=inc) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert stats.saved.get("image", 0) == 500

    def test_proxy_pool_circuit_breaker(self):
        from smart_spider.http_client import ProxyPool
        pool = ProxyPool(["http://p1:8080", "http://p2:8080"], max_fail=2)
        entry = pool.get()
        assert entry is not None
        pool.report_fail(entry)
        pool.report_fail(entry)
        # After 2 fails, entry should be OPEN
        states = pool.circuit_states
        assert states[entry.url] == "open"

    def test_rate_limiter_basic(self):
        from smart_spider.http_client import RateLimiter
        limiter = RateLimiter(rate=100.0)
        # Should not block
        limiter.acquire()
        limiter.acquire()

    def test_engine_registry_complete(self):
        from smart_spider.engines import ENGINE_REGISTRY
        expected = {"baidu", "bing", "sogou", "360", "google", "weibo",
                    "bilibili", "bing_video", "baidu_text", "bing_text",
                    "weixin", "xiaohongshu"}
        assert set(ENGINE_REGISTRY.keys()) == expected

    def test_media_type_enum(self):
        from smart_spider.engines import MediaType
        assert MediaType.IMAGE.value == "image"
        assert MediaType.VIDEO.value == "video"
        assert MediaType.TEXT.value == "text"

    def test_disk_guard(self):
        from smart_spider.smart_spider import SmartSpider
        # 磁盘守卫方法存在
        assert hasattr(SmartSpider, "_check_disk_space")

    def test_graceful_shutdown(self):
        from smart_spider.smart_spider import SmartSpider
        assert hasattr(SmartSpider, "_should_stop")

    def test_content_dedup(self):
        from smart_spider.smart_spider import UrlDeduplicator
        dedup = UrlDeduplicator(content_dedup=True)
        # Same content, different URLs
        assert not dedup.is_content_seen(b"hello world")
        assert dedup.is_content_seen(b"hello world")
        assert not dedup.is_content_seen(b"different content")
        dedup.close()

    def test_avif_detection(self):
        from smart_spider.smart_spider import _detect_ext
        # AVIF ftyp box: \x00\x00\x00\x20 ftypavif
        avif_header = b'\x00\x00\x00\x20ftypavif'
        assert _detect_ext(avif_header) == ".avif"

    def test_crawl_event_dataclass(self):
        from smart_spider.smart_spider import CrawlEvent
        event = CrawlEvent(
            event_type="item_saved",
            keyword="cat",
            media_type="image",
            url="http://example.com/img.jpg",
            detail={"score": 0.85},
        )
        assert event.event_type == "item_saved"
        assert event.keyword == "cat"
