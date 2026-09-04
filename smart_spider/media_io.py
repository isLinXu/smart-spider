# coding=utf-8
"""视频下载与正文提取（从 smart_spider 拆出）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Optional

from loguru import logger

from .http_client import ProxyPool, SmartHttpClient


class VideoDownloader:
    """用 yt-dlp 下载视频页面 URL 对应的视频流。

    支持 ProxyPool 轮换代理（优先）或固定代理（fallback）。
    下载失败时自动换代理重试（最多 max_retries 次）。
    """

    def __init__(
        self,
        output_dir: str,
        proxy_pool: Optional[ProxyPool] = None,
        proxy: Optional[str] = None,
        max_filesize: str = "500m",
        format_spec: str = "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        cookies_file: Optional[str] = None,
        max_retries: int = 2,
        timeout: int = 300,
    ):
        self.output_dir = output_dir
        self._proxy_pool = proxy_pool
        self._static_proxy = proxy
        self.max_filesize = max_filesize
        self.format_spec = format_spec
        self.cookies_file = cookies_file
        self.max_retries = max_retries
        self.timeout = timeout

    def _pick_proxy(self) -> Optional[str]:
        """从 ProxyPool 取一个代理 URL，无代理时返回 None。"""
        if self._proxy_pool and not self._proxy_pool.is_empty():
            entry = self._proxy_pool.get()
            return entry.url if entry else None
        return self._static_proxy

    def _ytdlp_cmd(self, url: str, out_path: str, proxy: Optional[str]) -> list[str]:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--format", self.format_spec,
            "--max-filesize", self.max_filesize,
            "--output", os.path.join(out_path, "%(title)s.%(ext)s"),
            "--no-warnings",
            "--quiet",
            "--write-info-json",
            "--write-thumbnail",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        ]
        if proxy:
            cmd += ["--proxy", proxy]
        if self.cookies_file and os.path.exists(self.cookies_file):
            cmd += ["--cookies", self.cookies_file]
        cmd.append(url)
        return cmd

    def download(self, url: str, out_path: str) -> bool:
        """下载视频，失败时换代理重试（最多 max_retries 次）。

        Bug fix: old version picked a proxy once and returned False on failure
        without retrying.  With a proxy pool this meant a bad proxy would cause
        every download to fail silently.  Now each retry picks a fresh proxy.
        """
        os.makedirs(out_path, exist_ok=True)
        for attempt in range(self.max_retries + 1):
            proxy = self._pick_proxy()
            cmd = self._ytdlp_cmd(url, out_path, proxy)
            logger.info(
                f"yt-dlp (attempt {attempt + 1}/{self.max_retries + 1}): {url[:60]}"
                + (f" [proxy={proxy}]" if proxy else "")
            )
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=self.timeout
                )
                if result.returncode == 0:
                    logger.info(f"视频下载成功: {url[:60]}")
                    return True
                logger.warning(
                    f"yt-dlp 失败 [{result.returncode}] attempt {attempt + 1}: "
                    f"{result.stderr[:200]}"
                )
            except subprocess.TimeoutExpired:
                logger.error(f"VideoDownloader timeout (attempt {attempt + 1}): {url[:60]}")
            except FileNotFoundError:
                logger.error("yt-dlp not found; install with: pip install yt-dlp")
                return False  # no point retrying if binary is missing
            except Exception as e:
                logger.error(f"VideoDownloader unexpected error (attempt {attempt + 1}): {e}")
        return False


class TextExtractor:
    """从网页 URL 提取纯文本正文，依赖 trafilatura（可选）。"""

    def __init__(self, http_client: SmartHttpClient):
        self._client = http_client
        try:
            import trafilatura
            self._trafilatura = trafilatura
        except ImportError:
            self._trafilatura = None
            logger.warning("trafilatura not installed, text extraction will use basic regex. "
                           "Run: pip install trafilatura")

    def extract(self, url: str) -> Optional[dict]:
        """
        提取页面正文。

        Returns:
            {"url": str, "title": str, "text": str, "date": str, "author": str} 或 None
        """
        try:
            html = self._client.get_text(url)
        except Exception as e:
            logger.error(f"TextExtractor fetch error {url[:60]}: {e}")
            return None

        if not html or len(html.strip()) < 50:
            logger.debug(f"TextExtractor: empty/too-short response for {url[:60]}")
            return None

        if self._trafilatura:
            try:
                result = self._trafilatura.extract(
                    html,
                    include_comments=False,
                    include_tables=True,
                    output_format="json",
                    url=url,
                )
                if result:
                    data = json.loads(result)
                    text = data.get("text", "")
                    if text and len(text.strip()) > 20:
                        return {
                            "url": url,
                            "title": data.get("title", ""),
                            "text": text,
                            "date": data.get("date", ""),
                            "author": data.get("author", ""),
                        }
            except Exception as e:
                logger.warning(f"trafilatura error: {e}")

        # 降级：简单正则提取 <title> + <p> 文本
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html, re.DOTALL | re.IGNORECASE)
        text = "\n".join(
            re.sub(r"<[^>]+>", "", p).strip()
            for p in paragraphs
            if len(re.sub(r"<[^>]+>", "", p).strip()) > 50
        )
        # 有 title 或有正文时返回结果；两者都空则返回 None
        if not text and not title:
            return None
        return {"url": url, "title": title, "text": text, "date": "", "author": ""}


