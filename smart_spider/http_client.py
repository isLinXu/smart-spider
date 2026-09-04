# coding=utf-8
"""SmartSpider 反爬网络层。

核心反爬策略
-----------
1. **UA 指纹随机化**：每次请求随机 User-Agent + 随机 Accept-Language / Sec-CH-UA 组合
2. **请求头完整性**：模拟真实浏览器完整 Header 集合，通过 TLS 指纹校验
3. **代理池轮换**：支持 HTTP/SOCKS5 代理列表，按策略（轮询/随机/失败摘除）分发
4. **指数退避重试**：遭遇 429/503/连接错误时自动等待后重试，最多 max_retries 次
5. **请求限速**：全局令牌桶（RateLimiter），控制每秒最大并发请求数，避免触发速率封禁
6. **Referer 伪造**：自动为每个搜索引擎注入对应的合法 Referer
7. **Cookie 会话保持**：每个代理维护独立 Session + Cookie jar，模拟真实会话
8. **TLS 指纹伪造（可选）**：优先使用 curl_cffi 模拟 Chrome TLS 握手，
   通过 Cloudflare 等 TLS 指纹检测；未安装时自动降级到 requests。
   安装：pip install curl_cffi
"""
import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

import requests
from fake_useragent import UserAgent
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .url_policy import URLPolicy, UnsafeURLError


class ResponseTooLargeError(requests.RequestException):
    """Raised before an HTTP response can exceed its configured byte budget."""

# ──────────────────────────────────────────────────────────────────────────────
# curl_cffi 可选：TLS 指纹伪造
# ──────────────────────────────────────────────────────────────────────────────

try:
    from curl_cffi import requests as curl_requests
    _CURL_CFFI_AVAILABLE = True
    logger.info("curl_cffi available: TLS fingerprint spoofing enabled (Chrome impersonation)")
except ImportError:
    _CURL_CFFI_AVAILABLE = False
    logger.debug("curl_cffi not installed, using requests (no TLS fingerprint spoofing). "
                 "Install with: pip install curl_cffi")

# ──────────────────────────────────────────────────────────────────────────────
# 请求头指纹库
# ──────────────────────────────────────────────────────────────────────────────

_ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "ja-JP,ja;q=0.9,zh-CN;q=0.8,zh;q=0.7",
]

_SEC_FETCH_DEST = ["document", "image", "empty"]

# 每个搜索引擎对应的合法 Referer
_ENGINE_REFERERS = {
    "baidu":      "https://www.baidu.com/",
    "bing":       "https://www.bing.com/",
    "sogou":      "https://www.sogou.com/",
    "360":        "https://www.so.com/",
    "bilibili":   "https://www.bilibili.com/",
    "bing_video": "https://www.bing.com/",
    "baidu_text": "https://www.baidu.com/",
    "bing_text":  "https://www.bing.com/",
    "weixin":     "https://weixin.sogou.com/",
    "xiaohongshu":"https://www.xiaohongshu.com/",
    "google":     "https://www.google.com/",
    "weibo":      "https://m.weibo.cn/",
}

# curl_cffi Chrome 版本轮换（模拟不同 Chrome 版本的 TLS 指纹）
_CHROME_IMPERSONATES = [
    "chrome124", "chrome120", "chrome110",
    "chrome107", "chrome104", "chrome101",
]

# ──────────────────────────────────────────────────────────────────────────────
# 令牌桶限速器
# ──────────────────────────────────────────────────────────────────────────────

