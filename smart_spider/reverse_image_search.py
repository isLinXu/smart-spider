# coding=utf-8
"""通过真实网页上传本地图片并执行远程以图搜图。

该模块与 :mod:`smart_spider.image_retrieval` 的本地向量检索保持独立：

* ``ImageSimilarityIndex`` 只在本地图片集合中检索；
* ``ReverseImageSearcher`` 明确把查询图片上传到外部网页，并解析网页结果。

网页反向搜图通常没有稳定的跨平台公开 API，因此这里采用 Playwright 驱动
用户可见的上传页面。Provider 只负责站点差异，调用方拿到统一的结果契约。
不包含验证码绕过、登录绕过或隐藏接口调用；遇到验证页面会返回明确错误。
"""
from __future__ import annotations

import html as html_module
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol
from urllib.parse import parse_qs, unquote, urljoin, urlparse


try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - exercised by dependency error tests
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment,misc]
    sync_playwright = None  # type: ignore[assignment]

try:
    from playwright_stealth import Stealth
except ImportError:  # optional; regular Playwright remains supported
    Stealth = None  # type: ignore[assignment,misc]


SUPPORTED_PROVIDERS = ("baidu", "bing", "google_lens")
PROVIDER_ALIASES = {
    "google": "google_lens",
    "lens": "google_lens",
}


class ReverseImageSearchError(RuntimeError):
    """远程以图搜图失败。"""


class BrowserDependencyError(ReverseImageSearchError):
    """Playwright 或 Chromium 未安装。"""


class ProviderBlockedError(ReverseImageSearchError):
    """Provider 返回了验证码、登录或其他阻断页面。"""


@dataclass(frozen=True)
class RemoteImageSearchResult:
    """统一的一条远程以图搜图结果。"""

    provider: str
    title: str = ""
    source_url: str = ""
    image_url: str = ""
    thumbnail_url: str = ""
    snippet: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, rank: int = 0) -> dict[str, Any]:
        result: dict[str, Any] = {
            "provider": self.provider,
            "title": self.title,
            "source_url": self.source_url,
            "image_url": self.image_url,
            "thumbnail_url": self.thumbnail_url,
            "snippet": self.snippet,
            "metadata": dict(self.metadata),
        }
        if rank > 0:
            result["rank"] = rank
        return result


@dataclass(frozen=True)
class ProviderSearchResponse:
    """单个 Provider 的执行结果。"""

    provider: str
    query_image: str
    result_page_url: str = ""
    results: tuple[RemoteImageSearchResult, ...] = ()
    elapsed_ms: int = 0
    attempts: int = 1
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "query_image": self.query_image,
            "result_page_url": self.result_page_url,
            "elapsed_ms": self.elapsed_ms,
            "attempts": self.attempts,
            "results": [
                result.to_dict(rank=index)
                for index, result in enumerate(self.results, start=1)
            ],
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class ReverseImageSearchResponse:
    """一次查询的完整结果。"""

    query_image: str
    providers: tuple[ProviderSearchResponse, ...]
    searched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_image": self.query_image,
            "searched_at": self.searched_at,
            "providers": [provider.to_dict() for provider in self.providers],
        }


class ReverseImageSearchProvider(Protocol):
    """Provider 最小协议，便于新增站点或注入测试实现。"""

    name: str
    start_url: str

    def upload(self, page: Any, image_path: str, timeout_ms: int) -> None:
        ...

    def parse_results(
        self,
        html: str,
        *,
        page_url: str,
        top_k: int,
    ) -> list[RemoteImageSearchResult]:
        ...


def normalize_provider_name(name: str) -> str:
    """将 CLI 别名归一化为内部 Provider 名称。"""
    normalized = name.strip().lower().replace("-", "_")
    return PROVIDER_ALIASES.get(normalized, normalized)


def resolve_providers(names: Optional[Iterable[str]]) -> tuple[str, ...]:
    """解析 Provider 列表，支持 ``all``、别名和去重。"""
    requested = list(names or ("all",))
    resolved: list[str] = []
    for name in requested:
        normalized = normalize_provider_name(name)
        if normalized == "all":
            candidates = list(SUPPORTED_PROVIDERS)
        else:
            candidates = [normalized]
        for candidate in candidates:
            if candidate not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"unknown reverse image provider '{name}'; "
                    f"choose from {', '.join(SUPPORTED_PROVIDERS)}"
                )
            if candidate not in resolved:
                resolved.append(candidate)
    return tuple(resolved)


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_module.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _resolve_url(value: str, page_url: str) -> str:
    value = html_module.unescape(value).replace("\\/", "/").strip()
    if not value:
        return ""
    if value.startswith("//"):
        parsed = urlparse(page_url)
        return f"{parsed.scheme or 'https'}:{value}"
    if value.startswith(("/", "?")):
        return urljoin(page_url, value)
    return value


