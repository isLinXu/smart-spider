"""Douyin discovery and downloads using browser-visible data, without API signing."""
from __future__ import annotations

import json
import re
from html import unescape
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from loguru import logger

DOUYIN_HOME = "https://www.douyin.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def _douyin_host(host: str) -> bool:
    return any(host == root or host.endswith("." + root)
               for root in ("douyin.com", "iesdouyin.com"))


def normalize_input(value: str, *, keyword: bool = False) -> str:
    """Accept a search term, a numeric aweme ID, or copied share text."""
    value = value.strip()
    if not value:
        raise ValueError("请输入抖音关键词、视频链接或用户主页")
    if keyword:
        return DOUYIN_HOME + "search/" + quote(value, safe="") + "?type=video"
    if re.fullmatch(r"\d{10,30}", value):
        return DOUYIN_HOME + "video/" + value
    match = re.search(r'https?://[^\s<>"，。；！）]+', value)
    if not match:
        raise ValueError("未找到抖音链接；关键词请使用 --keyword")
    url = match.group().rstrip(").,;!")
    parts = urlsplit(url)
    if (not _douyin_host(parts.hostname or "") or parts.username or parts.password
            or parts.port not in (None, 80, 443)):
        raise ValueError("只支持 douyin.com / iesdouyin.com 的抖音链接")
    if parts.hostname == "v.douyin.com":
        return url
    if re.match(r"^/(?:video/\d+|share/video/\d+|user/[^/]+|share/user/[^/]+)", parts.path):
        return url
    if parse_qs(parts.query).get("modal_id", [""])[0].isdigit():
        return url
    raise ValueError("请提供视频详情、分享短链接或用户主页链接")


def video_id(url: str) -> str:
    parts = urlsplit(url)
    if not _douyin_host(parts.hostname or ""):
        return ""
    match = re.search(r"/(?:share/)?video/(\d+)(?:/|$)", parts.path)
    candidate = match.group(1) if match else parse_qs(parts.query).get("modal_id", [""])[0]
    return candidate if candidate.isdigit() else ""


def load_browser_cookies(path: str | None) -> list[dict]:
    """Read the same Netscape cookie file accepted by yt-dlp, respecting expiry."""
    if not path:
        return []
    jar = MozillaCookieJar(path)
    jar.load(ignore_discard=True, ignore_expires=True)
    return [dict(name=c.name, value=c.value, domain=c.domain, path=c.path or "/",
                 secure=c.secure, **({"expires": c.expires} if c.expires else {}))
            for c in jar if _douyin_host(c.domain.lstrip("."))
            and (not c.expires or not c.is_expired())]


def _first_url(value) -> str:
    # Web hydration uses [{src: ...}], while the API uses {url_list: [...]}.
    if isinstance(value, str):
        if value.startswith("//"):
            return "https:" + value
        return value if value.startswith(("https://", "http://")) else ""
    if isinstance(value, dict):
        value = value.get("url_list", value.get("urlList", value.get("src", [])))
        if isinstance(value, str):
            return _first_url(value)
    if isinstance(value, list):
        for entry in value:
            url = _first_url(entry)
            if url:
                return url
    return ""


def parse_payload(payload) -> list[dict]:
    """Normalize current API/data-island and legacy item_list video objects."""
    found: dict[str, dict] = {}
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
            continue
        if not isinstance(node, dict):
            continue
        identifier = str(node.get("aweme_id") or node.get("awemeId") or "")
        video = node.get("video")
        if identifier.isdigit() and isinstance(video, dict):
            if node.get("images") or node.get("aweme_type") in (68, 150):
                continue  # 图集和直播不作为视频输出。
            author = node.get("author") or node.get("authorInfo") or {}
            if not isinstance(author, dict):
                author = {}
            duration = video.get("duration", node.get("duration", 0))
            try:
                duration = float(duration or 0) / 1000
            except (ValueError, TypeError):
                duration = 0
            media_url = _first_url(video.get("play_addr") or video.get("playAddr"))
            if not media_url:
                media_url = _first_url(video.get("download_addr"))
            found.setdefault(identifier, {
                "url": DOUYIN_HOME + "video/" + identifier,
                "meta": {"aweme_id": identifier, "title": str(node.get("desc") or ""),
                         "author": author.get("nickname", author.get("nickName", "")),
                         "author_id": author.get("sec_uid", author.get("secUid", "")),
                         "duration": duration, "width": video.get("width", 0),
                         "height": video.get("height", 0),
                         "thumb": _first_url(video.get("cover")),
                         "create_time": node.get("create_time", node.get("createTime", 0)),
                         "media_url": media_url},
            })
            continue
        stack.extend(reversed(list(node.values())))
    return list(found.values())


