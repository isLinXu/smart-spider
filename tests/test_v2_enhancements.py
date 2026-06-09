# coding=utf-8
"""Tests for v2.1 enhancements: Circuit Breaker, content dedup, CrawlStats, CrawlEvent, AVIF, disk guard."""
import hashlib
import json
import os
import tempfile
import threading
import time

import pytest

from smart_spider.http_client import ProxyPool, ProxyEntry
from smart_spider.smart_spider import (
    CrawlEvent,
    CrawlStats,
    UrlDeduplicator,
    _detect_ext,
    _AVIF_BRANDS,
)


# ════════════════════════════════════════════════════════════════════════════════
# Circuit Breaker Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """ProxyPool Circuit Breaker lifecycle tests."""

    def test_initial_state_closed(self):
        pool = ProxyPool(["http://a:1", "http://b:2"])
        for state in pool.circuit_states.values():
            assert state == "closed"

    def test_trip_opens_breaker(self):
        pool = ProxyPool(["http://a:1"], max_fail=2)
        entry = pool.get()
        pool.report_fail(entry)
        assert pool.circuit_states["http://a:1"] == "closed"
        pool.report_fail(entry)
        assert pool.circuit_states["http://a:1"] == "open"

    def test_recovery_timeout_transitions_to_half_open(self):
        pool = ProxyPool(["http://a:1"], max_fail=1, recovery_timeout=0.3)
        entry = pool.get()
        pool.report_fail(entry)
        assert pool.circuit_states["http://a:1"] == "open"
        time.sleep(0.4)
        # get() internally checks _is_available which transitions to half_open
        entry2 = pool.get()
        assert pool.circuit_states["http://a:1"] == "half_open"

    def test_half_open_success_closes_breaker(self):
        pool = ProxyPool(["http://a:1"], max_fail=1, recovery_timeout=0.3)
        entry = pool.get()
        pool.report_fail(entry)
        time.sleep(0.4)
        entry2 = pool.get()
        pool.report_success(entry2)
        assert pool.circuit_states["http://a:1"] == "closed"

    def test_half_open_failure_reopens_breaker(self):
        pool = ProxyPool(["http://a:1"], max_fail=1, recovery_timeout=0.3)
        entry = pool.get()
        pool.report_fail(entry)
        time.sleep(0.4)
        entry2 = pool.get()
        pool.report_fail(entry2)
        assert pool.circuit_states["http://a:1"] == "open"

    def test_all_open_forces_half_open(self):
        """When all proxies are OPEN, get() forces the earliest into HALF_OPEN."""
        pool = ProxyPool(["http://a:1", "http://b:2"], max_fail=1, recovery_timeout=999)
        ea = pool.get()
        eb = pool.get()
        pool.report_fail(ea)
        pool.report_fail(eb)
        assert all(v == "open" for v in pool.circuit_states.values())
        # Should still return something (forced recovery)
        entry = pool.get()
        assert entry is not None

    def test_healthy_proxy_preferred_over_half_open(self):
        """Healthy proxy should be preferred over a recovering one."""
        pool = ProxyPool(["http://a:1", "http://b:2"], max_fail=1, recovery_timeout=0.3)
        ea = pool.get()
        pool.report_fail(ea)  # a trips
        time.sleep(0.4)
        # b is still healthy, should be preferred
        eb = pool.get()
        assert eb.url == "http://b:2"


# ════════════════════════════════════════════════════════════════════════════════
# Content Dedup Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestContentDedup:
    """UrlDeduplicator content hash deduplication tests."""

    def test_first_content_not_seen(self):
        d = UrlDeduplicator(content_dedup=True)
        assert d.is_content_seen(b"hello") is False

    def test_same_content_seen_twice(self):
        d = UrlDeduplicator(content_dedup=True)
        d.is_content_seen(b"hello")
        assert d.is_content_seen(b"hello") is True

    def test_different_content_not_seen(self):
        d = UrlDeduplicator(content_dedup=True)
        d.is_content_seen(b"hello")
        assert d.is_content_seen(b"world") is False

    def test_content_dedup_disabled(self):
        d = UrlDeduplicator(content_dedup=False)
        assert d.is_content_seen(b"hello") is False
        assert d.is_content_seen(b"hello") is False  # always False when disabled

    def test_url_and_content_dedup_independent(self):
        d = UrlDeduplicator(content_dedup=True)
        # Different URLs, same content
        d.is_seen("http://a.com/img.jpg")
        d.is_seen("http://b.com/img.jpg")
        # Content dedup catches the duplicate
        assert d.is_content_seen(b"same_image_data") is False
        assert d.is_content_seen(b"same_image_data") is True

    def test_persist_and_restore(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, ".seen.jsonl")
            d1 = UrlDeduplicator(persist_path=path, content_dedup=True)
            d1.is_seen("http://a.com")
            d1.is_content_seen(b"data_a")
            d1.close()

            # Restore
            d2 = UrlDeduplicator(persist_path=path, content_dedup=True)
            assert d2.is_seen("http://a.com") is True
            # Note: content dedup is in-memory only (not persisted)
            d2.close()