class RateLimiter:
    """令牌桶算法：控制全局每秒最大请求数。

    修复记录
    --------
    - 旧版 acquire() 在「睡眠后再次加锁减 token」的路径中存在 double-lock Bug：
      睡眠期间其他线程已补充并消耗了 token，醒来后再减会导致 token 变成负数，
      使下一个请求需要等待更长时间（误差累积）。
    - 新版改为「持锁计算等待时间 → 释放锁 → sleep → 再持锁消耗 token」，
      用 condition variable 替代裸 sleep，确保唤醒后 token 状态一致。
    """

    def __init__(self, rate: float):
        """
        Args:
            rate: 每秒允许的最大请求数（令牌补充速率）
        """
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self):
        """在持锁状态下补充令牌（调用方负责加锁）。"""
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._rate, self._tokens + elapsed * self._rate)

    def acquire(self):
        """阻塞直到获取一个令牌（精确令牌桶，无 double-lock Bug）。"""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # 计算需等待多少秒才能补充到 1 个 token
                wait = (1.0 - self._tokens) / self._rate
            # 在锁外 sleep，避免持锁阻塞其他线程
            time.sleep(wait)


# ──────────────────────────────────────────────────────────────────────────────
# 代理池
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ProxyEntry:
    url: str                  # e.g. "http://user:pass@1.2.3.4:8080" 或 "socks5://..."
    fail_count: int = 0
    last_used: float = 0.0
    session: Optional[requests.Session] = field(default=None, repr=False)
    # Circuit breaker 状态字段
    cb_state: str = field(default="closed", init=False)      # closed / open / half_open
    cb_open_since: float = field(default=0.0, init=False)    # 进入 OPEN 状态的时刻

    def as_dict(self):
        return {"http": self.url, "https": self.url}


class ProxyPool:
    """线程安全的代理池，集成 Circuit Breaker 熔断模式。

    熔断机制
    --------
    - CLOSED  → 正常工作，请求自由通过
    - OPEN    → 连续失败超过阈值，拒绝请求进入冷却期
    - HALF_OPEN → 冷却期满，允许一个试探请求；成功则 CLOSED，失败则回到 OPEN

    相比旧版的改进
    -------------
    旧版 fail_count 达到 max_fail 后永久排除代理（仅在全部代理失败时暴力重置），
    导致偶发故障的代理永远无法恢复。新版加入 recovery_timeout 冷却期，
    代理在冷却后会自动进入 HALF_OPEN 试探恢复，无需暴力重置。
    """

    def __init__(
        self,
        proxies: list[str],
        max_fail: int = 3,
        recovery_timeout: float = 30.0,
    ):
        self._entries = [ProxyEntry(url=p) for p in proxies]
        self._max_fail = max_fail
        self._recovery_timeout = recovery_timeout
        self._idx = 0
        self._lock = threading.Lock()

    def _is_available(self, entry: ProxyEntry) -> bool:
        """Circuit breaker 可用性检查。"""
        if entry.cb_state == "closed":
            return True
        if entry.cb_state == "open":
            if time.monotonic() - entry.cb_open_since >= self._recovery_timeout:
                entry.cb_state = "half_open"
                return True
            return False
        # half_open: 允许试探请求
        return True

    def _active(self) -> list[ProxyEntry]:
        return [e for e in self._entries if self._is_available(e)]

    def get(self, strategy: str = "round_robin") -> Optional[ProxyEntry]:
        """获取一个可用代理，无代理时返回 None（直连）。"""
        with self._lock:
            active = self._active()
            if not active:
                # 所有代理处于 OPEN 状态 — 强制最早失败的代理进入 HALF_OPEN
                # 防止全部代理同时熔断导致的永久死锁
                if self._entries:
                    earliest = min(
                        self._entries,
                        key=lambda e: e.cb_open_since or float("inf"),
                    )
                    earliest.cb_state = "half_open"
                    active = [earliest]
                if not active:
                    return None
            if strategy == "random":
                entry = random.choice(active)
            else:  # round_robin
                self._idx = self._idx % len(active)
                entry = active[self._idx]
                self._idx = (self._idx + 1) % len(active)
            entry.last_used = time.monotonic()
            return entry

    def report_fail(self, entry: ProxyEntry):
        with self._lock:
            entry.fail_count += 1
            if entry.fail_count >= self._max_fail:
                entry.cb_state = "open"
                entry.cb_open_since = time.monotonic()
            logger.warning(
                f"Proxy {entry.url} fail_count={entry.fail_count} cb={entry.cb_state}"
            )

    def report_success(self, entry: ProxyEntry):
        with self._lock:
            entry.fail_count = max(0, entry.fail_count - 1)
            entry.cb_state = "closed"

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    @property
    def circuit_states(self) -> dict[str, str]:
        """返回各代理的 Circuit Breaker 状态（监控用）。"""
        with self._lock:
            return {e.url: e.cb_state for e in self._entries}


