# coding=utf-8
"""Round-3 regression tests.

Covers:
- _ModalDoneEvents: mark_done, is_done, is_all_done, reset, thread-safety
- get_stream: peek is strictly bounded by peek_bytes
- download() per-keyword tracker reset (multi-keyword crawl)
- TextExtractor fallback regex when trafilatura is unavailable
- VideoDownloader timeout propagation (subprocess.TimeoutExpired)
"""
import io
import json
import queue
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from smart_spider.smart_spider import _ModalDoneEvents, _SENTINEL


# ──────────────────────────────────────────────────────────────────────────────
# _ModalDoneEvents
# ──────────────────────────────────────────────────────────────────────────────

class TestModalDoneEvents:

    def test_mark_done_sets_modal_event(self):
        t = _ModalDoneEvents(["image", "video"])
        assert not t.is_done("image")
        t.mark_done("image")
        assert t.is_done("image")

    def test_other_modal_unaffected(self):
        t = _ModalDoneEvents(["image", "video", "text"])
        t.mark_done("image")
        assert not t.is_done("video")
        assert not t.is_done("text")

    def test_all_done_only_after_all_modals(self):
        t = _ModalDoneEvents(["image", "video", "text"])
        assert not t.is_all_done()
        t.mark_done("image")
        assert not t.is_all_done()
        t.mark_done("video")
        assert not t.is_all_done()
        t.mark_done("text")
        assert t.is_all_done()

    def test_all_done_with_single_modal(self):
        t = _ModalDoneEvents(["image"])
        t.mark_done("image")
        assert t.is_all_done()

    def test_reset_clears_all_events(self):
        t = _ModalDoneEvents(["image", "video"])
        t.mark_done("image")
        t.mark_done("video")
        assert t.is_all_done()
        t.reset()
        assert not t.is_done("image")
        assert not t.is_done("video")
        assert not t.is_all_done()

    def test_reset_allows_reuse_for_next_keyword(self):
        t = _ModalDoneEvents(["image"])
        t.mark_done("image")
        assert t.is_all_done()
        t.reset()
        # Simulate next keyword: should be able to mark done again
        t.mark_done("image")
        assert t.is_all_done()

    def test_modal_event_is_threading_event(self):
        t = _ModalDoneEvents(["image"])
        ev = t.modal_event("image")
        assert isinstance(ev, threading.Event)

    def test_all_done_event_is_threading_event(self):
        t = _ModalDoneEvents(["image"])
        assert isinstance(t.all_done_event, threading.Event)

    def test_thread_safety(self):
        """Concurrent mark_done calls must not corrupt state."""
        t = _ModalDoneEvents(["image", "video", "text"])
        errors = []

        def _mark(mt):
            try:
                for _ in range(50):
                    t.mark_done(mt)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_mark, args=(mt,))
                   for mt in ["image", "video", "text"]]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert not errors, f"Thread-safety errors: {errors}"
        assert t.is_all_done()

    def test_mark_done_idempotent(self):
        """Calling mark_done multiple times must not raise or corrupt state."""
        t = _ModalDoneEvents(["image"])
        t.mark_done("image")
        t.mark_done("image")  # second call must be safe
        assert t.is_done("image")
        assert t.is_all_done()

    def test_modal_event_set_by_mark_done(self):
        """modal_event().is_set() must be True after mark_done."""
        t = _ModalDoneEvents(["video"])
        t.mark_done("video")
        assert t.modal_event("video").is_set()

    def test_all_done_event_set_after_all(self):
        """all_done_event.is_set() must be True after all modals done."""
        t = _ModalDoneEvents(["image", "text"])
        t.mark_done("image")
        t.mark_done("text")
        assert t.all_done_event.is_set()


# ──────────────────────────────────────────────────────────────────────────────
# get_stream: peek strictly bounded
# ──────────────────────────────────────────────────────────────────────────────

class TestGetStreamPeekBound:
    """Verify get_stream always returns peek <= peek_bytes."""

    def _make_client(self):
        from smart_spider.http_client import SmartHttpClient
        client = SmartHttpClient.__new__(SmartHttpClient)
        client._rate_limiter = MagicMock()
        client._rate_limiter.acquire = MagicMock()
        client._proxy_pool = MagicMock()
        client._proxy_pool.is_empty.return_value = True
        client._timeout = 10
        client._proxy_strategy = "round_robin"
        client._ua = MagicMock()
        client._ua.random = "Mozilla/5.0"
        return client

    def test_peek_exactly_peek_bytes_when_one_large_chunk(self):
        """If server returns a chunk larger than peek_bytes, peek must be truncated."""
        from smart_spider.http_client import SmartHttpClient

        client = self._make_client()
        large_data = b"X" * 20000

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([large_data])

        with patch.object(SmartHttpClient, "get", return_value=mock_resp):
            peek, resp = client.get_stream("https://example.com", peek_bytes=8192)

        assert len(peek) == 8192, (
            f"peek must be exactly 8192 bytes; got {len(peek)}"
        )
        assert peek == large_data[:8192]

    def test_peek_does_not_exceed_peek_bytes_with_exact_chunk(self):
        """Single chunk exactly equal to peek_bytes must pass through unchanged."""
        from smart_spider.http_client import SmartHttpClient

        client = self._make_client()
        exact_data = b"Y" * 8192

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([exact_data])

        with patch.object(SmartHttpClient, "get", return_value=mock_resp):
            peek, resp = client.get_stream("https://example.com", peek_bytes=8192)

        assert len(peek) == 8192

    def test_overflow_bytes_available_via_iter_content(self):
        """Bytes beyond peek_bytes must be retrievable from the returned response."""
        from smart_spider.http_client import SmartHttpClient

        client = self._make_client()
        full_data = b"A" * 8192 + b"B" * 4096  # 12 KB total

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([full_data])

        with patch.object(SmartHttpClient, "get", return_value=mock_resp):
            peek, resp = client.get_stream("https://example.com", peek_bytes=8192)

        assert len(peek) == 8192
        rest = b"".join(resp.iter_content(chunk_size=65536))
        assert rest == b"B" * 4096, "Overflow bytes must be yielded by subsequent iter_content"

    def test_small_total_content_peek_equals_content(self):
        """When total content < peek_bytes, peek equals the full content."""
        from smart_spider.http_client import SmartHttpClient

        client = self._make_client()
        small_data = b"Z" * 1024

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([small_data])

        with patch.object(SmartHttpClient, "get", return_value=mock_resp):
            peek, resp = client.get_stream("https://example.com", peek_bytes=8192)

        assert peek == small_data
        assert len(peek) == 1024


