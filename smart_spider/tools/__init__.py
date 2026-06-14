# coding=utf-8
"""工具函数集合。

将现有采集能力封装为工具函数，供 ReAct 决策核心调用。

工具分类
--------
1. 浏览器操作工具：navigate, click, type_text, scroll, screenshot
2. 采集工具：download_image, extract_text, extract_links
3. 过滤工具：clip_filter, content_dedup
4. 输出工具：save_result, save_metadata

用法
----
>>> from smart_spider.tools import ToolRegistry
>>> registry = ToolRegistry()
>>> registry.register("navigate", navigate, "导航到指定 URL")
>>> result = registry.get("navigate")(url="https://example.com", browser_controller=controller)
"""
import hashlib
import json
import os
import re
from typing import Any, Callable, Optional

from loguru import logger


# ──────────────────────────────────────────────────────────────────────────────
# 浏览器操作工具
# ──────────────────────────────────────────────────────────────────────────────

def navigate(url: str, browser_controller=None, **kwargs) -> str:
    """导航到指定 URL。

    Args:
        url: 目标 URL
        browser_controller: BrowserController 实例

    Returns:
        页面 HTML 内容
    """
    if browser_controller is None:
        return "Error: browser_controller is None"
    html = browser_controller.navigate(url)
    return html or ""


def click(selector: str, browser_controller=None, **kwargs) -> str:
    """点击指定选择器的元素。

    Args:
        selector: CSS 选择器
        browser_controller: BrowserController 实例

    Returns:
        操作结果描述
    """
    if browser_controller is None:
        return "Error: browser_controller is None"
    result = browser_controller.click(selector)
    # BrowserController.click() 返回 (bool, str)
    if isinstance(result, tuple):
        success, html = result
        return f"Click {'success' if success else 'failed'}: {selector}"
    return f"Click {'success' if result else 'failed'}: {selector}"


def type_text(selector: str, text: str, browser_controller=None, **kwargs) -> str:
    """在指定选择器的元素中输入文本。

    Args:
        selector: CSS 选择器
        text: 要输入的文本
        browser_controller: BrowserController 实例

    Returns:
        操作结果描述
    """
    if browser_controller is None:
        return "Error: browser_controller is None"
    result = browser_controller.type_text(selector, text)
    # BrowserController.type_text() 返回 (bool, str)
    if isinstance(result, tuple):
        success, html = result
        return f"Type {'success' if success else 'failed'}: {selector}"
    return f"Type {'success' if result else 'failed'}: {selector}"


def scroll(browser_controller=None, **kwargs) -> str:
    """滚动页面到底部。

    Args:
        browser_controller: BrowserController 实例

    Returns:
        操作结果描述
    """
    if browser_controller is None:
        return "Error: browser_controller is None"
    result = browser_controller.scroll()
    # BrowserController.scroll() 返回 (bool, str)
    if isinstance(result, tuple):
        success, html = result
        return f"Scroll {'success' if success else 'failed'}"
    return f"Scroll {'success' if result else 'failed'}"


def screenshot(path: str = "", browser_controller=None, **kwargs) -> str:
    """保存页面截图。

    Args:
        path: 截图保存路径（可选）
        browser_controller: BrowserController 实例

    Returns:
        操作结果描述
    """
    if browser_controller is None:
        return "Error: browser_controller is None"
    # BrowserController.screenshot() 返回 (bool, str)
    result = browser_controller.screenshot(path)
    if isinstance(result, tuple):
        success, saved_path = result
        return f"Screenshot {'saved' if success else 'failed'}: {saved_path}"
    return f"Screenshot {'saved' if result else 'failed'}"


# ──────────────────────────────────────────────────────────────────────────────
# 采集工具
# ──────────────────────────────────────────────────────────────────────────────

def download_image(url: str, save_dir: str, http_client=None, **kwargs) -> str:
    """下载图片。

    Args:
        url: 图片 URL
        save_dir: 保存目录
        http_client: SmartHttpClient 实例

    Returns:
        保存路径
    """
    if http_client is None:
        return "Error: http_client is None"

    try:
        # SmartHttpClient.get_stream() 返回 (peek_bytes, response)
        # 或者使用 get() 获取文本（不适合二进制）
        # 正确做法：使用 get_stream 读取二进制内容
        peek, resp = http_client.get_stream(url, peek_bytes=8192)
        try:
            rest = b"".join(resp.iter_content(chunk_size=65536))
            content = peek + rest
        finally:
            resp.close()

        if not content:
            return f"Download failed: {url}"

        name = hashlib.md5(url.encode()).hexdigest()
        ext = _detect_ext(content)
        fp = os.path.join(save_dir, f"{name}{ext}")
        os.makedirs(save_dir, exist_ok=True)
        with open(fp, "wb") as f:
            f.write(content)
        return fp
    except Exception as e:
        return f"Error: {e}"


def extract_text(html: str, **kwargs) -> str:
    """从 HTML 中提取纯文本。

    Args:
        html: HTML 内容

    Returns:
        纯文本
    """
    # 移除 script 和 style 标签
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", " ", text)
    # 清理空白
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_links(html: str, **kwargs) -> list[dict]:
    """从 HTML 中提取链接。

    Args:
        html: HTML 内容

    Returns:
        链接列表 [{"href": ..., "text": ...}]
    """
    links = []
    for match in re.finditer(r'<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', html, re.DOTALL | re.IGNORECASE):
        href = match.group(1).strip()
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            links.append({"href": href, "text": text[:100]})
    return links