class HttpMetrics:
    """Thread-safe request counters and latency summary for one client."""

    def __init__(self, max_latency_samples: int = 2048) -> None:
        if max_latency_samples <= 0:
            raise ValueError("max_latency_samples must be positive")
        self._lock = threading.Lock()
        self._requests = 0
        self._successes = 0
        self._failures = 0
        self._bytes = 0
        self._oversized_responses = 0
        self._dns_rebind_rejections = 0
        self._status = Counter()
        self._latencies_ms: list[float] = []
        self._max_latency_samples = max_latency_samples
        self._latency_seen = 0

    def observe(
        self,
        *,
        success: bool,
        elapsed_ms: float,
        status_code: Optional[int] = None,
        bytes_received: int = 0,
    ) -> None:
        with self._lock:
            self._requests += 1
            self._successes += int(success)
            self._failures += int(not success)
            self._bytes += max(0, int(bytes_received))
            self._latency_seen += 1
            latency = max(0.0, float(elapsed_ms))
            if len(self._latencies_ms) < self._max_latency_samples:
                self._latencies_ms.append(latency)
            else:
                # Reservoir sampling keeps percentile memory bounded during
                # long-running crawls without retaining every request.
                slot = random.randrange(self._latency_seen)
                if slot < self._max_latency_samples:
                    self._latencies_ms[slot] = latency
            if status_code is not None:
                self._status[str(int(status_code))] += 1

    def observe_oversized_response(self) -> None:
        with self._lock:
            self._oversized_responses += 1

    def observe_dns_rebind_rejection(self) -> None:
        with self._lock:
            self._dns_rebind_rejections += 1

    def snapshot(self) -> dict:
        with self._lock:
            latencies = sorted(self._latencies_ms)
            if latencies:
                p50 = latencies[(len(latencies) - 1) // 2]
                p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
            else:
                p50 = p95 = 0.0
            return {
                "requests": self._requests,
                "successes": self._successes,
                "failures": self._failures,
                "bytes_received": self._bytes,
                "oversized_responses": self._oversized_responses,
                "dns_rebind_rejections": self._dns_rebind_rejections,
                "status_codes": dict(sorted(self._status.items())),
                "latency_ms": {"p50": round(p50, 2), "p95": round(p95, 2)},
            }


# ──────────────────────────────────────────────────────────────────────────────
# Session 工厂
# ──────────────────────────────────────────────────────────────────────────────

def _build_session(proxy_url: Optional[str] = None) -> requests.Session:
    """创建带重试适配器的 requests.Session。"""
    session = requests.Session()
    retry = Retry(
        total=0,          # 底层重试交给上层 SmartHttpClient 管理
        backoff_factor=0,
        status_forcelist=[],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=50)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


# ──────────────────────────────────────────────────────────────────────────────
# 智能 HTTP 客户端
# ──────────────────────────────────────────────────────────────────────────────

class SmartHttpClient:
    """
    反爬 HTTP 客户端。

    使用方式
    --------
    client = SmartHttpClient(
        proxies=["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"],
        rate=5.0,         # 每秒最多 5 个请求
        max_retries=3,
        use_curl_cffi=True,  # 启用 TLS 指纹伪造（需安装 curl_cffi）
    )
    resp = client.get("https://example.com", engine="baidu")
    """

    def __init__(
        self,
        proxies: Optional[list[str]] = None,
        rate: float = 10.0,
        max_retries: int = 3,
        timeout: float = 10,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        max_response_bytes: int = 25 * 1024 * 1024,
        proxy_strategy: str = "round_robin",
        use_curl_cffi: Optional[bool] = None,
        url_policy: Optional[URLPolicy] = None,
        allow_private_hosts: bool = False,
        max_redirects: int = 5,
        metrics: Optional[HttpMetrics] = None,
    ):
        self._ua = UserAgent()
        self._proxy_pool = ProxyPool(proxies or [])
        self._rate_limiter = RateLimiter(rate)
        self._max_retries = max_retries
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._connect_timeout = float(connect_timeout or timeout)
        self._read_timeout = float(read_timeout or timeout)
        if self._connect_timeout <= 0 or self._read_timeout <= 0:
            raise ValueError("connect_timeout and read_timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        # requests accepts (connect, read). curl_cffi accepts a scalar timeout,
        # so it uses the conservative maximum while requests gets split bounds.
        self._timeout = (self._connect_timeout, self._read_timeout)
        self._curl_timeout = max(self._connect_timeout, self._read_timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._proxy_strategy = proxy_strategy
        self.url_policy = url_policy or URLPolicy(
            allow_private_hosts=allow_private_hosts
        )
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        self.max_redirects = max_redirects
        self.metrics = metrics or HttpMetrics()
        # Host-level circuit breaker.  Image search results frequently contain
        # permanently dead CDNs; after a few failures, skip that host for the
        # remainder of this client session instead of queueing thousands of
        # doomed requests behind the normal retry/timeout path.
        self._host_failures: dict[str, int] = {}
        self._host_blocked_until: dict[str, float] = {}
        self._host_lock = threading.Lock()
        self._host_failure_threshold = 3
        self._host_block_seconds = 300.0
        self._host_circuit_exempt = {
            (urlparse(url).hostname or "").lower()
            for url in _ENGINE_REFERERS.values()
        }
        # Baidu's image JSON endpoint differs from its configured referer host.
        self._host_circuit_exempt.add("image.baidu.com")
        # 直连 Session（无代理时使用）
        self._direct_session = _build_session()

        # TLS 指纹：自动检测 curl_cffi，或由调用方显式指定
        if use_curl_cffi is None:
            self._use_curl = _CURL_CFFI_AVAILABLE
        else:
            if use_curl_cffi and not _CURL_CFFI_AVAILABLE:
                raise ImportError("curl_cffi is not installed. Run: pip install curl_cffi")
            self._use_curl = use_curl_cffi

    def _host_allowed(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host or host in self._host_circuit_exempt:
            return True
        now = time.monotonic()
        with self._host_lock:
            blocked_until = self._host_blocked_until.get(host, 0.0)
            if blocked_until > now:
                return False
            if blocked_until:
                self._host_blocked_until.pop(host, None)
                self._host_failures.pop(host, None)
        return True

    def _record_host_failure(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host or host in self._host_circuit_exempt:
            return
        with self._host_lock:
            failures = self._host_failures.get(host, 0) + 1
            self._host_failures[host] = failures
            if failures >= self._host_failure_threshold:
                self._host_blocked_until[host] = time.monotonic() + self._host_block_seconds
                logger.info(
                    f"Host circuit opened for {host} after {failures} failures "
                    f"({self._host_block_seconds:.0f}s)"
                )

    def _record_host_success(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if not host or host in self._host_circuit_exempt:
            return
        with self._host_lock:
            self._host_failures.pop(host, None)
            self._host_blocked_until.pop(host, None)

    @staticmethod
    def _extract_peer_address(response: requests.Response) -> Optional[str]:
        """Best-effort peer extraction across requests/urllib3 response shapes."""
        raw = getattr(response, "raw", None)
        candidates = [
            getattr(getattr(raw, "connection", None), "sock", None),
            getattr(getattr(raw, "_connection", None), "sock", None),
            getattr(getattr(getattr(getattr(raw, "_original_response", None), "fp", None), "raw", None), "_sock", None),
        ]
        for sock in candidates:
            try:
                peer = sock.getpeername()[0]
            except (AttributeError, OSError, TypeError, IndexError):
                continue
            if isinstance(peer, str) and peer:
                return peer
        return None

    def _validate_connected_peer(
        self,
        response: requests.Response,
        resolved_addresses: frozenset[str],
        *,
        via_proxy: bool,
    ) -> None:
        # A proxy socket belongs to the proxy, not to the target URL; judging it
        # against the target DNS result would reject legitimate proxy traffic.
        if via_proxy:
            return
        peer = self._extract_peer_address(response)
        if peer is None:
            return
        try:
            self.url_policy.validate_peer(peer, resolved_addresses)
        except UnsafeURLError:
            self.metrics.observe_dns_rebind_rejection()
            response.close()
            raise

    @staticmethod
    def _redirect_location(response: requests.Response) -> Optional[str]:
        status = getattr(response, "status_code", None)
        if status not in {301, 302, 303, 307, 308}:
            return None
        headers = getattr(response, "headers", {}) or {}
        location = headers.get("Location") if hasattr(headers, "get") else None
        return location if isinstance(location, str) and location.strip() else None

    def _make_headers(self, engine: Optional[str] = None) -> dict:
        """生成随机化的浏览器请求头。"""
        ua = self._ua.random
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }
        if engine and engine in _ENGINE_REFERERS:
            headers["Referer"] = _ENGINE_REFERERS[engine]
        headers["Sec-Fetch-Dest"] = random.choice(_SEC_FETCH_DEST)
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = "same-origin" if engine else "none"
        return headers

    def _get_session(self, proxy_entry: Optional[ProxyEntry]) -> requests.Session:
        if proxy_entry is None:
            return self._direct_session
        if proxy_entry.session is None:
            proxy_entry.session = _build_session(proxy_entry.url)
        return proxy_entry.session

    def _curl_get(
        self,
        url: str,
        headers: dict,
        proxy_url: Optional[str],
        stream: bool,
        **kwargs,
    ):
        """使用 curl_cffi 发起请求（Chrome TLS 指纹）。"""
        impersonate = random.choice(_CHROME_IMPERSONATES)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        return curl_requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=self._curl_timeout,
            impersonate=impersonate,
            stream=stream,
            **kwargs,
        )

    def _send_single_request(
        self,
        method: str,
        url: str,
        headers: dict,
        proxy_entry: Optional[ProxyEntry],
        stream: bool,
        **kwargs,
    ) -> requests.Response:
        """Send exactly one HTTP hop with automatic redirects disabled."""
        request_kwargs = dict(kwargs)
        request_kwargs.pop("allow_redirects", None)
        if self._use_curl:
            proxy_url = proxy_entry.url if proxy_entry else None
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            request = curl_requests.get if method == "GET" else curl_requests.head
            return request(
                url,
                headers=headers,
                proxies=proxies,
                timeout=self._curl_timeout,
                impersonate=random.choice(_CHROME_IMPERSONATES),
                # Always receive through a bounded iterator. A non-streaming
                # public call is materialized by _request after the byte cap.
                stream=True,
                allow_redirects=False,
                **request_kwargs,
            )
        session = self._get_session(proxy_entry)
        request = session.get if method == "GET" else session.head
        return request(
            url,
            headers=headers,
            timeout=self._timeout,
            # Always receive through a bounded iterator. A non-streaming
            # public call is materialized by _request after the byte cap.
            stream=True,
            allow_redirects=False,
            **request_kwargs,
        )

    def _send_with_validated_redirects(
        self,
        method: str,
        url: str,
        headers: dict,
        proxy_entry: Optional[ProxyEntry],
        stream: bool,
        **kwargs,
    ) -> requests.Response:
        """Validate every redirect target before it receives a request."""
        current_url = url
        redirects = 0
        while True:
            # Re-resolve immediately before every socket connection.  The peer
            # check below catches resolvers that change between these steps.
            self.url_policy.validate(current_url)
            resolved = self.url_policy.resolve_public_addresses(current_url)
            response = self._send_single_request(
                method, current_url, headers, proxy_entry, stream, **kwargs
            )
            try:
                actual_url = getattr(response, "url", None)
                if isinstance(actual_url, str) and actual_url:
                    self.url_policy.validate(actual_url)
                self._validate_connected_peer(
                    response, resolved, via_proxy=proxy_entry is not None
                )
                location = self._redirect_location(response)
                if location is None:
                    return response
                if redirects >= self.max_redirects:
                    raise requests.TooManyRedirects(
                        f"too many redirects for {url}", response=response
                    )
                next_url = urljoin(current_url, location)
                # This happens *before* closing the old hop and before issuing
                # a connection to the target, preventing redirect SSRF.
                self.url_policy.validate(next_url)
            except Exception:
                response.close()
                raise
            response.close()
            current_url = next_url
            redirects += 1

    @staticmethod
    def _declared_content_length(response: requests.Response) -> Optional[int]:
        headers = getattr(response, "headers", {}) or {}
        value = headers.get("Content-Length") if hasattr(headers, "get") else None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _drain_and_close(self, response: requests.Response) -> None:
        """Release a rejected socket without reading an unbounded error body."""
        try:
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total >= 1024 * 1024:
                    break
        except Exception:
            pass
        finally:
            response.close()

    def _request(
        self,
        method: str,
        url: str,
        engine: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        stream: bool = False,
        **kwargs,
    ) -> requests.Response:
        started = time.monotonic()
        last_exc: Optional[Exception] = None
        for attempt in range(self._max_retries + 1):
            if not self._host_allowed(url):
                error = requests.ConnectionError(
                    f"Host circuit open: {urlparse(url).hostname or url}"
                )
                self.metrics.observe(
                    success=False, elapsed_ms=(time.monotonic() - started) * 1000
                )
                raise error
            self._rate_limiter.acquire()
            proxy_entry = (
                self._proxy_pool.get(self._proxy_strategy)
                if not self._proxy_pool.is_empty() else None
            )
            headers = self._make_headers(engine)
            if extra_headers:
                headers.update(extra_headers)
            time.sleep(random.uniform(0.05, 0.30))
            try:
                response = self._send_with_validated_redirects(
                    method, url, headers, proxy_entry, stream, **kwargs
                )
                if response.status_code in (429, 403, 503):
                    self._drain_and_close(response)
                    logger.warning(
                        f"{method} anti-crawl {response.status_code} for {url[:60]}, "
                        f"attempt {attempt + 1}/{self._max_retries + 1}"
                    )
                    if proxy_entry:
                        self._proxy_pool.report_fail(proxy_entry)
                    self._record_host_failure(url)
                    last_exc = requests.HTTPError(response=response)
                    if attempt >= self._max_retries:
                        break
                else:
                    if proxy_entry:
                        self._proxy_pool.report_success(proxy_entry)
                    self._record_host_success(url)
                    received_bytes = self._declared_content_length(response) or 0
                    if not stream:
                        # Preserve requests' ordinary ``get().content`` API,
                        # but make it impossible to bypass the HTTP byte cap.
                        try:
                            content = self.read_bounded_bytes(
                                response,
                                max_bytes=self.max_response_bytes,
                                close_response=False,
                            )
                        except Exception:
                            response.close()
                            raise
                        try:
                            response._content = content  # type: ignore[attr-defined]
                            response._content_consumed = True  # type: ignore[attr-defined]
                        except Exception:
                            # Alternative response implementations may not
                            # expose requests' private content fields.
                            pass
                        # Some injected response doubles expose only a
                        # declared length and an empty iterator. Keep that
                        # historical metric behavior while real responses use
                        # the bytes actually consumed above.
                        received_bytes = len(content) or received_bytes
                    self.metrics.observe(
                        success=True,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                        status_code=getattr(response, "status_code", None),
                        bytes_received=received_bytes,
                    )
                    return response
            except (UnsafeURLError, requests.TooManyRedirects, ResponseTooLargeError) as exc:
                self.metrics.observe(
                    success=False,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    status_code=getattr(getattr(exc, "response", None), "status_code", None),
                )
                raise
            except Exception as exc:
                last_exc = exc
                if proxy_entry:
                    self._proxy_pool.report_fail(proxy_entry)
                self._record_host_failure(url)
                if attempt >= self._max_retries:
                    break
                logger.warning(
                    f"{method} error for {url[:60]}: {type(exc).__name__}: {exc}, "
                    f"retry {attempt + 2}/{self._max_retries + 1}"
                )
            wait = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait)
        self.metrics.observe(
            success=False,
            elapsed_ms=(time.monotonic() - started) * 1000,
            status_code=getattr(getattr(last_exc, "response", None), "status_code", None),
        )
        raise last_exc or requests.RequestException(
            f"{method} failed after {self._max_retries} retries: {url}"
        )

    def get(
        self,
        url: str,
        engine: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        stream: bool = False,
        **kwargs,
    ) -> requests.Response:
        """GET with retry, manual redirect validation, and split timeouts.

        Anti-crawl statuses ``(429, 403, 503)`` are drained with
        ``resp.iter_content()`` and closed in a ``try: ... finally:`` block via
        ``resp.close()`` by :meth:`_drain_and_close` before a retry.
        """
        return self._request(
            "GET", url, engine, extra_headers, stream=stream, **kwargs
        )

    def head(
        self,
        url: str,
        engine: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        **kwargs,
    ) -> requests.Response:
        """HEAD with the same redirect and SSRF protections as GET."""
        return self._request("HEAD", url, engine, extra_headers, **kwargs)

    def _check_declared_size(self, response: requests.Response, max_bytes: int) -> None:
        declared = self._declared_content_length(response)
        if declared is not None and declared > max_bytes:
            self.metrics.observe_oversized_response()
            response.close()
            raise ResponseTooLargeError(
                f"response declares {declared} bytes, above limit {max_bytes}"
            )

    def _bounded_iter_content(
        self,
        response: requests.Response,
        *,
        max_bytes: int,
        already_read: int = 0,
    ) -> Iterator[bytes]:
        """Yield response chunks while enforcing a byte ceiling at the HTTP edge."""
        total = already_read
        try:
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    self.metrics.observe_oversized_response()
                    response.close()
                    raise ResponseTooLargeError(
                        f"response exceeded byte limit {max_bytes}"
                    )
                yield chunk
        except ResponseTooLargeError:
            raise

    def read_bounded_bytes(
        self,
        response: requests.Response,
        *,
        max_bytes: Optional[int] = None,
        prefix: bytes = b"",
        close_response: bool = True,
    ) -> bytes:
        """Read a response via the shared bounded streaming implementation."""
        limit = getattr(self, "max_response_bytes", 25 * 1024 * 1024) if max_bytes is None else int(max_bytes)
        if limit <= 0:
            raise ValueError("max_bytes must be positive")
        self._check_declared_size(response, limit)
        if len(prefix) > limit:
            self.metrics.observe_oversized_response()
            response.close()
            raise ResponseTooLargeError(f"response exceeded byte limit {limit}")
        chunks = [bytes(prefix)] if prefix else []
        try:
            chunks.extend(
                self._bounded_iter_content(
                    response, max_bytes=limit, already_read=len(prefix)
                )
            )
            return b"".join(chunks)
        finally:
            if close_response:
                response.close()

    def get_bytes(
        self,
        url: str,
        engine: Optional[str] = None,
        *,
        max_bytes: Optional[int] = None,
        extra_headers: Optional[dict] = None,
    ) -> bytes:
        """Fetch bytes through the mandatory bounded streaming download path."""
        response = self.get(
            url, engine=engine, extra_headers=extra_headers, stream=True
        )
        return self.read_bounded_bytes(response, max_bytes=max_bytes)

    def get_text(
        self,
        url: str,
        engine: Optional[str] = None,
        *,
        max_bytes: Optional[int] = None,
        extra_headers: Optional[dict] = None,
    ) -> str:
        """Fetch text without allowing a response to bypass the byte budget."""
        response = self.get(
            url, engine=engine, extra_headers=extra_headers, stream=True
        )
        try:
            content = self.read_bounded_bytes(response, max_bytes=max_bytes)
        finally:
            # read_bounded_bytes closes first; this keeps mocked responses and
            # alternative clients compatible with the historical contract.
            response.close()
        # ``apparent_encoding`` may read response.content again. Use the
        # header-derived encoding only after bounded bytes have been captured.
        encoding = (
            getattr(response, "encoding", None)
            or getattr(response, "charset_encoding", None)
            or "utf-8"
        )
        return content.decode(encoding, errors="replace")

    def get_stream(
        self,
        url: str,
        engine: Optional[str] = None,
        peek_bytes: int = 8192,
        *,
        max_bytes: Optional[int] = None,
    ):
        """Stream-fetch the first peek_bytes bytes; return (peek, response).

        The caller can continue reading the rest via resp.iter_content().
        peek is guaranteed to be AT MOST peek_bytes bytes long -- excess bytes
        from an oversized chunk are prepended back into the response by storing
        them in a local buffer that iter_content() will yield first.

        Bug fix: old version accumulated chunks until len(peek) >= peek_bytes,
        so peek could be up to 2*peek_bytes-1 bytes if the server returned a
        large chunk.  Callers that assumed peek == first 8 KB could misbehave.
        Now peek is strictly truncated to peek_bytes; leftover bytes are stored
        as _peek_overflow on the response object so the next iter_content call
        yields them transparently.
        """
        limit = getattr(self, "max_response_bytes", 25 * 1024 * 1024) if max_bytes is None else int(max_bytes)
        if peek_bytes <= 0 or limit <= 0:
            raise ValueError("peek_bytes and max_bytes must be positive")
        resp = self.get(url, engine=engine, stream=True)
        self._check_declared_size(resp, limit)
        peek = b""
        overflow = b""
        bytes_read = 0
        try:
            for chunk in resp.iter_content(chunk_size=peek_bytes):
                if not chunk:
                    continue
                bytes_read += len(chunk)
                if bytes_read > limit:
                    self.metrics.observe_oversized_response()
                    raise ResponseTooLargeError(
                        f"response exceeded byte limit {limit}"
                    )
                if len(peek) + len(chunk) <= peek_bytes:
                    peek += chunk
                else:
                    needed = peek_bytes - len(peek)
                    peek += chunk[:needed]
                    overflow = chunk[needed:]
                if len(peek) >= peek_bytes:
                    break
        except Exception:
            # Assignment at the call site has not completed yet, so only this
            # method can reliably release the response after a peek failure.
            resp.close()
            raise

        # Preserve bytes that were read beyond the peek and ensure callers
        # cannot bypass the byte ceiling by consuming the returned response.
        if overflow:
            resp._peek_overflow = overflow  # type: ignore[attr-defined]
        _orig_iter = resp.iter_content

        def _patched_iter(chunk_size=1, decode_unicode=False):
            if overflow:
                yield overflow
            total = bytes_read
            for chunk in _orig_iter(
                chunk_size=chunk_size, decode_unicode=decode_unicode
            ):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    self.metrics.observe_oversized_response()
                    resp.close()
                    raise ResponseTooLargeError(
                        f"response exceeded byte limit {limit}"
                    )
                yield chunk

        resp.iter_content = _patched_iter  # type: ignore[method-assign]

        return peek, resp

    def close(self) -> None:
        """Close pooled sessions and release sockets."""
        sessions = [self._direct_session]
        sessions.extend(
            entry.session for entry in self._proxy_pool._entries if entry.session is not None
        )
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
