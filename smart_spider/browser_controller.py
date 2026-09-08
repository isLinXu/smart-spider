# coding=utf-8
"""BrowserController - 浏览器操作控制器。

复用 DynamicRenderer 的渲染能力，但提供更适合 agent 的接口：
- 独立于 SmartSpider，可单独使用
- 提供 navigate() / click() / type_text() / scroll() 等语义化操作
- 使用 PersistentBrowserSession 维持跨步骤的浏览器会话
- 所有交互操作后自动返回更新后的 HTML

设计原则
--------
1. 单一职责：BrowserController 只负责浏览器操作
2. 依赖注入：通过构造函数传入 DynamicRenderer 或 PersistentBrowserSession
3. 可测试性：支持 Mock 进行单元测试
4. 状态追踪：维护 current_html / current_url，交互后自动更新

用法
----
>>> from smart_spider.browser_controller import BrowserController
>>>
>>> # 方式 1：自动创建持久化会话（推荐，Agent 专用）
>>> controller = BrowserController(headless=True)
>>> html = controller.navigate("https://example.com")
>>> success, html = controller.click("#btn")
>>> controller.close()
>>>
>>> # 方式 2：注入已有的 DynamicRenderer（批量渲染场景）
>>> from smart_spider.dynamic_renderer import DynamicRenderer
>>> renderer = DynamicRenderer(headless=True)
>>> controller = BrowserController(dynamic_renderer=renderer)
>>> html = controller.navigate("https://example.com")
"""
import os
import tempfile
import time
from typing import Optional

from .url_policy import URLPolicy


class BrowserController:
    """浏览器操作控制器。

    封装 PersistentBrowserSession（优先）或 DynamicRenderer，
    提供语义化的浏览器操作，所有交互方法返回更新后的 HTML。
    """

    def __init__(
        self,
        dynamic_renderer: Optional[object] = None,
        *,
        headless: bool = True,
        proxy: Optional[str] = None,
        cookies: Optional[list[dict]] = None,
        storage_state: Optional[object] = None,
        page_timeout: int = 30_000,
        url_policy: Optional[URLPolicy] = None,
        allow_private_hosts: bool = False,
    ):
        """初始化控制器。

        Args:
            dynamic_renderer: 可选的 DynamicRenderer 实例（批量渲染场景）
            headless: 是否使用无头浏览器（仅自动创建会话时生效）
            proxy: 代理地址
            cookies: Cookie 列表
            storage_state: Playwright storage_state（dict 或文件路径）
            page_timeout: 页面超时毫秒数
        """
        self._renderer = dynamic_renderer
        self._session = None
        self.url_policy = url_policy or getattr(dynamic_renderer, "url_policy", None) or URLPolicy(
            allow_private_hosts=allow_private_hosts
        )
        self.current_html = ""
        self.current_url = ""

        if dynamic_renderer is not None:
            # 使用已有的 DynamicRenderer（每次交互新建 context）
            self._mode = "renderer"
        else:
            # 自动创建持久化会话（Agent 推荐）
            from .browser import PersistentBrowserSession
            self._session = PersistentBrowserSession(
                proxy=proxy,
                headless=headless,
                page_timeout=page_timeout,
                cookies=cookies,
                storage_state=storage_state,
                url_policy=url_policy,
                allow_private_hosts=allow_private_hosts,
            )
            self._mode = "session"

    # ── 导航 ──────────────────────────────────────────────────────

    def navigate(self, url: str) -> str:
        """导航到指定 URL，返回页面 HTML。

        Args:
            url: 目标 URL

        Returns:
            页面 HTML 内容
        """
        url = self.url_policy.validate(url)
        if self._mode == "session":
            html = self._session.navigate(url)
        else:
            html = self._renderer.render(url)

        if html:
            self.current_html = html
            self.current_url = url
        return html or ""

    # ── 交互操作（返回更新后的 HTML）──────────────────────────────

    def click(self, selector: str) -> tuple[bool, str]:
        """点击指定选择器的元素。

        Args:
            selector: CSS 选择器

        Returns:
            (是否成功, 更新后的页面 HTML)
        """
        if self._mode == "session":
            success, html = self._session.click(selector)
        else:
            # DynamicRenderer 没有 click 返回 HTML 的能力
            success = self._renderer.click(selector)
            html = self.current_html  # 无法获取更新

        if success and html:
            self.current_html = html
            self.current_url = self._session.current_url if self._session else self.current_url
        return success, html

    def type_text(self, selector: str, text: str) -> tuple[bool, str]:
        """在指定选择器的元素中输入文本。

        Args:
            selector: CSS 选择器
            text: 要输入的文本

        Returns:
            (是否成功, 更新后的页面 HTML)
        """
        if self._mode == "session":
            success, html = self._session.type_text(selector, text)
        else:
            success = self._renderer.type_text(selector, text)
            html = self.current_html

        if success and html:
            self.current_html = html
        return success, html

    def scroll(self) -> tuple[bool, str]:
        """滚动页面到底部。

        Returns:
            (是否成功, 更新后的页面 HTML)
        """
        if self._mode == "session":
            success, html = self._session.scroll_to_bottom()
        else:
            success = self._renderer.scroll_to_bottom()
            html = self.current_html

        if success and html:
            self.current_html = html
        return success, html

    def screenshot(self, path: str = "") -> tuple[bool, str]:
        """保存页面截图。

        Args:
            path: 截图保存路径（留空则自动生成临时路径）

        Returns:
            (是否成功, 截图路径)
        """
        if not path:
            path = os.path.join(tempfile.gettempdir(), f"screenshot_{int(time.time())}.png")

        if self._mode == "session":
            success = self._session.screenshot(path)
        else:
            success = self._renderer.screenshot(self.current_url, path)

        return success, path

    def get_current_html(self) -> str:
        """获取当前页面 HTML（不重新导航）。"""
        if self._mode == "session":
            html = self._session.get_current_html()
            if html:
                self.current_html = html
            return html
        return self.current_html

    def export_storage_state(self, path: str = "") -> dict:
        """导出会话 storage_state（仅 session 模式）。"""
        if self._mode != "session" or self._session is None:
            raise RuntimeError("export_storage_state requires PersistentBrowserSession mode")
        return self._session.export_storage_state(path)

    # ── 生命周期 ──────────────────────────────────────────────────

    def close(self):
        """关闭控制器，释放资源。"""
        if self._session:
            self._session.close()
        elif self._renderer:
            self._renderer.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
