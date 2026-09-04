# coding=utf-8
"""页面感知模块。

从浏览器页面中提取多模态信息：
- 文本内容（HTML → 纯文本）
- 图片内容（CLIP 相似度过滤）
- OCR 文字识别（截图 → 文字）
- 页面结构（DOM 树摘要）

这是 agent 的"眼睛"，为 ReAct 决策核心提供环境感知。
"""
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    Image = None

from .clip_inference import CLIPInference
from ..image_safety import UnsafeImageError, decode_image_bytes
from .ocr import OCRModule
from ..url_policy import URLPolicy


@dataclass
class PageState:
    """页面状态快照。

    包含从页面中提取的所有多模态信息，
    供 ReAct 决策核心使用。
    """
    url: str = ""
    title: str = ""
    html: str = ""
    text: str = ""
    links: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)
    buttons: list[dict] = field(default_factory=list)
    ocr_texts: list[dict] = field(default_factory=list)
    clip_scores: dict[str, float] = field(default_factory=dict)
    screenshot_path: Optional[str] = None
    error: Optional[str] = None

    def to_observation(self) -> str:
        """将页面状态转换为 ReAct 观察描述。"""
        parts = []
        parts.append(f"URL: {self.url}")
        parts.append(f"Title: {self.title}")

        if self.error:
            parts.append(f"Error: {self.error}")
            return "\n".join(parts)

        # 文本摘要（截断到 2000 字符）
        if self.text:
            text_summary = self.text[:2000] + ("..." if len(self.text) > 2000 else "")
            parts.append(f"Text: {text_summary}")

        # 链接摘要（最多 20 个）
        if self.links:
            link_strs = [f"[{l.get('text', '')[:30]}]({l.get('href', '')[:80]})" for l in self.links[:20]]
            parts.append(f"Links({len(self.links)}): {', '.join(link_strs)}")

        # 图片摘要（最多 10 个）
        if self.images:
            img_strs = [f"{img.get('src', '')[:60]}" for img in self.images[:10]]
            parts.append(f"Images({len(self.images)}): {', '.join(img_strs)}")

        # 表单摘要
        if self.forms:
            parts.append(f"Forms({len(self.forms)}): {[f.get('action', '')[:60] for f in self.forms]}")

        # 按钮摘要
        if self.buttons:
            btn_strs = [f"[{b.get('text', '')[:20]}]" for b in self.buttons[:10]]
            parts.append(f"Buttons({len(self.buttons)}): {', '.join(btn_strs)}")

        # OCR 文字
        if self.ocr_texts:
            ocr_strs = [t.get("text", "")[:50] for t in self.ocr_texts[:10]]
            parts.append(f"OCR: {', '.join(ocr_strs)}")

        # CLIP 分数
        if self.clip_scores:
            score_strs = [f"{k}={v:.3f}" for k, v in self.clip_scores.items()]
            parts.append(f"CLIP: {', '.join(score_strs)}")

        return "\n".join(parts)