def parse_page(html: str) -> list[dict]:
    try:
        return parse_payload(json.loads(html))
    except (ValueError, TypeError):
        pass
    items = {}
    for script in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I):
        # Decode only as a fallback: descriptions may contain literal %xx or &quot;.
        for text in (script.strip(), unescape(script.strip()), unquote(unescape(script.strip()))):
            text = re.sub(r"^\s*(?:window\.)?_ROUTER_DATA\s*=\s*", "", text).rstrip("; \n")
            try:
                for item in parse_payload(json.loads(text)):
                    items[item["url"]] = item
                break
            except (ValueError, TypeError):
                continue
    return list(items.values())


class DouyinCrawler:
    """One bounded browser session per discovery; scrolls retain response history."""

    def __init__(self, *, cookies_file=None, cookies=None, headless=True, proxy=None,
                 timeout=30, max_scrolls=50, login_wait=0, url_policy=None,
                 browser_channel=None, login_confirmation=None):
        from .url_policy import URLPolicy
        if timeout <= 0 or max_scrolls < 0 or login_wait < 0:
            raise ValueError("timeout 必须大于 0，max_scrolls 和 login_wait 不能为负")
        if headless and login_wait:
            raise ValueError("--login-wait 需要 --no-headless")
        if headless and login_confirmation is not None:
            raise ValueError("人工确认需要显示浏览器")
        self.login_confirmation = login_confirmation
        self.cookies = load_browser_cookies(cookies_file) + list(cookies or [])
        if browser_channel not in (None, "chrome", "msedge"):
            raise ValueError("browser_channel 必须为 chrome 或 msedge")
        self.browser_channel = browser_channel
        self.headless, self.proxy = headless, proxy
        self.timeout, self.max_scrolls, self.login_wait = timeout, max_scrolls, login_wait
        self.url_policy = url_policy or URLPolicy()
        self.diagnostics = {}

    def discover(self, value: str, *, limit=50, keyword=False) -> list[dict]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        url = normalize_input(value, keyword=keyword)
        self.diagnostics = {"page_title": "", "responses": [], "discovered": 0}
        self.url_policy.validate(url)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError('请安装 pip install -e ".[browser,video]" 并执行 playwright install chromium') from exc
        from .browser import _validate_browser_request
        found = {}
        target_id = video_id(url)

        def add(items):
            for item in items:
                identifier = item["meta"]["aweme_id"]
                if target_id and identifier != target_id:
                    continue
                if identifier in found or len(found) < limit:
                    previous = found.get(identifier)
                    if not previous or item["meta"].get("media_url"):
                        found[identifier] = item

        def on_response(response):
            parts = urlsplit(response.url)
            if not _douyin_host(parts.hostname or ""):
                return
            record = None
            if "/search/" in parts.path or "/aweme/" in parts.path:
                record = {"path": parts.path, "status": response.status, "parsed_videos": 0}
                if len(self.diagnostics["responses"]) < 100:
                    self.diagnostics["responses"].append(record)
            if not any(p in parts.path for p in (
                "/aweme/v1/web/aweme/post/", "/aweme/v1/web/aweme/detail/",
                "/aweme/v1/web/general/search/", "/aweme/v1/web/search/item/",
                "/web/api/v2/aweme/", "/aweme/v1/web/search/stream/",
            )):
                return
            try:
                if response.status == 200 and int(response.headers.get("content-length", "0")) <= 8 * 1024 * 1024:
                    payload = response.json()
                    items = parse_payload(payload)
                    if record is not None:
                        record["parsed_videos"] = len(items)
                        record["payload_type"] = type(payload).__name__
                        if isinstance(payload, dict):
                            record["keys"] = list(payload)[:30]
                            status = payload.get("status_code")
                            if isinstance(status, (str, int)):
                                record["status_code"] = status
                            data = payload.get("data")
                            record["data_type"] = type(data).__name__
                            if isinstance(data, list):
                                record["data_count"] = len(data)
                    add(items)
            except Exception as exc:
                if record is not None:
                    record["parse_error"] = type(exc).__name__
                # Empty/non-JSON responses are common during login and verification.
                return

        with sync_playwright() as playwright:
            options = {"headless": self.headless}
            if self.browser_channel:
                options["channel"] = self.browser_channel
            if self.proxy:
                options["proxy"] = {"server": self.proxy}
            browser = playwright.chromium.launch(**options)
            try:
                context = browser.new_context(user_agent=USER_AGENT, locale="zh-CN", service_workers="block")
                context.route("**/*", lambda route: route.continue_()
                              if _validate_browser_request(route.request.url, self.url_policy)
                              else route.abort())
                if self.cookies:
                    context.add_cookies(self.cookies)
                page = context.new_page()
                page.on("response", on_response)
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                if not _douyin_host(urlsplit(page.url).hostname or ""):
                    raise RuntimeError("抖音分享链接跳转到了非抖音页面")
                target_id = target_id or video_id(page.url)
                if target_id:
                    found = {key: item for key, item in found.items() if key == target_id}
                if self.login_confirmation is not None:
                    self.login_confirmation(page)
                    if keyword:
                        # The initial anonymous request may have completed before login.
                        # Reissue the search in the now-authorized context.
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                elif self.login_wait:
                    logger.info("请在浏览器内登录或完成验证，等待 {} 秒", self.login_wait)
                    page.wait_for_timeout(self.login_wait * 1000)
                idle = 0
                for scroll in range(self.max_scrolls + 1):
                    previous = len(found)
                    page.wait_for_timeout(2000)
                    add(parse_page(page.content()))
                    if len(found) >= limit or (target_id and target_id in found):
                        break
                    idle = idle + 1 if len(found) == previous else 0
                    if idle >= 4 or scroll == self.max_scrolls:
                        break
                    # Douyin uses nested scrolling containers, not only window.scrollY.
                    page.mouse.move(800, 600)
                    page.mouse.wheel(0, 1800)
                title = page.title()
                self.diagnostics["page_title"] = title if isinstance(title, str) else ""
                self.diagnostics["discovered"] = len(found)
                if not found:
                    if isinstance(title, str) and ("验证" in title or "captcha" in title.lower()):
                        raise RuntimeError("抖音返回验证码页面；请使用 --no-headless --login-wait 120 手动完成验证，或传入有效的 --cookies-file")
                    raise RuntimeError("未发现抖音视频：可能需要登录/验证、作品不可用或页面接口已变化；可传入 --cookies-file，或 --no-headless --login-wait 120")
                logger.info("抖音发现 {} 条不同视频，目标上限 {} 条", len(found), limit)
                return list(found.values())[:limit]
            finally:
                browser.close()