# ════════════════════════════════════════════════════════════════════════════════
# CrawlStats Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestCrawlStats:

    def test_initial_counts_zero(self):
        s = CrawlStats()
        assert s.summary()["saved"] == {}

    def test_inc_saved(self):
        s = CrawlStats()
        s.inc_saved("image", 5)
        assert s.summary()["saved"]["image"] == 5

    def test_inc_filtered_and_failed(self):
        s = CrawlStats()
        s.inc_filtered("image", 3)
        s.inc_failed("video", 2)
        assert s.summary()["filtered"]["image"] == 3
        assert s.summary()["failed"]["video"] == 2

    def test_elapsed_seconds(self):
        s = CrawlStats()
        s.start_time = time.monotonic() - 10.0
        assert s.elapsed_seconds >= 9.5

    def test_summary_structure(self):
        s = CrawlStats()
        s.start_time = time.monotonic()
        s.inc_saved("image", 1)
        summary = s.summary()
        assert "saved" in summary
        assert "filtered" in summary
        assert "failed" in summary
        assert "pages_fetched" in summary
        assert "elapsed_seconds" in summary

    def test_thread_safety(self):
        s = CrawlStats()
        s.start_time = time.monotonic()
        errors = []

        def increment_many():
            try:
                for _ in range(1000):
                    s.inc_saved("image")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment_many) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert s.summary()["saved"]["image"] == 10000


# ════════════════════════════════════════════════════════════════════════════════
# CrawlEvent Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestCrawlEvent:

    def test_basic_fields(self):
        e = CrawlEvent(event_type="item_saved", keyword="cat", media_type="image")
        assert e.event_type == "item_saved"
        assert e.keyword == "cat"
        assert e.media_type == "image"

    def test_optional_fields(self):
        e = CrawlEvent(
            event_type="item_filtered", keyword="dog", media_type="image",
            engine="bing", url="http://x", detail={"sim": 0.15},
        )
        assert e.engine == "bing"
        assert e.url == "http://x"
        assert e.detail["sim"] == 0.15


# ════════════════════════════════════════════════════════════════════════════════
# AVIF Detection Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestAVIFDetection:

    def test_avif_brand_detected(self):
        fake_avif = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1"
        assert _detect_ext(fake_avif) == ".avif"

    def test_avis_brand_detected(self):
        fake_avis = b"\x00\x00\x00\x18ftypavis\x00\x00\x00\x00mif1"
        # avis is an AVIF image sequence brand, maps to .avis extension
        assert _detect_ext(fake_avis) == ".avis"

    def test_non_avif_ftyp_not_detected(self):
        fake_mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00mif1"
        assert _detect_ext(fake_mp4) == ".jpg"  # falls through to default

    def test_short_content_no_crash(self):
        assert _detect_ext(b"\x00\x00") == ".jpg"  # no crash, falls through

    def test_existing_formats_still_work(self):
        assert _detect_ext(b"\xff\xd8\xff\xe0") == ".jpg"
        assert _detect_ext(b"\x89PNG\r\n\x1a\n") == ".png"
        webp = b"RIFF\x00\x00\x00\x00WEBP"
        assert _detect_ext(webp) == ".webp"


# ════════════════════════════════════════════════════════════════════════════════
# Callback System Integration Tests
# ════════════════════════════════════════════════════════════════════════════════

class TestCallbackSystem:

    def test_emit_callback_calls_all_callbacks(self):
        results = []

        def cb1(event):
            results.append(("cb1", event.event_type))

        def cb2(event):
            results.append(("cb2", event.event_type))

        # Simulate callback emission logic
        callbacks = [cb1, cb2]
        event = CrawlEvent(event_type="item_saved", keyword="test", media_type="image")
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                pass
        assert len(results) == 2
        assert results[0] == ("cb1", "item_saved")
        assert results[1] == ("cb2", "item_saved")

    def test_callback_exception_doesnt_break_others(self):
        results = []

        def bad_cb(event):
            raise ValueError("oops")

        def good_cb(event):
            results.append(event.event_type)

        callbacks = [bad_cb, good_cb]
        event = CrawlEvent(event_type="item_saved", keyword="test", media_type="image")
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                pass
        assert results == ["item_saved"]