class PagePerception:
    """页面感知器。

    从浏览器页面中提取多模态信息，生成 PageState 快照。

    用法
    ----
    >>> from smart_spider.perception import PagePerception
    >>> perception = PagePerception(enable_clip=True, enable_ocr=True)
    >>> state = perception.perceive(html, url="https://example.com")
    >>> print(state.to_observation())
    """

    def __init__(
        self,
        enable_clip: bool = True,
        enable_ocr: bool = False,
        clip_model_name: str = "ViT-B/32",
        clip_device: str = "cpu",
        clip_threshold: float = 0.25,
        ocr_lang: str = "ch",
        max_image_bytes: int = 25 * 1024 * 1024,
        max_image_pixels: int = 50_000_000,
    ):
        """初始化感知器。

        Args:
            enable_clip: 是否启用 CLIP 推理
            enable_ocr: 是否启用 OCR
            clip_model_name: CLIP 模型名称
            clip_device: CLIP 推理设备
            clip_threshold: CLIP 相似度阈值
            ocr_lang: OCR 语言
            max_image_bytes: 单张图片最大响应字节数
            max_image_pixels: 单张图片最大解码像素数
        """
        if max_image_bytes <= 0 or max_image_pixels <= 0:
            raise ValueError("image byte and pixel limits must be positive")
        self.max_image_bytes = int(max_image_bytes)
        self.max_image_pixels = int(max_image_pixels)
        self.enable_clip = enable_clip
        self.enable_ocr = enable_ocr
        self.clip_threshold = clip_threshold

        # CLIP 推理器（延迟初始化）
        self._clip: Optional[CLIPInference] = None
        self._clip_model_name = clip_model_name
        self._clip_device = clip_device

        # OCR 推理器（延迟初始化）
        self._ocr: Optional[OCRModule] = None
        self._ocr_lang = ocr_lang

    def _init_clip(self):
        """延迟初始化 CLIP 推理器。"""
        if self._clip is None and self.enable_clip:
            try:
                self._clip = CLIPInference(
                    model_name=self._clip_model_name,
                    device=self._clip_device,
                )
                logger.info(f"CLIP initialized: {self._clip_model_name} on {self._clip_device}")
            except Exception as e:
                logger.warning(f"CLIP init failed: {e}, disabling CLIP")
                self.enable_clip = False

    def _init_ocr(self):
        """延迟初始化 OCR 推理器。"""
        if self._ocr is None and self.enable_ocr:
            try:
                self._ocr = OCRModule(lang=self._ocr_lang)
                logger.info(f"OCR initialized: lang={self._ocr_lang}")
            except Exception as e:
                logger.warning(f"OCR init failed: {e}, disabling OCR")
                self.enable_ocr = False

    def perceive(self, html: str, url: str = "", screenshot_path: Optional[str] = None) -> PageState:
        """从 HTML 中提取多模态信息。

        Args:
            html: 页面 HTML 内容
            url: 页面 URL
            screenshot_path: 可选的截图路径（用于 OCR）

        Returns:
            PageState 页面状态快照
        """
        state = PageState(url=url, html=html, screenshot_path=screenshot_path)

        if not html:
            state.error = "Empty HTML"
            return state

        # 1. 提取文本
        state.text = self._extract_text(html)

        # 2. 提取标题
        state.title = self._extract_title(html)

        # 3. 提取链接
        state.links = self._extract_links(html)

        # 4. 提取图片
        state.images = self._extract_images(html)

        # 5. 提取表单
        state.forms = self._extract_forms(html)

        # 6. 提取按钮
        state.buttons = self._extract_buttons(html)

        # 7. OCR（如果启用且有截图）
        if self.enable_ocr and screenshot_path and os.path.exists(screenshot_path):
            self._init_ocr()
            if self._ocr:
                try:
                    img = Image.open(screenshot_path)
                    state.ocr_texts = self._ocr.recognize_text(img)
                except Exception as e:
                    logger.warning(f"OCR failed: {e}")

        return state

    def compute_clip_scores(self, state: PageState, keywords: list[str], http_client=None) -> PageState:
        """计算 CLIP 相似度分数。

        会下载页面中的图片并用 CLIP 计算与关键词的相似度。

        Args:
            state: 页面状态
            keywords: 关键词列表
            http_client: 可选的 SmartHttpClient 实例，用于下载图片

        Returns:
            更新后的 PageState
        """
        if not self.enable_clip or not state.images:
            return state

        self._init_clip()
        if self._clip is None:
            return state

        for keyword in keywords:
            try:
                text_feat = self._clip.encode_text(keyword)
                max_sim = 0.0
                scored = 0

                for img_info in state.images[:10]:  # 最多计算 10 张图片
                    src = img_info.get("src", "")
                    if not src:
                        continue

                    # 尝试下载图片
                    pil_img = self._download_and_open_image(src, http_client)
                    if pil_img is None:
                        continue

                    try:
                        img_feat = self._clip.encode_image(pil_img)
                        sim = self._clip.compute_similarity(img_feat, text_feat)
                        max_sim = max(max_sim, sim)
                        scored += 1
                    except Exception as e:
                        logger.debug(f"CLIP encode error for {src[:50]}: {e}")

                state.clip_scores[keyword] = max_sim
                if scored > 0:
                    logger.debug(f"CLIP score for '{keyword}': {max_sim:.4f} (scored {scored} images)")

            except Exception as e:
                logger.warning(f"CLIP score error for '{keyword}': {e}")

        return state

    def _download_and_open_image(self, src: str, http_client=None) -> Optional[object]:
        """下载图片并打开为 PIL Image。

        Args:
            src: 图片 URL
            http_client: 可选的 SmartHttpClient 实例

        Returns:
            PIL Image 对象，失败返回 None
        """
        if not _PIL_AVAILABLE:
            return None

        try:
            content = None
            if http_client is not None:
                # 使用 SmartHttpClient 下载（带代理、反爬）
                get_bytes = getattr(http_client, "get_bytes", None)
                if callable(get_bytes):
                    try:
                        content = get_bytes(src, max_bytes=self.max_image_bytes)
                    except TypeError as exc:
                        # Preserve compatibility with small injected clients
                        # that implement the historical one-argument method.
                        if "max_bytes" not in str(exc):
                            raise
                        content = get_bytes(src)
                else:
                    content = http_client.get(src)
                # 兼容自定义 HTTP 客户端返回 Response 的旧实现。
                if hasattr(content, "content"):
                    content = content.content

            if content is None:
                # 降级：使用 urllib 下载
                import urllib.request
                URLPolicy().validate(src)
                req = urllib.request.Request(src, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SmartSpider/2.2)"
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    declared = resp.headers.get("Content-Length")
                    if declared and int(declared) > self.max_image_bytes:
                        return None
                    chunks = []
                    total = 0
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > self.max_image_bytes:
                            return None
                        chunks.append(chunk)
                    content = b"".join(chunks)

            if not content:
                return None

            if not isinstance(content, (bytes, bytearray, memoryview)):
                return None

            try:
                return decode_image_bytes(
                    bytes(content), max_pixels=self.max_image_pixels
                )
            except UnsafeImageError:
                return None
        except Exception as e:
            logger.debug(f"Image download failed for {src[:50]}: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────
    # HTML 解析辅助方法
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_text(html: str) -> str:
        """从 HTML 中提取纯文本。"""
        # 移除 script 和 style 标签
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # 移除 HTML 标签
        text = re.sub(r"<[^>]+>", " ", text)
        # 清理空白
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _extract_title(html: str) -> str:
        """从 HTML 中提取标题。"""
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def _extract_links(html: str) -> list[dict]:
        """从 HTML 中提取链接。"""
        links = []
        for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE):
            href = match.group(1).strip()
            text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                links.append({"href": href, "text": text[:100]})
        return links[:100]  # 最多 100 个链接

    @staticmethod
    def _extract_images(html: str) -> list[dict]:
        """从 HTML 中提取图片。"""
        images = []
        for match in re.finditer(r'<img\s+[^>]*src=["\']([^"\']*)["\'][^>]*>', html, re.IGNORECASE):
            src = match.group(1).strip()
            if src and not src.startswith("data:"):
                # 提取 alt 属性
                alt_match = re.search(r'alt=["\']([^"\']*)["\']', match.group(0), re.IGNORECASE)
                alt = alt_match.group(1).strip() if alt_match else ""
                images.append({"src": src, "alt": alt})
        return images[:50]  # 最多 50 张图片

    @staticmethod
    def _extract_forms(html: str) -> list[dict]:
        """从 HTML 中提取表单。"""
        forms = []
        for match in re.finditer(r'<form\s+[^>]*>(.*?)</form>', html, re.DOTALL | re.IGNORECASE):
            form_html = match.group(0)
            action_match = re.search(r'action=["\']([^"\']*)["\']', form_html, re.IGNORECASE)
            method_match = re.search(r'method=["\']([^"\']*)["\']', form_html, re.IGNORECASE)
            action = action_match.group(1).strip() if action_match else ""
            method = method_match.group(1).strip().upper() if method_match else "GET"
            forms.append({"action": action, "method": method})
        return forms

    @staticmethod
    def _extract_buttons(html: str) -> list[dict]:
        """从 HTML 中提取按钮。"""
        buttons = []
        # <button> 标签
        for match in re.finditer(r'<button[^>]*>(.*?)</button>', html, re.DOTALL | re.IGNORECASE):
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text:
                buttons.append({"text": text[:50], "type": "button"})
        # <input type="submit"> 标签
        for match in re.finditer(r'<input[^>]*type=["\']submit["\'][^>]*>', html, re.IGNORECASE):
            value_match = re.search(r'value=["\']([^"\']*)["\']', match.group(0), re.IGNORECASE)
            value = value_match.group(1).strip() if value_match else "Submit"
            buttons.append({"text": value[:50], "type": "submit"})
        return buttons[:20]