def extract_images(html: str, **kwargs) -> list[dict]:
    """从 HTML 中提取图片。

    Args:
        html: HTML 内容

    Returns:
        图片列表 [{"src": ..., "alt": ...}]
    """
    images = []
    for match in re.finditer(r'<img\s+[^>]*src=["\']([^"\']*)["\'][^>]*>', html, re.IGNORECASE):
        src = match.group(1).strip()
        if src and not src.startswith("data:"):
            alt_match = re.search(r'alt=["\']([^"\']*)["\']', match.group(0), re.IGNORECASE)
            alt = alt_match.group(1).strip() if alt_match else ""
            images.append({"src": src, "alt": alt})
    return images


# ──────────────────────────────────────────────────────────────────────────────
# 过滤工具
# ──────────────────────────────────────────────────────────────────────────────

def clip_filter(image_path: str, keyword: str, threshold: float = 0.25, clip_inference=None, **kwargs) -> dict:
    """使用 CLIP 过滤图片。

    Args:
        image_path: 图片路径
        keyword: 关键词
        threshold: 相似度阈值
        clip_inference: CLIPInference 实例

    Returns:
        {"passed": bool, "similarity": float}
    """
    if clip_inference is None:
        return {"passed": False, "similarity": 0.0, "error": "clip_inference is None"}

    try:
        from PIL import Image
        img = Image.open(image_path)
        img_feat = clip_inference.encode_image(img)
        text_feat = clip_inference.encode_text(keyword)
        sim = clip_inference.compute_similarity(img_feat, text_feat)
        return {"passed": sim > threshold, "similarity": sim}
    except Exception as e:
        return {"passed": False, "similarity": 0.0, "error": str(e)}


def content_dedup(content: bytes, deduplicator=None, **kwargs) -> dict:
    """内容去重。

    Args:
        content: 文件内容（bytes）
        deduplicator: UrlDeduplicator 实例

    Returns:
        {"is_duplicate": bool}
    """
    if deduplicator is None:
        return {"is_duplicate": False, "error": "deduplicator is None"}

    try:
        is_dup = deduplicator.is_content_seen(content)
        return {"is_duplicate": is_dup}
    except Exception as e:
        return {"is_duplicate": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# 输出工具
# ──────────────────────────────────────────────────────────────────────────────

def save_result(data: Any, path: str, **kwargs) -> str:
    """保存结果到文件。

    Args:
        data: 要保存的数据
        path: 保存路径

    Returns:
        保存路径
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if isinstance(data, (dict, list)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        elif isinstance(data, bytes):
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(data))
        return path
    except Exception as e:
        return f"Error: {e}"


def save_metadata(url: str, metadata: dict, path: str, **kwargs) -> str:
    """保存元数据到 JSON 文件。

    Args:
        url: 资源 URL
        metadata: 元数据字典
        path: 保存路径

    Returns:
        保存路径
    """
    try:
        data = {"url": url, **metadata}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
    except Exception as e:
        return f"Error: {e}"


# ──────────────────────────────────────────────────────────────────────────────
# 工具注册表
# ──────────────────────────────────────────────────────────────────────────────

_TOOL_REGISTRY: dict[str, dict] = {}


def register_tool(name: str, func: Callable, description: str = ""):
    """注册工具函数到全局注册表。"""
    _TOOL_REGISTRY[name] = {
        "func": func,
        "description": description,
    }


def get_tool(name: str) -> Optional[Callable]:
    """获取已注册的工具函数。"""
    entry = _TOOL_REGISTRY.get(name)
    return entry["func"] if entry else None


def list_tools() -> list[dict]:
    """列出所有已注册的工具。"""
    return [
        {"name": name, "description": entry["description"]}
        for name, entry in _TOOL_REGISTRY.items()
    ]


def format_tools_prompt() -> str:
    """格式化工具列表为 LLM 提示。"""
    lines = []
    for name, entry in _TOOL_REGISTRY.items():
        lines.append(f"- {name}: {entry['description']}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────────────────────

def _detect_ext(content: bytes) -> str:
    """检测文件扩展名。"""
    if content[:8] == b'\x89PNG\r\n\x1a\n':
        return ".png"
    elif content[:2] == b'\xff\xd8':
        return ".jpg"
    elif content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return ".webp"
    elif content[:4] == b'GIF8':
        return ".gif"
    else:
        return ".bin"


# ──────────────────────────────────────────────────────────────────────────────
# 自动注册所有工具
# ──────────────────────────────────────────────────────────────────────────────

def _auto_register():
    """自动注册所有工具到全局注册表。"""
    register_tool("navigate", navigate, "导航到指定 URL。参数: url (str)")
    register_tool("click", click, "点击页面元素。参数: selector (str)")
    register_tool("type_text", type_text, "在输入框中输入文本。参数: selector (str), text (str)")
    register_tool("scroll", scroll, "滚动页面到底部。无参数")
    register_tool("screenshot", screenshot, "保存页面截图。参数: url (str), path (str)")
    register_tool("download_image", download_image, "下载图片。参数: url (str), save_dir (str)")
    register_tool("extract_text", extract_text, "从 HTML 中提取纯文本。参数: html (str)")
    register_tool("extract_links", extract_links, "从 HTML 中提取链接。参数: html (str)")
    register_tool("extract_images", extract_images, "从 HTML 中提取图片。参数: html (str)")
    register_tool("clip_filter", clip_filter, "使用 CLIP 过滤图片。参数: image_path (str), keyword (str)")
    register_tool("content_dedup", content_dedup, "内容去重。参数: content (bytes)")
    register_tool("save_result", save_result, "保存结果到文件。参数: data (Any), path (str)")
    register_tool("save_metadata", save_metadata, "保存元数据。参数: url (str), metadata (dict), path (str)")


_auto_register()