def _byte_limit(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)b?", str(value).strip(), re.I)
    if not match:
        raise ValueError("视频大小格式应为 500m、1g 或字节数")
    size = int(float(match[1]) * 1024 ** ("kmgt".index(match[2].lower()) + 1 if match[2] else 0))
    if size <= 0:
        raise ValueError("视频大小必须大于 0")
    return size


def download_item(item: dict, out_path: str, *, downloader, http_client) -> bool:
    """Download captured MP4, or fall back to yt-dlp when no media URL exists."""
    meta = item["meta"]
    identifier = str(meta.get("aweme_id", ""))
    if not identifier.isdigit() or video_id(item["url"]) != identifier:
        raise ValueError("无效的抖音视频 ID")
    folder = Path(out_path) / identifier
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (identifier + ".mp4")
    temporary = destination.with_suffix(".mp4.part")
    media_url = meta.get("media_url")
    try:
        if media_url:
            limit = _byte_limit(downloader.max_filesize)
            peek, response = http_client.get_stream(media_url, engine="douyin", max_bytes=limit)
            try:
                response.raise_for_status()
                if len(peek) < 12 or peek[4:8] != b"ftyp":
                    raise ValueError("抖音下载响应不是 MP4 视频")
                total = len(peek)
                with temporary.open("wb") as handle:
                    handle.write(peek)
                    for chunk in response.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > limit:
                            raise ValueError("视频超过大小限制")
                        handle.write(chunk)
                declared = response.headers.get("Content-Length")
                if declared and not response.headers.get("Content-Encoding") and total != int(declared):
                    raise ValueError("视频下载不完整")
                temporary.replace(destination)
            finally:
                response.close()
        else:
            if not downloader.download(item["url"], str(folder)):
                return False
            if not any(p.is_file() and p.stat().st_size > 0 and p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}
                       for p in folder.iterdir()):
                return False  # yt-dlp may exit 0 after skipping an oversized file.
        public_meta = {key: value for key, value in meta.items() if key != "media_url"}
        metadata = folder / (identifier + ".json")
        metadata.write_text(json.dumps({"source_url": item["url"], **public_meta}, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        logger.warning("抖音视频 {} 下载失败 ({})", identifier, reason)
        return False
    finally:
        temporary.unlink(missing_ok=True)
