# coding=utf-8
"""HTTP 客户端层单元测试。

覆盖：
- RateLimiter：令牌桶限速
- ProxyPool：轮询/随机/失败摘除/自动复位
- SmartHttpClient：重试逻辑、Header 随机化
- UrlDeduplicator：去重正确性、线程安全
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests as req_lib

from smart_spider.http_client import HttpMetrics, RateLimiter, ProxyPool, SmartHttpClient
from smart_spider.url_policy import UnsafeURLError, URLPolicy
from smart_spider.smart_spider import UrlDeduplicator


# ──────────────────────────────────────────────────────────────────────────────
# RateLimiter
# ──────────────────────────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_acquire_does_not_block_within_rate(self):
        """速率内的请求不应阻塞。"""
        limiter = RateLimiter(rate=100.0)  # 100 req/s
        start = time.monotonic()
        for _ in range(10):
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"10 requests at 100 RPS should finish in <1s, took {elapsed:.2f}s"

    def test_acquire_throttles_when_exceeded(self):
        """超出速率时应产生阻塞等待。

        rate=2：初始桶满（2 tokens），连续 6 次 acquire：
          - 前 2 次立即消耗桶内 token
          - 后 4 次需补充，总补充耗时 ≈ 4/2 = 2s
          但实现中每次补充只等不足的 token，实际耗时约 (6-2-1)/2 ≈ 1.0s~2.0s
        保守断言：应 >= 0.8s。
        """
        limiter = RateLimiter(rate=2.0)
        start = time.monotonic()
        for _ in range(6):
            limiter.acquire()
        elapsed = time.monotonic() - start
        assert elapsed >= 0.8, f"Expected >=0.8s throttle at 2 RPS, got {elapsed:.2f}s"

    def test_thread_safe(self):
        """多线程并发 acquire 不应抛出异常。"""
        limiter = RateLimiter(rate=50.0)
        errors = []

        def worker():
            try:
                for _ in range(5):
                    limiter.acquire()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ──────────────────────────────────────────────────────────────────────────────
# ProxyPool
# ──────────────────────────────────────────────────────────────────────────────

class TestProxyPool:
    def test_empty_pool_returns_none(self):
        pool = ProxyPool([])
        assert pool.get() is None

    def test_is_empty_true(self):
        assert ProxyPool([]).is_empty()
        assert not ProxyPool(["http://1.2.3.4:8080"]).is_empty()

    def test_round_robin(self):
        pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
        urls = [pool.get().url for _ in range(6)]
        # 应循环：a, b, c, a, b, c
        assert urls == ["http://a:1", "http://b:2", "http://c:3",
                        "http://a:1", "http://b:2", "http://c:3"]

    def test_random_returns_valid_entry(self):
        pool = ProxyPool(["http://a:1", "http://b:2"])
        for _ in range(20):
            entry = pool.get(strategy="random")
            assert entry.url in ("http://a:1", "http://b:2")

    def test_fail_isolates_proxy(self):
        pool = ProxyPool(["http://bad:1", "http://good:2"], max_fail=2)
        bad = pool.get()  # => http://bad:1
        pool.report_fail(bad)
        pool.report_fail(bad)
        # bad 已达上限，所有 get() 应返回 good
        for _ in range(5):
            entry = pool.get()
            assert entry.url == "http://good:2"

    def test_fail_auto_reset(self):
        """所有代理都失败后应自动复位。"""
        pool = ProxyPool(["http://a:1"], max_fail=1)
        a = pool.get()
        pool.report_fail(a)  # a 被隔离
        # 下次 get 时应自动复位
        entry = pool.get()
        assert entry is not None
        assert entry.url == "http://a:1"

    def test_report_success_decrements_fail_count(self):
        pool = ProxyPool(["http://a:1"])
        entry = pool.get()
        pool.report_fail(entry)
        assert entry.fail_count == 1
        pool.report_success(entry)
        assert entry.fail_count == 0

    def test_thread_safe_round_robin(self):
        """多线程并发 get 不应崩溃，且每个 URL 都应被轮询到。"""
        pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
        results = []
        lock = threading.Lock()

        def worker():
            entry = pool.get()
            if entry:
                with lock:
                    results.append(entry.url)

        threads = [threading.Thread(target=worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 30
        assert set(results) == {"http://a:1", "http://b:2", "http://c:3"}


# ──────────────────────────────────────────────────────────────────────────────
# UrlDeduplicator
# ──────────────────────────────────────────────────────────────────────────────

class TestUrlDeduplicator:
    def test_first_seen_returns_false(self):
        dedup = UrlDeduplicator()
        assert not dedup.is_seen("https://example.com/1")

    def test_second_seen_returns_true(self):
        dedup = UrlDeduplicator()
        dedup.is_seen("https://example.com/1")
        assert dedup.is_seen("https://example.com/1")

    def test_different_urls_not_seen(self):
        dedup = UrlDeduplicator()
        assert not dedup.is_seen("https://example.com/a")
        assert not dedup.is_seen("https://example.com/b")

    def test_thread_safe_no_duplicate_pass(self):
        """多线程并发场景下同一 URL 只应被 is_seen=False 一次。"""
        dedup = UrlDeduplicator()
        url = "https://example.com/shared"
        results = []
        lock = threading.Lock()

        def worker():
            seen = dedup.is_seen(url)
            with lock:
                results.append(seen)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 恰好 1 个线程看到 False（首次），49 个看到 True
        assert results.count(False) == 1
        assert results.count(True) == 49


# ──────────────────────────────────────────────────────────────────────────────
# SmartHttpClient（使用 mock，不发真实网络请求）
# ──────────────────────────────────────────────────────────────────────────────

class TestSmartHttpClient:
    @pytest.fixture
    def mock_ok_response(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"hello world"
        resp.text = "hello world"
        resp.apparent_encoding = "utf-8"
        return resp

    @pytest.fixture
    def mock_429_response(self):
        resp = MagicMock()
        resp.status_code = 429
        return resp

    @patch("smart_spider.http_client._build_session")
    def test_get_success(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session

        client = SmartHttpClient(rate=1000.0)
        resp = client.get("https://example.com")
        assert resp.status_code == 200

    @patch("smart_spider.http_client._build_session")
    def test_get_retries_on_429(self, mock_build, mock_429_response, mock_ok_response):
        mock_session = MagicMock()
        # 前两次返回 429，第三次成功
        mock_session.get.side_effect = [mock_429_response, mock_429_response, mock_ok_response]
        mock_build.return_value = mock_session

        with patch("time.sleep"):  # 跳过实际等待
            client = SmartHttpClient(rate=1000.0, max_retries=3)
            client._use_curl = False  # 强制走 requests 路径
            resp = client.get("https://example.com")
        assert resp.status_code == 200
        assert mock_session.get.call_count == 3

    @patch("smart_spider.http_client._build_session")
    def test_get_raises_after_max_retries(self, mock_build):
        mock_session = MagicMock()
        mock_session.get.side_effect = req_lib.ConnectionError("connection refused")
        mock_build.return_value = mock_session

        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0, max_retries=2)
            client._use_curl = False  # 强制走 requests 路径
            with pytest.raises(Exception):
                client.get("https://example.com")
        assert mock_session.get.call_count == 3  # 1 次 + 2 次重试

    @patch("smart_spider.http_client._build_session")
    def test_headers_contain_user_agent(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session

        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0)
            client._use_curl = False  # 强制走 requests 路径
            client.get("https://example.com")

        call_kwargs = mock_session.get.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers") or {}
        assert "User-Agent" in headers

    @patch("smart_spider.http_client._build_session")
    def test_referer_injected_for_known_engine(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session

        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0)
            client._use_curl = False  # 强制走 requests 路径
            client.get("https://example.com", engine="baidu")

        headers = mock_session.get.call_args.kwargs.get("headers", {})
        assert headers.get("Referer") == "https://www.baidu.com/"

    @patch("smart_spider.http_client._build_session")
    def test_get_bytes_returns_bytes(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session

        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0)
            data = client.get_bytes("https://example.com")
        assert isinstance(data, bytes)

    def test_url_policy_rejects_private_and_credential_urls(self):
        policy = URLPolicy(resolve_dns=False)
        with pytest.raises(UnsafeURLError):
            policy.validate("http://127.0.0.1/image.jpg")
        with pytest.raises(UnsafeURLError):
            policy.validate("http://user:pass@example.com/image.jpg")
        with pytest.raises(UnsafeURLError):
            policy.validate("file:///tmp/image.jpg")

    @patch("smart_spider.http_client._build_session")
    def test_metrics_capture_success(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_ok_response.headers = {"Content-Length": "11"}
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session
        metrics = HttpMetrics()
        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0, metrics=metrics)
            client._use_curl = False
            client.get("https://example.com")
        snapshot = metrics.snapshot()
        assert snapshot["requests"] == 1
        assert snapshot["successes"] == 1
        assert snapshot["bytes_received"] == 11
        assert snapshot["status_codes"] == {"200": 1}

    def test_metrics_bound_latency_memory(self):
        metrics = HttpMetrics(max_latency_samples=2)
        for value in range(10):
            metrics.observe(success=True, elapsed_ms=value)
        assert len(metrics._latencies_ms) == 2
        assert metrics.snapshot()["requests"] == 10
