# coding=utf-8
"""新功能回归测试。

覆盖：
- RateLimiter：修复后的精确令牌桶（无 double-lock Bug）
- UrlDeduplicator：持久化（persist_path）加载/追加 
- GoogleImageEngine：CSE JSON 解析、HTML 降级正则
- check_engine_health / check_all_engines：健康检查框架
- UrlDeduplicator.close()：文件句柄正确关闭
"""
import json
import os
import tempfile
import threading
import time

import pytest

from smart_spider.http_client import RateLimiter
from smart_spider.smart_spider import UrlDeduplicator
from smart_spider.engines import (
    GoogleImageEngine,
    check_engine_health,
    check_all_engines,
    ENGINE_REGISTRY,
    get_engine,
)


# ──────────────────────────────────────────────────────────────────────────────
# RateLimiter（修复后验证）
# ──────────────────────────────────────────────────────────────────────────────

class TestRateLimiterFixed:
    def test_token_count_never_goes_negative(self):
        """修复 double-lock Bug 后 token 不应变成负数（通过监控 _tokens 值）。"""
        limiter = RateLimiter(rate=5.0)
        errors = []

        def worker():
            for _ in range(3):
                limiter.acquire()
                with limiter._lock:
                    if limiter._tokens < -0.01:
                        errors.append(limiter._tokens)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Negative token counts detected: {errors}"

    def test_acquire_does_not_block_within_rate(self):
        limiter = RateLimiter(rate=200.0)
        start = time.monotonic()
        for _ in range(20):
            limiter.acquire()
        assert time.monotonic() - start < 1.0

    def test_acquire_throttles_at_low_rate(self):
        limiter = RateLimiter(rate=2.0)
        start = time.monotonic()
        for _ in range(5):
            limiter.acquire()
        elapsed = time.monotonic() - start
        # rate=2 → 初始 2 tokens，5 次需等 ≥ (5-2)/2 = 1.5s，保守断言 1.0s
        assert elapsed >= 1.0, f"Expected >=1.0s, got {elapsed:.2f}s"


# ──────────────────────────────────────────────────────────────────────────────
# UrlDeduplicator 持久化
# ──────────────────────────────────────────────────────────────────────────────

class TestUrlDeduplicatorPersist:
    def test_persist_writes_on_new_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "seen.jsonl")
            dedup = UrlDeduplicator(persist_path=path)
            dedup.is_seen("https://example.com/a")
            dedup.is_seen("https://example.com/b")
            dedup.close()

            assert os.path.exists(path)
            lines = open(path).read().strip().splitlines()
            assert len(lines) == 2  # 2 个不同 URL → 2 行 hash

    def test_persist_does_not_write_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "seen.jsonl")
            dedup = UrlDeduplicator(persist_path=path)
            dedup.is_seen("https://example.com/same")
            dedup.is_seen("https://example.com/same")  # 重复，不写
            dedup.close()

            lines = open(path).read().strip().splitlines()
            assert len(lines) == 1

    def test_resume_loads_existing_state(self):
        """第二个 UrlDeduplicator 从文件恢复，应识别上次已见的 URL。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "seen.jsonl")

            # 第一次运行
            d1 = UrlDeduplicator(persist_path=path)
            d1.is_seen("https://example.com/persisted")
            d1.close()

            # 第二次运行（恢复）
            d2 = UrlDeduplicator(persist_path=path)
            assert d2.is_seen("https://example.com/persisted"), \
                "URL seen in previous session should be recognized as seen"
            assert not d2.is_seen("https://example.com/new"), \
                "New URL should not be seen"
            d2.close()

    def test_close_is_idempotent(self):
        """多次 close 不应抛出异常。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "seen.jsonl")
            dedup = UrlDeduplicator(persist_path=path)
            dedup.close()
            dedup.close()  # 第二次不应抛出

    def test_no_persist_path_still_works(self):
        """无 persist_path 时行为与之前相同（不写文件）。"""
        dedup = UrlDeduplicator()
        assert not dedup.is_seen("https://x.com/1")
        assert dedup.is_seen("https://x.com/1")
        dedup.close()  # 无文件，静默返回


# ──────────────────────────────────────────────────────────────────────────────
# GoogleImageEngine
# ──────────────────────────────────────────────────────────────────────────────

