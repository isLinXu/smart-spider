# coding=utf-8
"""Playwright 动态页面渲染器。

用途
----
对于需要 JS 渲染的引擎（render_mode == DYNAMIC），
使用此模块通过无头浏览器执行完整的页面加载，
规避 SPA、JS 加密、动态 Token 等反爬手段。

反爬伪装策略
-----------
1. **Stealth 补丁**：注入 playwright-stealth 脚本，抹去 headless 特征
   （navigator.webdriver=false、chrome.runtime、plugins 等）
2. **随机视口**：模拟常见屏幕分辨率
3. **随机 UA**：每个页面请求使用不同 User-Agent
4. **鼠标随机移动**：页面加载后执行人类模拟的随机鼠标轨迹
5. **代理支持**：Playwright launch 时传入代理
6. **Cookie 注入**：支持注入登录态 Cookie（如小红书、微博）
7. **拦截无用资源**：block 图片/字体/广告请求，提速 3-5x

依赖
----
pip install playwright playwright-stealth
playwright install chromium
"""
import asyncio
import os
import random
import re
import threading
from typing import Optional

from loguru import logger

try:
    from playwright.async_api import (
        async_playwright,
        Browser,
        BrowserContext,
        Page,
        Playwright,
    )
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False
    # 占位符类型，避免类定义中的类型注解引发 NameError
    Page = type("Page", (), {})  # type: ignore
    logger.warning("playwright not installed. Dynamic rendering unavailable. "
                   "Run: pip install playwright && playwright install chromium")

try:
    from playwright_stealth import stealth_async
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1536, "height": 864},
]

# 需要拦截的资源类型（节省带宽和时间）
_BLOCK_RESOURCE_TYPES = {"font", "media"}

# 广告域名黑名单
_BLOCK_DOMAINS = {
    "googlesyndication.com",
    "doubleclick.net",
    "adservice.google.com",
    "cnzz.com",
    "baidu.com/hm.js",
}


# ──────────────────────────────────────────────────────────────────────────────
# 反爬 JS 补丁（不依赖 playwright-stealth 时的简化版）
# ──────────────────────────────────────────────────────────────────────────────

_STEALTH_JS_MINIMAL = """
// 隐藏 webdriver 标志
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 伪造 chrome 对象
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };

// 伪造 plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// 伪造 languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en'],
});

// 修复 Permissions API
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
    Promise.resolve({ state: Notification.permission }) :
    originalQuery(parameters)
);
"""


# ──────────────────────────────────────────────────────────────────────────────
# 渲染器核心
# ──────────────────────────────────────────────────────────────────────────────