def _unwrap_redirect_url(value: str) -> str:
    """提取 Google/Bing 常见跳转链接中的真实外部 URL。"""
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("url", "q", "u", "target"):
        candidate = query.get(key, [""])[0]
        candidate = unquote(candidate)
        if candidate.startswith(("http://", "https://")):
            return candidate
    return value


def _host_matches(host: str, blocked_hosts: set[str]) -> bool:
    host = host.lower().split(":", 1)[0]
    return any(host == blocked or host.endswith("." + blocked) for blocked in blocked_hosts)


def _detect_browser_executable() -> Optional[str]:
    """优先复用本机已安装的 Chrome/Chromium，便于离线安装 Playwright 内核。"""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("microsoft-edge"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


class _AnchorCollector(HTMLParser):
    """收集网页中的链接、图片和 data-* 属性，避免依赖 BeautifulSoup。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._anchor: Optional[dict[str, Any]] = None
        self._anchor_depth = 0
        self.records: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and self._anchor is None:
            self._anchor = {
                "href": attributes.get("href", ""),
                "data_url": attributes.get("data-url", ""),
                "text": [],
                "image_url": "",
                "image_alt": "",
                "attrs": attributes,
            }
            self._anchor_depth = 1
            return
        if self._anchor is None:
            return
        if tag.lower() == "a":
            self._anchor_depth += 1
        elif tag.lower() == "img":
            self._anchor["image_url"] = (
                attributes.get("src")
                or attributes.get("data-src")
                or attributes.get("data-iurl")
                or attributes.get("data-original")
                or ""
            )
            self._anchor["image_alt"] = attributes.get("alt", "")

    def handle_endtag(self, tag: str) -> None:
        if self._anchor is None or tag.lower() != "a":
            return
        self._anchor_depth -= 1
        if self._anchor_depth <= 0:
            record = dict(self._anchor)
            record["text"] = " ".join(record["text"])
            self.records.append(record)
            self._anchor = None
            self._anchor_depth = 0

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"].append(data)


def _parse_anchor_records(html: str) -> list[dict[str, Any]]:
    parser = _AnchorCollector()
    parser.feed(html)
    parser.close()
    return parser.records


def _generic_external_results(
    html: str,
    *,
    page_url: str,
    provider: str,
    blocked_hosts: set[str],
    top_k: int,
) -> list[RemoteImageSearchResult]:
    """从动态结果页的外部链接中提取统一结果。"""
    results: list[RemoteImageSearchResult] = []
    seen: set[tuple[str, str]] = set()
    for record in _parse_anchor_records(html):
        raw_source = record.get("href") or record.get("data_url") or ""
        source_url = _unwrap_redirect_url(_resolve_url(raw_source, page_url))
        if not source_url.startswith(("http://", "https://")):
            continue
        if _host_matches(urlparse(source_url).netloc, blocked_hosts):
            continue
        image_url = _resolve_url(record.get("image_url", ""), page_url)
        title = _clean_text(record.get("text", "")) or _clean_text(record.get("image_alt", ""))
        if not title and not image_url:
            continue
        key = (source_url, image_url)
        if key in seen:
            continue
        seen.add(key)
        results.append(RemoteImageSearchResult(
            provider=provider,
            title=title[:500],
            source_url=source_url,
            image_url=image_url if image_url.startswith(("http://", "https://")) else "",
            thumbnail_url=image_url if image_url.startswith(("http://", "https://")) else "",
            metadata={"source_domain": urlparse(source_url).netloc},
        ))
        if len(results) >= top_k:
            break
    return results


def _extract_tag_attribute(tag: str, attribute: str) -> str:
    pattern = rf"\b{re.escape(attribute)}\s*=\s*(['\"])(.*?)\1"
    match = re.search(pattern, tag, flags=re.IGNORECASE | re.DOTALL)
    return html_module.unescape(match.group(2)) if match else ""


def _parse_bing_results(html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
    """解析 Bing 图片页的 iusc 元数据。"""
    results: list[RemoteImageSearchResult] = []
    seen: set[tuple[str, str]] = set()
    # HTML 属性中 class 可能位于前后任意位置，逐个 a 标签再过滤更可靠。
    all_tags = re.findall(r"<a\b[^>]*>", html, flags=re.IGNORECASE | re.DOTALL)
    for tag in all_tags:
        if not re.search(r"\bclass\s*=\s*(['\"])[^'\"]*\biusc\b[^'\"]*\1", tag, flags=re.IGNORECASE):
            continue
        raw_payload = _extract_tag_attribute(tag, "m")
        if not raw_payload:
            continue
        raw_payload = raw_payload.replace("&quot;", '"').replace("&#39;", "'")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        source_url = _resolve_url(str(payload.get("purl") or ""), page_url)
        image_url = _resolve_url(str(payload.get("murl") or ""), page_url)
        thumbnail_url = _resolve_url(str(payload.get("turl") or ""), page_url)
        source_url = _unwrap_redirect_url(source_url)
        if not source_url and image_url:
            source_url = image_url
        if not source_url.startswith(("http://", "https://")):
            continue
        key = (source_url, image_url)
        if key in seen:
            continue
        seen.add(key)
        results.append(RemoteImageSearchResult(
            provider="bing",
            title=_clean_text(str(payload.get("t") or payload.get("title") or ""))[:500],
            source_url=source_url,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            snippet=_clean_text(str(payload.get("desc") or ""))[:1000],
            metadata={
                "format": payload.get("fmt", ""),
                "source_domain": urlparse(source_url).netloc,
            },
        ))
        if len(results) >= top_k:
            return results
    return results or _generic_external_results(
        html,
        page_url=page_url,
        provider="bing",
        blocked_hosts={"bing.com", "microsoft.com"},
        top_k=top_k,
    )


def _parse_google_results(html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
    return _generic_external_results(
        html,
        page_url=page_url,
        provider="google_lens",
        blocked_hosts={"google.com", "googleusercontent.com", "gstatic.com"},
        top_k=top_k,
    )


def _parse_baidu_results(html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
    return _generic_external_results(
        html,
        page_url=page_url,
        provider="baidu",
        # 保留 baijiahao.baidu.com 等百度内容来源，避免把 Provider 自身
        # 的 graph.baidu.com 导航和相似图跳转误当成外部来源。
        blocked_hosts={"graph.baidu.com", "image.baidu.com", "www.baidu.com", "baidubce.com"},
        top_k=top_k,
    )


class _BaseBrowserProvider:
    """共享 Playwright 上传逻辑。"""

    name = ""
    start_url = ""
    start_urls: tuple[str, ...] = ()
    input_selectors = ("input[type='file']",)
    trigger_selectors: tuple[str, ...] = ()
    blocked_hosts: set[str] = set()
    result_selectors: tuple[str, ...] = ()

    def upload(self, page: Any, image_path: str, timeout_ms: int) -> None:
        for selector in self.input_selectors:
            locator = page.locator(selector)
            if locator.count() > 0:
                locator.first.set_input_files(image_path)
                return
        for selector in self.trigger_selectors:
            try:
                trigger = page.locator(selector)
                if trigger.count() == 0:
                    continue
                with page.expect_file_chooser(timeout=timeout_ms) as chooser_info:
                    trigger.first.click()
                chooser_info.value.set_files(image_path)
                return
            except PlaywrightTimeoutError:
                continue
        raise ReverseImageSearchError(
            f"{self.name}: no image upload input found; the provider page may have changed"
        )

    def wait_for_results(
        self,
        page: Any,
        timeout_ms: int,
        settle_ms: int,
        *,
        initial_url: str = "",
    ) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        appeared = False
        while time.monotonic() < deadline:
            if initial_url and page.url != initial_url:
                appeared = True
                break
            if any(page.locator(selector).count() > 0 for selector in self.result_selectors):
                appeared = True
                break
            page.wait_for_timeout(250)
        try:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
        except PlaywrightTimeoutError:
            pass
        if not appeared:
            raise ReverseImageSearchError(
                f"{self.name}: results did not appear within {timeout_ms} ms"
            )
        page.wait_for_timeout(settle_ms)

    def parse_results(self, html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
        raise NotImplementedError


class BaiduReverseImageProvider(_BaseBrowserProvider):
    name = "baidu"
    start_url = "https://graph.baidu.com/pcpage/index?tpl_from=pc"
    start_urls = (start_url,)
    input_selectors = (
        "input[type='file']",
        "input[name='image']",
        "#sttb",
    )
    trigger_selectors = ("text=上传图片", "text=识图", "[aria-label*='上传']")
    blocked_hosts = {"baidu.com", "baidubce.com"}
    result_selectors = (
        ".general-imgcol-item",
        "a[href*='douyin.com']",
        "a[href*='baijiahao.baidu.com']",
    )

    def parse_results(self, html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
        return _parse_baidu_results(html, page_url=page_url, top_k=top_k)


class BingVisualSearchProvider(_BaseBrowserProvider):
    name = "bing"
    # 直接进入 Bing 的“上传图片”视图；支持 Visual Search 的区域会保留
    # 此入口，其他区域可能重定向到普通图片搜索页并返回可诊断错误。
    start_url = "https://www.bing.com/images/search?view=detailv2&iss=sbiupload"
    start_urls = (
        start_url,
        "https://www.bing.com/images/searchbyimage?cbir=sbi",
    )
    input_selectors = (
        "input[type='file']",
        "#sb_fileinput",
        "input[name='imgurl']",
    )
    trigger_selectors = ("#sb_sbi", "text=Visual Search", "[aria-label*='image']")
    blocked_hosts = {"bing.com", "microsoft.com"}
    result_selectors = ("a.iusc", "#b_results")

    def parse_results(self, html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
        return _parse_bing_results(html, page_url=page_url, top_k=top_k)


class GoogleLensProvider(_BaseBrowserProvider):
    name = "google_lens"
    start_url = "https://lens.google.com/"
    start_urls = (start_url, "https://lens.google.com/uploadbyurl")
    input_selectors = ("input[type='file']",)
    trigger_selectors = ("text=Upload", "text=上传", "[aria-label*='Upload']")
    blocked_hosts = {"google.com", "googleusercontent.com", "gstatic.com"}
    # Lens 结果页的 URL 通常会变化；通用外链选择器会在首页导航中提前命中，
    # 因此这里仅依赖 URL 变化，避免过早解析空结果。
    result_selectors = ()

    def parse_results(self, html: str, *, page_url: str, top_k: int) -> list[RemoteImageSearchResult]:
        return _parse_google_results(html, page_url=page_url, top_k=top_k)


PROVIDER_REGISTRY: dict[str, type[_BaseBrowserProvider]] = {
    "baidu": BaiduReverseImageProvider,
    "bing": BingVisualSearchProvider,
    "google_lens": GoogleLensProvider,
}


def _blocked_page_reason(page_text: str) -> Optional[str]:
    normalized = re.sub(r"\s+", " ", page_text).lower()
    markers = (
        "captcha",
        "verify you are human",
        "unusual traffic",
        "请完成安全验证",
        "安全验证",
    )
    if any(marker in normalized for marker in markers):
        return "provider returned a CAPTCHA or human-verification page"
    return None


class ReverseImageSearcher:
    """使用 Playwright 逐个调用远程反向搜图网页。"""

    def __init__(
        self,
        providers: Optional[Iterable[str]] = None,
        *,
        headless: bool = True,
        proxy: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        browser_executable: Optional[str] = None,
        debug_dir: Optional[str] = None,
        timeout_ms: int = 45_000,
        settle_ms: int = 4_000,
        max_attempts: int = 2,
    ) -> None:
        self.provider_names = resolve_providers(providers)
        if timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if settle_ms < 0:
            raise ValueError("settle_ms must be non-negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.headless = headless
        self.proxy = proxy
        self.user_data_dir = user_data_dir
        self.browser_executable = browser_executable or _detect_browser_executable()
        self.debug_dir = debug_dir
        self.timeout_ms = timeout_ms
        self.settle_ms = settle_ms
        self.max_attempts = max_attempts

    def _provider(self, name: str) -> _BaseBrowserProvider:
        return PROVIDER_REGISTRY[name]()

    def _save_debug(self, page: Any, provider_name: str, attempt: int = 1) -> None:
        if not self.debug_dir:
            return
        debug_root = Path(self.debug_dir).expanduser().resolve()
        debug_root.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", provider_name)
        suffix = f"-attempt-{attempt}"
        try:
            (debug_root / f"{safe_name}{suffix}.html").write_text(
                page.content(), encoding="utf-8"
            )
            page.screenshot(
                path=str(debug_root / f"{safe_name}{suffix}.png"),
                full_page=True,
            )
        except Exception:
            # 调试产物不能覆盖原始 Provider 错误。
            pass

    def _search_one(
        self,
        context: Any,
        provider: _BaseBrowserProvider,
        image_path: str,
        top_k: int,
        start_url: str,
        attempt: int = 1,
    ) -> ProviderSearchResponse:
        started = time.monotonic()
        page = context.new_page()
        page.set_default_timeout(self.timeout_ms)
        try:
            if Stealth is not None:
                Stealth().apply_stealth_sync(page)
            page.goto(start_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            initial_url = page.url
            provider.upload(page, image_path, self.timeout_ms)
            provider.wait_for_results(
                page,
                self.timeout_ms,
                self.settle_ms,
                initial_url=initial_url,
            )
            page_text = page.locator("body").inner_text(timeout=self.timeout_ms)
            blocked_reason = _blocked_page_reason(page_text)
            if blocked_reason:
                raise ProviderBlockedError(blocked_reason)
            results = provider.parse_results(
                page.content(),
                page_url=page.url,
                top_k=top_k,
            )
            return ProviderSearchResponse(
                provider=provider.name,
                query_image=image_path,
                result_page_url=page.url,
                results=tuple(results),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except (ReverseImageSearchError, PlaywrightTimeoutError) as exc:
            self._save_debug(page, provider.name, attempt)
            return ProviderSearchResponse(
                provider=provider.name,
                query_image=image_path,
                result_page_url=page.url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )
        except Exception as exc:  # provider failures must not hide other providers
            self._save_debug(page, provider.name, attempt)
            return ProviderSearchResponse(
                provider=provider.name,
                query_image=image_path,
                result_page_url=page.url,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            page.close()

    def _search_provider(
        self,
        context: Any,
        provider: _BaseBrowserProvider,
        image_path: str,
        top_k: int,
    ) -> ProviderSearchResponse:
        """按 Provider 的备用入口重试，并返回聚合后的诊断信息。"""
        start_urls = tuple(getattr(provider, "start_urls", ()) or ())
        if not start_urls:
            start_urls = (provider.start_url,)
        started = time.monotonic()
        last_response: Optional[ProviderSearchResponse] = None
        for attempt in range(1, self.max_attempts + 1):
            start_url = start_urls[(attempt - 1) % len(start_urls)]
            response = self._search_one(
                context,
                provider,
                image_path,
                top_k,
                start_url=start_url,
                attempt=attempt,
            )
            response = replace(
                response,
                attempts=attempt,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            last_response = response
            if response.results:
                return response

        if last_response is None:  # max_attempts is validated in __init__
            raise AssertionError("provider search completed without an attempt")
        if not last_response.error:
            return replace(
                last_response,
                error=(
                    f"{provider.name}: no parsed results found after "
                    f"{self.max_attempts} attempt(s)"
                ),
            )
        return last_response

    def search(self, image_path: str, *, top_k: int = 20, fail_fast: bool = False) -> ReverseImageSearchResponse:
        """上传本地图片并返回各 Provider 的结果。"""
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"query image not found: {path}")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if sync_playwright is None:
            raise BrowserDependencyError(
                "Playwright is not installed; run `pip install 'smart-spider[browser]'` "
                "and `playwright install chromium`"
            )

        browser = None
        context = None
        providers: list[ProviderSearchResponse] = []
        try:
            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {"headless": self.headless}
                if self.proxy:
                    launch_kwargs["proxy"] = {"server": self.proxy}
                if self.browser_executable:
                    launch_kwargs["executable_path"] = self.browser_executable
                if self.user_data_dir:
                    user_dir = str(Path(self.user_data_dir).expanduser().resolve())
                    Path(user_dir).mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        user_dir,
                        **launch_kwargs,
                    )
                else:
                    browser = playwright.chromium.launch(**launch_kwargs)
                    context = browser.new_context(locale="zh-CN")
                for provider_name in self.provider_names:
                    provider = self._provider(provider_name)
                    response = self._search_provider(context, provider, str(path), top_k)
                    providers.append(response)
                    if fail_fast and response.error:
                        break
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
        except Exception:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
            raise
        return ReverseImageSearchResponse(
            query_image=str(path),
            providers=tuple(providers),
            searched_at=datetime.now(timezone.utc).isoformat(),
        )


__all__ = [
    "SUPPORTED_PROVIDERS",
    "PROVIDER_ALIASES",
    "BaiduReverseImageProvider",
    "BingVisualSearchProvider",
    "GoogleLensProvider",
    "ProviderSearchResponse",
    "RemoteImageSearchResult",
    "ReverseImageSearchError",
    "BrowserDependencyError",
    "ProviderBlockedError",
    "ReverseImageSearchProvider",
    "ReverseImageSearchResponse",
    "ReverseImageSearcher",
    "normalize_provider_name",
    "resolve_providers",
]
