# coding=utf-8
"""URL + 内容去重器（从 smart_spider 拆出）。"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Optional

from loguru import logger


class UrlDeduplicator:
    """线程安全的 URL + 内容双重去重器。

    双重去重策略
    ------------
    1. URL 去重：MD5(url)，防止同一 URL 重复采集
    2. 内容去重：MD5(content)，防止不同 URL 但相同内容的资源重复保存
       （常见于 CDN 镜像、缩略图/原图指向同一文件）

    存储后端
    --------
    优先使用 pybloom-live 的 BloomFilter（内存效率高，适合长期运行）；
    若未安装则降级到 in-memory set（精确但内存无界增长）。

    持久化（断点续采）
    ------------------
    传入 persist_path 时，每次新增 URL hash 都追加写入 .jsonl 文件。
    初始化时若文件已存在则自动加载，实现跨进程的去重状态恢复。

    >>> dedup = UrlDeduplicator(persist_path="./output/.seen_urls.jsonl")
    """

    def __init__(
        self,
        capacity: int = 1_000_000,
        error_rate: float = 0.001,
        persist_path: Optional[str] = None,
        content_dedup: bool = True,
    ):
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._persist_file = None  # 写追加文件句柄
        self._content_dedup = content_dedup
        self._claimed_urls: set[str] = set()
        self._retryable_urls: set[str] = set()
        self._claimed_contents: set[str] = set()
        self._retryable_contents: set[str] = set()

        try:
            from pybloom_live import ScalableBloomFilter
            self._bloom = ScalableBloomFilter(
                initial_capacity=capacity,
                error_rate=error_rate,
            )
            self._set = None
            # 内容去重也使用 BloomFilter
            self._content_bloom = ScalableBloomFilter(
                initial_capacity=capacity,
                error_rate=error_rate,
            ) if content_dedup else None
            self._content_set: Optional[set] = None
            logger.info("UrlDeduplicator: using BloomFilter (pybloom-live)")
        except ImportError:
            self._bloom = None
            self._set: set = set()
            self._content_bloom = None
            self._content_set: Optional[set] = set() if content_dedup else None
            logger.debug("UrlDeduplicator: using in-memory set "
                         "(install pybloom-live for memory-efficient dedup)")

        # 加载持久化状态
        if persist_path:
            self._load(persist_path)
            self._persist_file = open(persist_path, "a", encoding="utf-8", buffering=1)  # line-buffered

    def _load(self, path: str):
        """从持久化文件恢复已见 URL hash。"""
        if not os.path.exists(path):
            return
        loaded = 0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    h = line.strip()
                    if not h:
                        continue
                    if self._bloom is not None:
                        self._bloom.add(h)
                    else:
                        self._set.add(h)
                    loaded += 1
            logger.info(f"UrlDeduplicator: loaded {loaded} seen URLs from {path}")
        except Exception as e:
            logger.warning(f"UrlDeduplicator: failed to load {path}: {e}")

    def is_seen(self, url: str) -> bool:
        """检查 URL 是否已见过，并将其标记为已见。线程安全。"""
        h = hashlib.md5(url.encode()).hexdigest()
        with self._lock:
            if self._bloom is not None:
                if h in self._bloom:
                    return True
                self._bloom.add(h)
            else:
                if h in self._set:
                    return True
                self._set.add(h)
            # 持久化新增 hash
            if self._persist_file is not None:
                try:
                    self._persist_file.write(h + "\n")
                except Exception:
                    pass
            return False

    def is_content_seen(self, content: bytes) -> bool:
        """检查内容是否已见过（MD5 去重），并将其标记为已见。线程安全。

        用于防止不同 URL 指向相同内容的资源重复保存（CDN 镜像等场景）。
        未启用内容去重时始终返回 False。
        """
        if not self._content_dedup:
            return False
        h = hashlib.md5(content).hexdigest()
        with self._lock:
            if self._content_bloom is not None:
                if h in self._content_bloom:
                    return True
                self._content_bloom.add(h)
            elif self._content_set is not None:
                if h in self._content_set:
                    return True
                self._content_set.add(h)
            return False

    def claim(self, url: str) -> bool:
        """领取 URL，但只有 ``commit`` 后才会进入永久去重集合。"""
        h = hashlib.md5(url.encode()).hexdigest()
        with self._lock:
            if h in self._claimed_urls:
                return False
            if h in self._retryable_urls:
                self._retryable_urls.remove(h)
                self._claimed_urls.add(h)
                return True
            if self._bloom is not None:
                if h in self._bloom:
                    return False
            elif h in self._set:
                return False
            self._claimed_urls.add(h)
            return True

    def commit(self, url: str):
        """成功处理 URL 后提交永久去重状态。"""
        h = hashlib.md5(url.encode()).hexdigest()
        with self._lock:
            self._claimed_urls.discard(h)
            if self._bloom is not None:
                self._bloom.add(h)
            else:
                self._set.add(h)
            if self._persist_file is not None:
                try:
                    self._persist_file.write(h + "\n")
                except Exception:
                    pass

    def release(self, url: str):
        """临时失败时释放 URL，允许后续重试。"""
        h = hashlib.md5(url.encode()).hexdigest()
        with self._lock:
            if h in self._claimed_urls:
                self._claimed_urls.remove(h)
                self._retryable_urls.add(h)

    def claim_content(self, content: bytes) -> bool:
        """领取内容哈希，避免并发重复保存且允许失败后重试。"""
        if not self._content_dedup:
            return True
        h = hashlib.md5(content).hexdigest()
        with self._lock:
            if h in self._claimed_contents:
                return False
            if h in self._retryable_contents:
                self._retryable_contents.remove(h)
                self._claimed_contents.add(h)
                return True
            if self._content_bloom is not None:
                if h in self._content_bloom:
                    return False
            elif self._content_set is not None and h in self._content_set:
                return False
            self._claimed_contents.add(h)
            return True

    def commit_content(self, content: bytes):
        """成功写盘后提交内容哈希。"""
        if not self._content_dedup:
            return
        h = hashlib.md5(content).hexdigest()
        with self._lock:
            self._claimed_contents.discard(h)
            if self._content_bloom is not None:
                self._content_bloom.add(h)
            elif self._content_set is not None:
                self._content_set.add(h)

    def release_content(self, content: bytes):
        """写盘失败时释放内容哈希。"""
        if not self._content_dedup:
            return
        h = hashlib.md5(content).hexdigest()
        with self._lock:
            if h in self._claimed_contents:
                self._claimed_contents.remove(h)
                self._retryable_contents.add(h)

    def close(self):
        """关闭持久化文件句柄。"""
        with self._lock:
            if self._persist_file is not None:
                try:
                    self._persist_file.close()
                except Exception:
                    pass
                self._persist_file = None

    def __len__(self) -> int:
        with self._lock:
            if self._set is not None:
                return len(self._set)
            return -1  # BloomFilter 无法精确计数

    def __del__(self):
        self.close()