# ──────────────────────────────────────────────────────────────────────────────
# TextExtractor fallback regex
# ──────────────────────────────────────────────────────────────────────────────

class TestTextExtractorFallback:
    """When trafilatura is not installed, basic regex extraction must still work."""

    def _make_extractor(self, html: str):
        from smart_spider.smart_spider import TextExtractor
        http = MagicMock()
        http.get_text.return_value = html
        extractor = TextExtractor.__new__(TextExtractor)
        extractor._client = http
        extractor._trafilatura = None  # simulate not installed
        return extractor

    def test_title_extracted(self):
        ext = self._make_extractor(
            "<html><head><title>Hello World</title></head><body></body></html>"
        )
        result = ext.extract("https://example.com")
        assert result is not None
        assert result["title"] == "Hello World"

    def test_paragraph_text_extracted(self):
        ext = self._make_extractor(
            "<html><body>"
            "<p>This is a long enough paragraph that exceeds fifty characters.</p>"
            "<p>Short</p>"
            "</body></html>"
        )
        result = ext.extract("https://example.com")
        assert result is not None
        assert "long enough paragraph" in result["text"]
        assert "Short" not in result["text"]  # < 50 chars, should be filtered

    def test_url_preserved(self):
        ext = self._make_extractor("<html><body></body></html>")
        result = ext.extract("https://example.com/article")
        # v2.1: no title + no text → returns None (empty content not worth saving)
        assert result is None

    def test_empty_fields_on_minimal_html(self):
        ext = self._make_extractor("<html></html>")
        result = ext.extract("https://example.com")
        # v2.1: empty HTML with no title/text → returns None
        assert result is None

    def test_fetch_error_returns_none(self):
        from smart_spider.smart_spider import TextExtractor
        http = MagicMock()
        http.get_text.side_effect = OSError("network error")
        extractor = TextExtractor.__new__(TextExtractor)
        extractor._client = http
        extractor._trafilatura = None
        result = extractor.extract("https://example.com")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# VideoDownloader: timeout and missing yt-dlp
# ──────────────────────────────────────────────────────────────────────────────

class TestVideoDownloaderEdgeCases:

    def _make_downloader(self, tmp_path):
        from smart_spider.smart_spider import VideoDownloader
        return VideoDownloader(output_dir=str(tmp_path))

    def test_timeout_returns_false(self, tmp_path):
        """subprocess.TimeoutExpired must be caught and return False."""
        dl = self._make_downloader(tmp_path)
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=300)):
            result = dl.download("https://example.com/video", str(tmp_path))
        assert result is False

    def test_missing_ytdlp_returns_false(self, tmp_path):
        """FileNotFoundError (yt-dlp not installed) must return False."""
        dl = self._make_downloader(tmp_path)
        with patch("subprocess.run", side_effect=FileNotFoundError("yt-dlp not found")):
            result = dl.download("https://example.com/video", str(tmp_path))
        assert result is False

    def test_nonzero_returncode_returns_false(self, tmp_path):
        """Non-zero yt-dlp exit code must return False."""
        dl = self._make_downloader(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ERROR: unsupported URL"
        with patch("subprocess.run", return_value=mock_result):
            result = dl.download("https://example.com/video", str(tmp_path))
        assert result is False

    def test_success_returns_true(self, tmp_path):
        """Zero return code must return True."""
        dl = self._make_downloader(tmp_path)
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("subprocess.run", return_value=mock_result):
            result = dl.download("https://example.com/video", str(tmp_path))
        assert result is True

    def test_proxy_injected_when_available(self, tmp_path):
        """When proxy_pool provides a proxy, yt-dlp cmd must include --proxy."""
        from smart_spider.smart_spider import VideoDownloader
        from smart_spider.http_client import ProxyPool
        pool = ProxyPool(["http://1.2.3.4:8080"])
        dl = VideoDownloader(output_dir=str(tmp_path), proxy_pool=pool)
        cmd = dl._ytdlp_cmd("https://example.com/video", str(tmp_path),
                            proxy="http://1.2.3.4:8080")
        assert "--proxy" in cmd
        assert "http://1.2.3.4:8080" in cmd