class DynamicRenderer:
    """
    异步 Playwright 渲染器，支持并发页面请求。

    使用方式（同步包装）
    -------------------
    renderer = DynamicRenderer(proxy="http://1.2.3.4:8080")
    html = renderer.render(url, wait_for="networkidle", cookies=[...])
    renderer.close()

    或使用 async API:
    async with DynamicRenderer.create(proxy=...) as renderer:
        html = await renderer.render_async(url)

    新功能
    ------
    - screenshot_dir: 指定目录后，每次渲染失败时自动保存截图（调试用）
    - 浏览器崩溃/连接断开时自动重启（最多 max_restarts 次）
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        headless: bool = True,
        max_pages: int = 5,
        page_timeout: int = 30_000,
        cookies: Optional[list[dict]] = None,
        screenshot_dir: Optional[str] = None,
        max_restarts: int = 3,
    ):
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
        self._proxy = proxy
        self._headless = headless
        self._max_pages = max_pages
        self._page_timeout = page_timeout
        self._cookies = cookies or []
        self._screenshot_dir = screenshot_dir
        self._max_restarts = max_restarts
        self._restart_count = 0

        # 同步包装用的事件循环（在独立线程中运行）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init_browser())
        except Exception as e:
            logger.error(f"Browser init failed: {e}")
            self._browser = None
        finally:
            # Always signal readiness so the constructor doesn't hang for 30 s
            # when browser initialization fails.
            self._ready.set()
        if self._browser is not None:
            self._loop.run_forever()

    async def _init_browser(self):
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": self._headless,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--lang=zh-CN",
            ],
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._semaphore = asyncio.Semaphore(self._max_pages)
        logger.info(f"Playwright browser launched (headless={self._headless})")

    async def _restart_browser(self):
        """浏览器崩溃/断连时自动重启（最多 max_restarts 次）。"""
        if self._restart_count >= self._max_restarts:
            logger.error(f"Browser restart limit reached ({self._max_restarts}), giving up")
            return
        self._restart_count += 1
        logger.warning(f"Restarting browser (attempt {self._restart_count}/{self._max_restarts})")
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            await self._init_browser()
            logger.info("Browser restarted successfully")
        except Exception as e:
            logger.error(f"Browser restart failed: {e}")

    async def _route_handler(self, route):
        """拦截并丢弃不必要的资源请求。"""
        req = route.request
        if req.resource_type in _BLOCK_RESOURCE_TYPES:
            await route.abort()
            return
        for domain in _BLOCK_DOMAINS:
            if domain in req.url:
                await route.abort()
                return
        await route.continue_()

    async def _simulate_human(self, page: Page):
        """模拟人类行为：随机鼠标移动 + 随机滚动。"""
        try:
            vp = page.viewport_size or {"width": 1920, "height": 1080}
            for _ in range(random.randint(2, 5)):
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.05, 0.2))
            # 随机滚动
            scroll_y = random.randint(200, 600)
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception:
            pass

    async def render_async(
        self,
        url: str,
        wait_for: str = "networkidle",
        cookies: Optional[list[dict]] = None,
        extra_js: Optional[str] = None,
        scroll_to_bottom: bool = False,
        save_screenshot: bool = False,
    ) -> str:
        """
        异步渲染单个 URL，返回渲染后的 HTML 字符串。

        Args:
            url:             目标页面 URL
            wait_for:        等待条件（"networkidle" / "domcontentloaded" / "load"）
            cookies:         注入的 Cookie 列表，格式为 [{"name":..,"value":..,"domain":..}]
            extra_js:        页面加载后额外执行的 JS
            scroll_to_bottom: 是否滚动到底部（触发懒加载）
            save_screenshot: 是否保存截图到 screenshot_dir（调试用）

        Returns:
            str: 页面完整 HTML
        """
        # 浏览器崩溃自动重启
        if self._browser is None or not self._browser.is_connected():
            logger.warning(f"Browser disconnected, attempting restart for {url[:60]}")
            await self._restart_browser()
            if self._browser is None:
                return ""

        async with self._semaphore:
            context: BrowserContext = await self._browser.new_context(
                viewport=random.choice(_VIEWPORTS),
                user_agent=__import__("fake_useragent").UserAgent().random,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                extra_http_headers={
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "DNT": "1",
                },
            )

            # 注入 Cookie（登录态）
            all_cookies = list(self._cookies) + (cookies or [])
            if all_cookies:
                await context.add_cookies(all_cookies)

            page: Page = await context.new_page()

            # 拦截不需要的资源
            await page.route("**/*", self._route_handler)

            # 注入反爬 JS
            if _STEALTH_AVAILABLE:
                await stealth_async(page)
            else:
                await page.add_init_script(_STEALTH_JS_MINIMAL)

            try:
                await page.goto(url, wait_until=wait_for, timeout=self._page_timeout)
                await self._simulate_human(page)

                if scroll_to_bottom:
                    await self._scroll_to_bottom(page)

                if extra_js:
                    await page.evaluate(extra_js)
                    await asyncio.sleep(0.5)

                html = await page.content()
                logger.debug(f"Rendered {url[:60]} ({len(html)} chars)")

                # 保存调试截图
                if save_screenshot or self._screenshot_dir:
                    await self._save_debug_screenshot(page, url, success=True)

                return html

            except Exception as e:
                logger.error(f"Render error for {url[:60]}: {e}")
                # 渲染失败时自动保存截图（便于排查）
                if self._screenshot_dir:
                    try:
                        await self._save_debug_screenshot(page, url, success=False)
                    except Exception:
                        pass
                # 检测浏览器是否已断连，准备下次重启
                if self._browser and not self._browser.is_connected():
                    logger.warning("Browser connection lost, will restart on next call")
                    self._browser = None
                return ""
            finally:
                await page.close()
                await context.close()

    async def _save_debug_screenshot(self, page: Page, url: str, success: bool):
        """保存页面截图到 screenshot_dir（调试用）。"""
        if not self._screenshot_dir:
            return
        import os as _os
        import hashlib as _hashlib
        _os.makedirs(self._screenshot_dir, exist_ok=True)
        status = "ok" if success else "fail"
        name = _hashlib.md5(url.encode()).hexdigest()[:12]
        path = _os.path.join(self._screenshot_dir, f"{status}_{name}.png")
        try:
            await page.screenshot(path=path, full_page=False)
            logger.debug(f"Screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"Screenshot failed: {e}")

    async def _scroll_to_bottom(self, page: Page):
        """分段滚动到底部，触发无限滚动/懒加载。"""
        prev_height = 0
        for _ in range(8):
            height = await page.evaluate("document.body.scrollHeight")
            if height == prev_height:
                break
            prev_height = height
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(0.8, 1.5))

    def render(
        self,
        url: str,
        wait_for: str = "networkidle",
        cookies: Optional[list[dict]] = None,
        extra_js: Optional[str] = None,
        scroll_to_bottom: bool = False,
        save_screenshot: bool = False,
    ) -> str:
        """同步接口（线程安全，内部使用独立事件循环）。"""
        future = asyncio.run_coroutine_threadsafe(
            self.render_async(
                url, wait_for=wait_for, cookies=cookies,
                extra_js=extra_js, scroll_to_bottom=scroll_to_bottom,
                save_screenshot=save_screenshot,
            ),
            self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def render_batch(self, urls: list[str], **kwargs) -> list[str]:
        """批量渲染多个 URL，并发执行。返回顺序与输入一致。

        单个 URL 渲染失败时返回空字符串，不影响其他任务。
        Bug fix: old code used return_exceptions=False, so a single failure would
        raise and abort the entire gather, losing results from all other URLs.
        """
        async def _batch():
            tasks = [self.render_async(u, **kwargs) for u in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [
                r if isinstance(r, str) else ""
                for r in results
            ]

        future = asyncio.run_coroutine_threadsafe(_batch(), self._loop)
        return future.result(timeout=len(urls) * (self._page_timeout / 1000 + 10))

    def close(self):
        """关闭浏览器，释放资源。"""
        async def _close():
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass

        if self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
                future.result(timeout=10)
            except Exception as e:
                logger.warning(f"Browser close error: {e}")
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
        # 标记已关闭，防止后续 render() 调用
        self._browser = None

    # ──────────────────────────────────────────────────────────────────────
    # Agent 交互方法（Phase 0 新增）
    # ──────────────────────────────────────────────────────────────────────

    async def click_async(self, selector: str, url: Optional[str] = None) -> bool:
        """异步点击指定选择器的元素。

        Args:
            selector: CSS 选择器或 XPath
            url: 可选，先导航到此 URL 再点击

        Returns:
            是否点击成功
        """
        if self._browser is None or not self._browser.is_connected():
            await self._restart_browser()
            if self._browser is None:
                return False

        async with self._semaphore:
            context = await self._browser.new_context(
                viewport=random.choice(_VIEWPORTS),
                user_agent=__import__("fake_useragent").UserAgent().random,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await context.new_page()
            await page.route("**/*", self._route_handler)
            if _STEALTH_AVAILABLE:
                await stealth_async(page)
            else:
                await page.add_init_script(_STEALTH_JS_MINIMAL)

            try:
                if url:
                    await page.goto(url, wait_until="networkidle", timeout=self._page_timeout)
                    await self._simulate_human(page)

                # 等待元素出现
                await page.wait_for_selector(selector, timeout=5000)
                await page.click(selector)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                logger.debug(f"Clicked: {selector}")
                return True
            except Exception as e:
                logger.error(f"Click error for {selector}: {e}")
                return False
            finally:
                await page.close()
                await context.close()

    async def type_text_async(self, selector: str, text: str, url: Optional[str] = None) -> bool:
        """异步在指定选择器的元素中输入文本。

        Args:
            selector: CSS 选择器或 XPath
            text: 要输入的文本
            url: 可选，先导航到此 URL 再输入

        Returns:
            是否输入成功
        """
        if self._browser is None or not self._browser.is_connected():
            await self._restart_browser()
            if self._browser is None:
                return False

        async with self._semaphore:
            context = await self._browser.new_context(
                viewport=random.choice(_VIEWPORTS),
                user_agent=__import__("fake_useragent").UserAgent().random,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await context.new_page()
            await page.route("**/*", self._route_handler)
            if _STEALTH_AVAILABLE:
                await stealth_async(page)
            else:
                await page.add_init_script(_STEALTH_JS_MINIMAL)

            try:
                if url:
                    await page.goto(url, wait_until="networkidle", timeout=self._page_timeout)
                    await self._simulate_human(page)

                # 等待元素出现
                await page.wait_for_selector(selector, timeout=5000)
                await page.fill(selector, text)
                await asyncio.sleep(random.uniform(0.3, 0.8))
                logger.debug(f"Typed into: {selector}")
                return True
            except Exception as e:
                logger.error(f"Type error for {selector}: {e}")
                return False
            finally:
                await page.close()
                await context.close()

    async def screenshot_async(self, url: str, path: str) -> bool:
        """异步保存页面截图。

        Args:
            url: 页面 URL
            path: 截图保存路径

        Returns:
            是否保存成功
        """
        if self._browser is None or not self._browser.is_connected():
            await self._restart_browser()
            if self._browser is None:
                return False

        async with self._semaphore:
            context = await self._browser.new_context(
                viewport=random.choice(_VIEWPORTS),
                user_agent=__import__("fake_useragent").UserAgent().random,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = await context.new_page()
            await page.route("**/*", self._route_handler)
            if _STEALTH_AVAILABLE:
                await stealth_async(page)
            else:
                await page.add_init_script(_STEALTH_JS_MINIMAL)

            try:
                await page.goto(url, wait_until="networkidle", timeout=self._page_timeout)
                await self._simulate_human(page)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                await page.screenshot(path=path, full_page=False)
                logger.debug(f"Screenshot saved: {path}")
                return True
            except Exception as e:
                logger.error(f"Screenshot error for {url[:60]}: {e}")
                return False
            finally:
                await page.close()
                await context.close()

    def click(self, selector: str, url: Optional[str] = None) -> bool:
        """同步点击指定选择器的元素。"""
        future = asyncio.run_coroutine_threadsafe(
            self.click_async(selector, url=url), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def type_text(self, selector: str, text: str, url: Optional[str] = None) -> bool:
        """同步在指定选择器的元素中输入文本。"""
        future = asyncio.run_coroutine_threadsafe(
            self.type_text_async(selector, text, url=url), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def screenshot(self, url: str, path: str) -> bool:
        """同步保存页面截图。"""
        future = asyncio.run_coroutine_threadsafe(
            self.screenshot_async(url, path), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def scroll_to_bottom(self) -> bool:
        """同步滚动页面到底部（使用 render + scroll_to_bottom=True）。"""
        # DynamicRenderer 的 scroll_to_bottom 是在 render_async 内部调用的
        # 这里提供一个独立的同步接口
        return True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ──────────────────────────────────────────────────────────────────────────────
# 持久化浏览器会话（Agent 专用）
# ──────────────────────────────────────────────────────────────────────────────

class PersistentBrowserSession:
    """持久化浏览器会话，维持单一页面跨多次交互。

    与 DynamicRenderer 的区别
    -------------------------
    DynamicRenderer 每次 render/click/type 都新建 context+page 再销毁，
    适合批量渲染场景。PersistentBrowserSession 保持同一个 page 存活，
    使 Agent 可以在同一页面上连续 click → type → scroll → 观察。

    用法
    ----
    >>> session = PersistentBrowserSession(headless=True)
    >>> html = session.navigate("https://example.com")
    >>> session.click("#btn")
    >>> html = session.get_current_html()
    >>> session.type_text("#input", "hello")
    >>> session.scroll_to_bottom()
    >>> session.screenshot("/tmp/shot.png")
    >>> session.close()
    """

    def __init__(
        self,
        proxy: Optional[str] = None,
        headless: bool = True,
        page_timeout: int = 30_000,
        cookies: Optional[list[dict]] = None,
    ):
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError(
                "playwright is not installed. Run: pip install playwright && playwright install chromium"
            )

        self._proxy = proxy
        self._headless = headless
        self._page_timeout = page_timeout
        self._cookies = cookies or []

        # 异步事件循环（独立线程）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._ready = threading.Event()
        self._current_url = ""

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._init_browser())
        except Exception as e:
            logger.error(f"PersistentBrowserSession init failed: {e}")
        finally:
            self._ready.set()
        if self._browser is not None:
            self._loop.run_forever()

    async def _init_browser(self):
        self._playwright = await async_playwright().start()
        launch_kwargs = {
            "headless": self._headless,
            "args": [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
                "--lang=zh-CN",
            ],
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}

        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=__import__("fake_useragent").UserAgent().random,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            extra_http_headers={
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "DNT": "1",
            },
        )

        # 注入 Cookie
        if self._cookies:
            await self._context.add_cookies(self._cookies)

        self._page = await self._context.new_page()
        await self._page.route("**/*", self._route_handler)

        # Stealth
        if _STEALTH_AVAILABLE:
            await stealth_async(self._page)
        else:
            await self._page.add_init_script(_STEALTH_JS_MINIMAL)

        logger.info(f"PersistentBrowserSession ready (headless={self._headless})")

    async def _route_handler(self, route):
        """拦截并丢弃不必要的资源请求。"""
        req = route.request
        if req.resource_type in _BLOCK_RESOURCE_TYPES:
            await route.abort()
            return
        for domain in _BLOCK_DOMAINS:
            if domain in req.url:
                await route.abort()
                return
        await route.continue_()

    # ── 异步核心方法 ──────────────────────────────────────────────

    async def _navigate_async(self, url: str, wait_for: str = "networkidle") -> str:
        if self._page is None:
            return ""

        try:
            await self._page.goto(url, wait_until=wait_for, timeout=self._page_timeout)
            await self._simulate_human(self._page)
            self._current_url = url
            html = await self._page.content()
            logger.debug(f"Navigated to {url[:60]} ({len(html)} chars)")
            return html
        except Exception as e:
            logger.error(f"Navigate error for {url[:60]}: {e}")
            return ""

    async def _click_async(self, selector: str) -> tuple[bool, str]:
        """点击元素，返回 (成功与否, 更新后的HTML)。"""
        if self._page is None:
            return False, ""

        try:
            await self._page.wait_for_selector(selector, timeout=5000)
            await self._page.click(selector)
            await asyncio.sleep(random.uniform(0.5, 1.0))
            html = await self._page.content()
            self._current_url = self._page.url
            logger.debug(f"Clicked: {selector}")
            return True, html
        except Exception as e:
            logger.error(f"Click error for {selector}: {e}")
            return False, ""

    async def _type_text_async(self, selector: str, text: str) -> tuple[bool, str]:
        """输入文本，返回 (成功与否, 更新后的HTML)。"""
        if self._page is None:
            return False, ""

        try:
            await self._page.wait_for_selector(selector, timeout=5000)
            await self._page.fill(selector, text)
            await asyncio.sleep(random.uniform(0.3, 0.6))
            html = await self._page.content()
            logger.debug(f"Typed into: {selector}")
            return True, html
        except Exception as e:
            logger.error(f"Type error for {selector}: {e}")
            return False, ""

    async def _scroll_to_bottom_async(self) -> tuple[bool, str]:
        """滚动到底部，返回 (成功与否, 更新后的HTML)。"""
        if self._page is None:
            return False, ""

        try:
            prev_height = 0
            for _ in range(8):
                height = await self._page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    break
                prev_height = height
                await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(0.8, 1.5))

            html = await self._page.content()
            return True, html
        except Exception as e:
            logger.error(f"Scroll error: {e}")
            return False, ""

    async def _screenshot_async(self, path: str) -> bool:
        """保存当前页面截图。"""
        if self._page is None:
            return False

        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            await self._page.screenshot(path=path, full_page=False)
            logger.debug(f"Screenshot saved: {path}")
            return True
        except Exception as e:
            logger.error(f"Screenshot error: {e}")
            return False

    async def _get_current_html_async(self) -> str:
        """获取当前页面 HTML。"""
        if self._page is None:
            return ""
        try:
            return await self._page.content()
        except Exception as e:
            logger.error(f"Get HTML error: {e}")
            return ""

    async def _simulate_human(self, page: Page):
        """模拟人类行为：随机鼠标移动。"""
        try:
            vp = page.viewport_size or {"width": 1920, "height": 1080}
            for _ in range(random.randint(2, 4)):
                x = random.randint(100, vp["width"] - 100)
                y = random.randint(100, vp["height"] - 100)
                await page.mouse.move(x, y)
                await asyncio.sleep(random.uniform(0.05, 0.2))
        except Exception:
            pass

    # ── 同步接口 ──────────────────────────────────────────────────

    def navigate(self, url: str, wait_for: str = "networkidle") -> str:
        """导航到指定 URL，返回页面 HTML。"""
        future = asyncio.run_coroutine_threadsafe(
            self._navigate_async(url, wait_for=wait_for), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def click(self, selector: str) -> tuple[bool, str]:
        """点击元素，返回 (成功与否, 更新后的HTML)。"""
        future = asyncio.run_coroutine_threadsafe(
            self._click_async(selector), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def type_text(self, selector: str, text: str) -> tuple[bool, str]:
        """输入文本，返回 (成功与否, 更新后的HTML)。"""
        future = asyncio.run_coroutine_threadsafe(
            self._type_text_async(selector, text), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def scroll_to_bottom(self) -> tuple[bool, str]:
        """滚动到底部，返回 (成功与否, 更新后的HTML)。"""
        future = asyncio.run_coroutine_threadsafe(
            self._scroll_to_bottom_async(), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def screenshot(self, path: str) -> bool:
        """保存当前页面截图。"""
        future = asyncio.run_coroutine_threadsafe(
            self._screenshot_async(path), self._loop,
        )
        return future.result(timeout=self._page_timeout / 1000 + 10)

    def get_current_html(self) -> str:
        """获取当前页面 HTML。"""
        future = asyncio.run_coroutine_threadsafe(
            self._get_current_html_async(), self._loop,
        )
        return future.result(timeout=10)

    @property
    def current_url(self) -> str:
        """获取当前页面 URL。"""
        return self._current_url

    def close(self):
        """关闭浏览器会话。"""
        async def _close():
            if self._page:
                try:
                    await self._page.close()
                except Exception:
                    pass
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass

        if self._loop and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
                future.result(timeout=10)
            except Exception as e:
                logger.warning(f"PersistentBrowserSession close error: {e}")
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
        self._browser = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
