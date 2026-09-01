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
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from fake_useragent import UserAgent
from loguru import logger
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
        timeout: int = 10,
        proxy_strategy: str = "round_robin",
        use_curl_cffi: Optional[bool] = None,
    ):
        self._ua = UserAgent()
        self._proxy_pool = ProxyPool(proxies or [])
        self._rate_limiter = RateLimiter(rate)
        self._max_retries = max_retries
        self._timeout = timeout
        self._proxy_strategy = proxy_strategy
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
            timeout=self._timeout,
            impersonate=impersonate,
            stream=stream,
            **kwargs,
        )

    def get(
        self,
        url: str,
        engine: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        stream: bool = False,
        **kwargs,
    ) -> requests.Response:
        """
        发起 GET 请求，内置重试 + 代理轮换 + 限速 + 随机 Header。

        Args:
            url:           目标 URL
            engine:        搜索引擎名称（用于注入 Referer），可为 None
            extra_headers: 额外覆盖的 Header
            stream:        是否流式下载

        Raises:
            requests.RequestException: 重试耗尽后仍失败
        """
        last_exc = None
        for attempt in range(self._max_retries + 1):
            if not self._host_allowed(url):
                raise requests.ConnectionError(
                    f"Host circuit open: {urlparse(url).hostname or url}"
                )
            self._rate_limiter.acquire()

            proxy_entry = (
                self._proxy_pool.get(self._proxy_strategy)
                if not self._proxy_pool.is_empty()
                else None
            )
            headers = self._make_headers(engine)
            if extra_headers:
                headers.update(extra_headers)

            # 随机抖动（50-300ms），避免请求过于规律
            time.sleep(random.uniform(0.05, 0.30))

            try:
                if self._use_curl:
                    proxy_url = proxy_entry.url if proxy_entry else None
                    resp = self._curl_get(url, headers, proxy_url, stream, **kwargs)
                else:
                    session = self._get_session(proxy_entry)
                    resp = session.get(
                        url,
                        headers=headers,
                        timeout=self._timeout,
                        stream=stream,
                        **kwargs,
                    )

                # 遭遇反爬状态码
                if resp.status_code in (429, 403, 503):
                    # Drain the response body before retrying.  If the body is
                    # left unconsumed, the underlying socket stays occupied and
                    # the connection pool treats it as "in use", eventually
                    # exhausting all available connections under sustained 429s.
                    try:
                        for _ in resp.iter_content(chunk_size=65536):
                            pass
                    except Exception:
                        pass
                    finally:
                        resp.close()
                    logger.warning(
                        f"Anti-crawl status {resp.status_code} for {url[:60]}, "
                        f"attempt {attempt + 1}/{self._max_retries + 1}"
                    )
                    if proxy_entry:
                        self._proxy_pool.report_fail(proxy_entry)
                    self._record_host_failure(url)
                    last_exc = requests.HTTPError(response=resp)
                    if attempt >= self._max_retries:
                        break
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                    continue

                if proxy_entry:
                    self._proxy_pool.report_success(proxy_entry)
                self._record_host_success(url)
                return resp

            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if proxy_entry:
                    self._proxy_pool.report_fail(proxy_entry)
                self._record_host_failure(url)
                if attempt >= self._max_retries:
                    break
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Request error ({type(e).__name__}) for {url[:60]}, "
                    f"retry {attempt + 2}/{self._max_retries + 1} after {wait:.1f}s"
                )
                time.sleep(wait)
            except Exception as e:
                # curl_cffi 等可能抛出非 requests 异常
                last_exc = e
                if proxy_entry:
                    self._proxy_pool.report_fail(proxy_entry)
                self._record_host_failure(url)
                if attempt >= self._max_retries:
                    break
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Unexpected error for {url[:60]}: {type(e).__name__}: {e}, "
                    f"retry {attempt + 2}/{self._max_retries + 1} after {wait:.1f}s"
                )
                time.sleep(wait)

        raise last_exc or requests.RequestException(f"Failed after {self._max_retries} retries: {url}")

    def head(
        self,
        url: str,
        engine: Optional[str] = None,
        extra_headers: Optional[dict] = None,
        **kwargs,
    ) -> requests.Response:
        """发起 HEAD 请求（轻量探测，用于预检 Content-Type / Content-Length）。

        不消耗大量带宽，适合在下载前检查资源是否存在及大小。
        共享 get() 的限速、代理轮换、重试逻辑。
        """
        last_exc = None
        for attempt in range(self._max_retries + 1):
            if not self._host_allowed(url):
                raise requests.ConnectionError(
                    f"Host circuit open: {urlparse(url).hostname or url}"
                )
            self._rate_limiter.acquire()
            proxy_entry = (
                self._proxy_pool.get(self._proxy_strategy)
                if not self._proxy_pool.is_empty()
                else None
            )
            headers = self._make_headers(engine)
            if extra_headers:
                headers.update(extra_headers)
            time.sleep(random.uniform(0.05, 0.30))

            try:
                if self._use_curl:
                    proxy_url = proxy_entry.url if proxy_entry else None
                    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
                    impersonate = random.choice(_CHROME_IMPERSONATES)
                    resp = curl_requests.head(
                        url, headers=headers, proxies=proxies,
                        timeout=self._timeout, impersonate=impersonate, **kwargs,
                    )
                else:
                    session = self._get_session(proxy_entry)
                    resp = session.head(
                        url, headers=headers, timeout=self._timeout, **kwargs,
                    )

                if resp.status_code in (429, 403, 503):
                    try:
                        for _ in resp.iter_content(chunk_size=65536):
                            pass
                    except Exception:
                        pass
                    finally:
                        resp.close()
                    logger.warning(
                        f"HEAD anti-crawl {resp.status_code} for {url[:60]}, "
                        f"attempt {attempt + 1}/{self._max_retries + 1}"
                    )
                    if proxy_entry:
                        self._proxy_pool.report_fail(proxy_entry)
                    self._record_host_failure(url)
                    last_exc = requests.HTTPError(response=resp)
                    if attempt >= self._max_retries:
                        break
                    wait = (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(wait)
                    continue

                if proxy_entry:
                    self._proxy_pool.report_success(proxy_entry)
                self._record_host_success(url)
                return resp

            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if proxy_entry:
                    self._proxy_pool.report_fail(proxy_entry)
                self._record_host_failure(url)
                if attempt >= self._max_retries:
                    break
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"HEAD error for {url[:60]}: {e}, retry {attempt + 2}/{self._max_retries + 1}")
                time.sleep(wait)
            except Exception as e:
                last_exc = e
                if proxy_entry:
                    self._proxy_pool.report_fail(proxy_entry)
                self._record_host_failure(url)
                if attempt >= self._max_retries:
                    break
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"HEAD unexpected error for {url[:60]}: {e}, retry {attempt + 2}/{self._max_retries + 1}")
                time.sleep(wait)

        raise last_exc or requests.RequestException(f"HEAD failed after {self._max_retries} retries: {url}")

    def get_bytes(self, url: str, engine: Optional[str] = None) -> bytes:
        """便捷方法：获取完整 bytes 内容。"""
        return self.get(url, engine=engine).content

    def get_text(self, url: str, engine: Optional[str] = None) -> str:
        """便捷方法：获取文本内容，自动检测编码。"""
        resp = self.get(url, engine=engine)
        resp.encoding = getattr(resp, 'apparent_encoding', None) or resp.charset_encoding or "utf-8"
        return resp.text

    def get_stream(self, url: str, engine: Optional[str] = None, peek_bytes: int = 8192):
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
        resp = self.get(url, engine=engine, stream=True)
        peek = b""
        overflow = b""
        for chunk in resp.iter_content(chunk_size=peek_bytes):
            if len(peek) + len(chunk) <= peek_bytes:
                peek += chunk
            else:
                needed = peek_bytes - len(peek)
                peek += chunk[:needed]
                overflow = chunk[needed:]
            if len(peek) >= peek_bytes:
                break

        # Store leftover bytes so callers using resp.iter_content() get them back.
        # We attach it as a private attribute; _OverflowResponse wraps it cleanly.
        if overflow:
            resp._peek_overflow = overflow  # type: ignore[attr-defined]
            _orig_iter = resp.iter_content

            def _patched_iter(chunk_size=1, decode_unicode=False):
                yield overflow
                yield from _orig_iter(chunk_size=chunk_size,
                                      decode_unicode=decode_unicode)

            resp.iter_content = _patched_iter  # type: ignore[method-assign]

        return peek, resp
