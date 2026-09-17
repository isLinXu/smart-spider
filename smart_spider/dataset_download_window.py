# coding=utf-8
"""有界下载窗口：候选队列、全局限流、按域名并发。"""
from __future__ import annotations

import threading
from typing import Callable, Optional
from urllib.parse import urlsplit


class DownloadWindow:
    """Acquire/release slots for page candidates and image downloads."""

    def __init__(
        self,
        *,
        max_inflight_downloads: int,
        max_pending_candidates: int,
        per_domain_concurrency: int,
        should_stop: Optional[Callable[[], bool]] = None,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        if max_inflight_downloads <= 0:
            raise ValueError("max_inflight_downloads must be positive")
        if max_pending_candidates <= 0:
            raise ValueError("max_pending_candidates must be positive")
        if per_domain_concurrency <= 0:
            raise ValueError("per_domain_concurrency must be positive")
        self.max_inflight_downloads = int(max_inflight_downloads)
        self.max_pending_candidates = int(max_pending_candidates)
        self.per_domain_concurrency = int(per_domain_concurrency)
        self._should_stop = should_stop or (lambda: False)
        self._on_change = on_change
        self._download_slots = threading.BoundedSemaphore(self.max_inflight_downloads)
        self._candidate_slots = threading.BoundedSemaphore(self.max_pending_candidates)
        self._domain_slots: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()
        self._inflight_downloads = 0
        self._inflight_candidates = 0

    @property
    def inflight_downloads(self) -> int:
        with self._lock:
            return self._inflight_downloads

    @property
    def inflight_candidates(self) -> int:
        with self._lock:
            return self._inflight_candidates

    def acquire_candidate_slot(self) -> bool:
        while not self._candidate_slots.acquire(timeout=0.2):
            if self._should_stop():
                return False
        with self._lock:
            self._inflight_candidates += 1
        self._notify()
        return True

    def release_candidate_slot(self) -> None:
        with self._lock:
            self._inflight_candidates = max(0, self._inflight_candidates - 1)
        self._candidate_slots.release()
        self._notify()

    def domain_slot(self, url: str) -> threading.BoundedSemaphore:
        hostname = (urlsplit(url).hostname or "_unknown").casefold()
        with self._lock:
            slot = self._domain_slots.get(hostname)
            if slot is None:
                slot = threading.BoundedSemaphore(self.per_domain_concurrency)
                self._domain_slots[hostname] = slot
            return slot

    def acquire_download_slot(self, url: str) -> Optional[threading.BoundedSemaphore]:
        while not self._download_slots.acquire(timeout=0.2):
            if self._should_stop():
                return None
        domain_slot = self.domain_slot(url)
        while not domain_slot.acquire(timeout=0.2):
            if self._should_stop():
                self._download_slots.release()
                return None
        with self._lock:
            self._inflight_downloads += 1
        self._notify()
        return domain_slot

    def release_download_slot(self, domain_slot: threading.BoundedSemaphore) -> None:
        with self._lock:
            self._inflight_downloads = max(0, self._inflight_downloads - 1)
        domain_slot.release()
        self._download_slots.release()
        self._notify()

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change()