class TestGoogleImageEngine:
    engine = GoogleImageEngine()

    def _cse_response(self, n=3):
        return json.dumps({
            "items": [
                {
                    "link": f"https://img.example.com/{i}.jpg",
                    "title": f"Google Image {i}",
                    "image": {
                        "thumbnailLink": f"https://th.example.com/{i}.jpg",
                        "width": 800,
                        "height": 600,
                        "contextLink": f"https://www.example.com/page{i}",
                    }
                }
                for i in range(n)
            ]
        })

    def test_extract_items_cse_json(self):
        items = self.engine.extract_items(self._cse_response(3))
        assert len(items) == 3
        assert items[0]["url"] == "https://img.example.com/0.jpg"
        assert items[0]["meta"]["title"] == "Google Image 0"
        assert items[0]["meta"]["width"] == 800
        assert items[0]["meta"]["thumb"] == "https://th.example.com/0.jpg"

    def test_extract_items_html_fallback(self):
        html = '"ou":"https://img.goog.com/photo1.jpg""ou":"https://img.goog.com/photo2.jpg"'
        items = self.engine.extract_items(html)
        assert len(items) == 2
        assert items[0]["url"] == "https://img.goog.com/photo1.jpg"

    def test_extract_items_empty(self):
        assert self.engine.extract_items("{}") == []
        assert self.engine.extract_items("") == []

    def test_build_url_with_api_key(self):
        eng = GoogleImageEngine(api_key="MYKEY", cse_id="MYCSE")
        url = eng.build_search_url("cat", 0)
        assert "googleapis.com/customsearch" in url
        assert "MYKEY" in url
        assert "MYCSE" in url
        assert "start=1" in url

    def test_build_url_without_api_key_uses_html(self):
        eng = GoogleImageEngine(api_key="", cse_id="")
        url = eng.build_search_url("cat", 0)
        assert "google.com/search" in url
        assert "tbm=isch" in url

    def test_engine_registered(self):
        eng = get_engine("google")
        assert eng.name == "google"

    def test_page_step(self):
        assert self.engine.page_step == 10


# ──────────────────────────────────────────────────────────────────────────────
# 引擎健康检查框架
# ──────────────────────────────────────────────────────────────────────────────

class TestEngineHealthCheck:
    def test_check_engine_health_structure(self, monkeypatch):
        """mock http_client，验证返回结构正确。"""
        from unittest.mock import MagicMock

        # 模拟一个返回 2 条结果的 http_client
        mock_client = MagicMock()
        mock_client.get_text.return_value = json.dumps({
            "data": [
                {"objURL": "https://img.example.com/1.jpg"},
                {"objURL": "https://img.example.com/2.jpg"},
            ]
        })

        result = check_engine_health("baidu", keyword="dog", http_client=mock_client)
        assert result["engine"] == "baidu"
        assert result["ok"] is True
        assert result["item_count"] == 2
        assert isinstance(result["latency_ms"], float)
        assert result["error"] == ""

    def test_check_engine_health_on_error(self, monkeypatch):
        """http_client 抛出异常时 ok=False，error 非空。"""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.get_text.side_effect = Exception("Connection refused")

        result = check_engine_health("bing", keyword="dog", http_client=mock_client)
        assert result["ok"] is False
        assert result["item_count"] == 0
        assert "Connection refused" in result["error"]

    def test_check_all_engines_returns_list(self, monkeypatch):
        """check_all_engines 返回所有静态引擎的结果列表。"""
        from unittest.mock import MagicMock
        from smart_spider.engines import RenderMode

        mock_client = MagicMock()
        mock_client.get_text.return_value = "{}"  # 返回空结果，但不崩

        results = check_all_engines(keyword="test", http_client=mock_client)
        static_engines = [
            n for n, e in ENGINE_REGISTRY.items()
            if e.render_mode == RenderMode.STATIC
        ]
        assert len(results) == len(static_engines)
        for r in results:
            assert "engine" in r
            assert "ok" in r
            assert "latency_ms" in r

    def test_check_all_engines_sorted_by_ok_then_latency(self, monkeypatch):
        """ok=True 的引擎排在 ok=False 的前面。"""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.get_text.return_value = "{}"

        results = check_all_engines(keyword="test", http_client=mock_client)
        # 验证排序：ok=True 的排在前面
        ok_flags = [r["ok"] for r in results]
        # 找到第一个 False 的位置，其后不应有 True
        found_false = False
        for flag in ok_flags:
            if not flag:
                found_false = True
            if found_false and flag:
                pytest.fail("ok=True entry found after ok=False entry (bad sort)")
