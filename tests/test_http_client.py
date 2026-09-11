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

from smart_spider.http_client import (
    HttpMetrics,
    RateLimiter,
    ProxyPool,
    ResponseTooLargeError,
    SmartHttpClient,
)
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
    def test_get_retries_on_503(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        server = MagicMock()
        server.status_code = 503
        mock_session.get.side_effect = [server, mock_ok_response]
        mock_build.return_value = mock_session
        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0, max_retries=3)
            client._use_curl = False
            resp = client.get("https://example.com")
        assert resp.status_code == 200
        assert mock_session.get.call_count == 2

    @patch("smart_spider.http_client._build_session")
    def test_429_honors_retry_after_cap(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        limited = MagicMock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "999"}
        mock_session.get.side_effect = [limited, mock_ok_response]
        mock_build.return_value = mock_session
        sleeps: list[float] = []

        def _sleep(seconds):
            sleeps.append(float(seconds))

        with patch("time.sleep", side_effect=_sleep):
            client = SmartHttpClient(rate=1000.0, max_retries=3, retry_after_cap=2.5)
            client._use_curl = False
            resp = client.get("https://example.com")
        assert resp.status_code == 200
        # First sleep is the jitter before send; Retry-After wait follows.
        assert any(abs(value - 2.5) < 1e-6 for value in sleeps)

    @patch("smart_spider.http_client._build_session")
    def test_hard_4xx_is_not_retried(self, mock_build):
        mock_session = MagicMock()
        missing = MagicMock()
        missing.status_code = 404
        missing.content = b"missing"
        missing.headers = {"Content-Length": "7"}
        missing.iter_content = MagicMock(return_value=iter([b"missing"]))
        mock_session.get.return_value = missing
        mock_build.return_value = mock_session
        with patch("time.sleep"):
            client = SmartHttpClient(rate=1000.0, max_retries=3)
            client._use_curl = False
            resp = client.get("https://example.com/missing")
        assert resp.status_code == 404
        assert mock_session.get.call_count == 1

    def test_retry_class_matrix(self):
        assert SmartHttpClient._retry_class(429) == "rate_limit"
        assert SmartHttpClient._retry_class(403) == "anti_crawl"
        assert SmartHttpClient._retry_class(503) == "server"
        assert SmartHttpClient._retry_class(404) == "client"
        assert SmartHttpClient._retry_class(200) == "ok"

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

    @patch("smart_spider.http_client._build_session")
    def test_non_stream_get_is_bounded_at_http_layer(self, mock_build):
        response = MagicMock(status_code=200, headers={})
        response.iter_content.return_value = [b"1234", b"56"]
        session = MagicMock()
        session.get.return_value = response
        mock_build.return_value = session

        with patch("time.sleep"), pytest.raises(ResponseTooLargeError):
            client = SmartHttpClient(
                rate=1000.0,
                max_retries=0,
                max_response_bytes=5,
                url_policy=URLPolicy(resolve_dns=False),
                use_curl_cffi=False,
            )
            client.get("https://example.com")

        response.close.assert_called()

    @patch("smart_spider.http_client._build_session")
    def test_redirect_target_is_validated_before_next_request(self, mock_build):
        redirect = MagicMock(status_code=302, headers={"Location": "/next"})
        final = MagicMock(status_code=200, headers={})
        session = MagicMock()
        session.get.side_effect = [redirect, final]
        mock_build.return_value = session

        with patch("time.sleep"):
            client = SmartHttpClient(
                rate=1000.0, url_policy=URLPolicy(resolve_dns=False),
                use_curl_cffi=False,
            )
            client.get("https://example.com/start")

        assert [call.args[0] for call in session.get.call_args_list] == [
            "https://example.com/start", "https://example.com/next"
        ]
        assert all(call.kwargs["allow_redirects"] is False for call in session.get.call_args_list)
        redirect.close.assert_called_once()

    @patch("smart_spider.http_client._build_session")
    def test_private_redirect_is_rejected_before_connection(self, mock_build):
        redirect = MagicMock(
            status_code=302, headers={"Location": "http://127.0.0.1/admin"}
        )
        session = MagicMock()
        session.get.return_value = redirect
        mock_build.return_value = session

        with patch("time.sleep"), pytest.raises(UnsafeURLError):
            client = SmartHttpClient(
                rate=1000.0, url_policy=URLPolicy(resolve_dns=False),
                use_curl_cffi=False,
            )
            client.get("https://example.com/start")

        assert session.get.call_count == 1
        redirect.close.assert_called_once()

    @patch("smart_spider.http_client._build_session")
    def test_bounded_bytes_rejects_declared_oversize_response(self, mock_build):
        response = MagicMock(status_code=200, headers={"Content-Length": "16"})
        session = MagicMock()
        session.get.return_value = response
        mock_build.return_value = session
        metrics = HttpMetrics()

        with patch("time.sleep"), pytest.raises(ResponseTooLargeError):
            client = SmartHttpClient(
                rate=1000.0, max_response_bytes=8, metrics=metrics,
                url_policy=URLPolicy(resolve_dns=False), use_curl_cffi=False,
            )
            client.get_bytes("https://example.com/image")

        assert response.close.called
        assert metrics.snapshot()["oversized_responses"] == 1

    @patch("smart_spider.http_client._build_session")
    def test_split_connect_and_read_timeout_reach_requests(self, mock_build, mock_ok_response):
        mock_session = MagicMock()
        mock_session.get.return_value = mock_ok_response
        mock_build.return_value = mock_session

        with patch("time.sleep"):
            client = SmartHttpClient(
                rate=1000.0, connect_timeout=2, read_timeout=7,
                url_policy=URLPolicy(resolve_dns=False), use_curl_cffi=False,
            )
            client.get("https://example.com")

        assert mock_session.get.call_args.kwargs["timeout"] == (2.0, 7.0)

    def test_url_policy_rejects_private_and_credential_urls(self):
        policy = URLPolicy(resolve_dns=False)
        with pytest.raises(UnsafeURLError):
            policy.validate("http://127.0.0.1/image.jpg")
        with pytest.raises(UnsafeURLError):
            policy.validate("http://user:pass@example.com/image.jpg")
        with pytest.raises(UnsafeURLError):
            policy.validate("file:///tmp/image.jpg")

    def test_url_policy_rejects_dns_rebinding_peer(self):
        policy = URLPolicy(resolve_dns=False)
        with pytest.raises(UnsafeURLError, match="connected peer"):
            policy.validate_peer("8.8.8.8", frozenset({"1.1.1.1"}))

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
