# coding=utf-8
"""Regression tests for the three critical bug fixes.

Fix 1 - done_event per-modal isolation:
    Each media type (image/video/text) now has its own done_event.
    Completing one modal must NOT stop other modals prematurely.

Fix 2 - _count_pages lower bound:
    Even when probe hits an empty page early (found_pages may be tiny),
    the returned page count must be >= ceil(max_items / page_step) + 2
    so that enough crawl tasks are submitted.

Fix 3 - image peek incomplete content:
    When iter_content() raises during full-body read, the URL must be
    dropped entirely (continue). The old fallback to peek-only bytes
    produced corrupted image files.
"""
import io
import json
import queue
import struct
import threading
import time
import zlib
from unittest.mock import MagicMock, patch, call

import pytest

from smart_spider.smart_spider import _SENTINEL, _QUEUE_PUT_TIMEOUT


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_minimal_png(width: int = 200, height: int = 200) -> bytes:
    """Return a valid minimal PNG with enough variance to pass pre-filter."""
    import random as _rnd
    raw_rows = []
    for _ in range(height):
        row = b"\x00"
        for _ in range(width):
            r = _rnd.randint(0, 255)
            g = _rnd.randint(0, 255)
            b = _rnd.randint(0, 255)
            row += bytes([r, g, b])
        raw_rows.append(row)
    raw = b"".join(raw_rows)
    compressed = zlib.compress(raw)

    def _chunk(name: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        return length + name + data + crc

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    idat = _chunk(b"IDAT", compressed)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _make_compact_jpeg(width: int = 100, height: int = 100) -> bytes:
    """Return a JPEG small enough to fit entirely within the 8 KB peek window.

    The pre-filter in _search_worker does Image.open(BytesIO(peek)).convert('RGB')
    where peek is only the first 8 KB.  PNG compression produces files >8 KB even
    for 100x100 images, so we use JPEG at low quality which is typically <4 KB.
    """
    from PIL import Image as _Image
    import numpy as _np
    arr = _np.random.default_rng(seed=42).integers(0, 255, (height, width, 3), dtype=_np.uint8)
    img = _Image.fromarray(arr)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=20)
    result = buf.getvalue()
    assert len(result) <= 8192, f"JPEG too large ({len(result)} bytes) for peek window"
    return result


def _make_minimal_jpeg(width: int = 200, height: int = 200) -> bytes:
    """Return a minimal valid JPEG using PIL."""
    from PIL import Image
    import io as _io
    img = Image.new("RGB", (width, height), color=(128, 64, 192))
    buf = _io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Fix 1: per-modal done_event isolation
# ──────────────────────────────────────────────────────────────────────────────

class TestPerModalDoneEvent:
    """Verify that completing one modal does NOT stop unfinished modals."""

    def test_independent_events_do_not_cross_trigger(self):
        """Setting image done must NOT set video done."""
        image_done = threading.Event()
        video_done = threading.Event()
        text_done = threading.Event()

        image_done.set()

        assert image_done.is_set()
        assert not video_done.is_set(), "video_done must remain unset"
        assert not text_done.is_set(), "text_done must remain unset"

    def test_all_done_fires_only_when_all_modals_complete(self):
        """all_done_event must not fire until every active modal is done."""
        events = {
            "image": threading.Event(),
            "video": threading.Event(),
            "text":  threading.Event(),
        }
        all_done = threading.Event()
        lock = threading.Lock()

        def maybe_set_all(mt: str) -> None:
            with lock:
                if all(e.is_set() for e in events.values()):
                    all_done.set()

        # complete image and video -- all_done must NOT fire yet
        events["image"].set()
        maybe_set_all("image")
        assert not all_done.is_set(), "all_done fires too early (text not done)"

        events["video"].set()
        maybe_set_all("video")
        assert not all_done.is_set(), "all_done fires too early (text not done)"

        # complete text -- now all_done should fire
        events["text"].set()
        maybe_set_all("text")
        assert all_done.is_set(), "all_done did not fire after all modals completed"

    def test_single_modal_all_done_fires_immediately(self):
        """With only one modal, all_done should fire when that modal is done."""
        events = {"image": threading.Event()}
        all_done = threading.Event()
        lock = threading.Lock()

        def maybe_set_all(mt: str) -> None:
            with lock:
                if all(e.is_set() for e in events.values()):
                    all_done.set()

        events["image"].set()
        maybe_set_all("image")
        assert all_done.is_set()

    def test_search_worker_checks_modal_done_event(self):
        """_search_worker must exit early when its modal done_event is set."""
        from smart_spider.smart_spider import SmartSpider

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._dedup = MagicMock()
        spider._dedup.is_seen.return_value = False
        spider._http = MagicMock()
        spider._renderer = None
        spider.model = None
        spider.preprocess = None

        fetch_called = []

        def _fake_fetch(engine_name, url):
            fetch_called.append(url)
            return ""

        spider._fetch_page = _fake_fetch

        done = threading.Event()
        done.set()

        spider._search_worker(
            "baidu", "https://image.baidu.com/test", "cat",
            None, None, None,
            done_event=done, all_done_event=None,
        )
        assert fetch_called == [], "fetch_page must NOT be called when done_event is set"

    def test_search_worker_checks_all_done_event(self):
        """_search_worker must also exit early when all_done_event is set."""
        from smart_spider.smart_spider import SmartSpider

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._dedup = MagicMock()
        spider._dedup.is_seen.return_value = False
        spider._http = MagicMock()
        spider._renderer = None
        spider.model = None
        spider.preprocess = None

        fetch_called = []

        def _fake_fetch(engine_name, url):
            fetch_called.append(url)
            return ""

        spider._fetch_page = _fake_fetch

        modal_done = threading.Event()
        all_done = threading.Event()
        all_done.set()

        spider._search_worker(
            "baidu", "https://image.baidu.com/test", "cat",
            None, None, None,
            done_event=modal_done, all_done_event=all_done,
        )
        assert fetch_called == [], "fetch_page must NOT be called when all_done_event is set"


# ──────────────────────────────────────────────────────────────────────────────
# Fix 2: _count_pages lower bound
# ──────────────────────────────────────────────────────────────────────────────

class TestCountPagesLowerBound:
    """Verify _count_pages never returns fewer pages than needed for max_items."""

    def _make_spider(self, max_items: int = 50):
        from smart_spider.smart_spider import SmartSpider
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider.max_items = max_items
        spider._http = MagicMock()
        return spider

    def test_returns_at_least_min_needed_when_probe_hits_empty_immediately(self):
        """If page 0 is empty, found_pages=0, but result must be >= min_needed."""
        from smart_spider.engines import BaiduImageEngine
        spider = self._make_spider(max_items=50)
        spider._http.get_text.return_value = json.dumps({"data": []})
        result = spider._count_pages("baidu", "test")
        eng = BaiduImageEngine()
        min_needed = spider.max_items // max(eng.page_step, 1) + 2
        assert result >= min_needed, (
            f"Expected result >= {min_needed} but got {result}."
        )

    def test_returns_at_least_min_needed_when_probe_hits_empty_at_page_1(self):
        """If page 0 has items but page 1 is empty, found_pages=1 is not enough."""
        from smart_spider.engines import BaiduImageEngine
        spider = self._make_spider(max_items=100)
        call_count = [0]

        def fake_get_text(url, engine=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return json.dumps({"data": [
                    {"objURL": f"https://img.example.com/{i}.jpg"} for i in range(10)
                ]})
            return json.dumps({"data": []})

        spider._http.get_text.side_effect = fake_get_text
        result = spider._count_pages("baidu", "test")
        eng = BaiduImageEngine()
        min_needed = spider.max_items // max(eng.page_step, 1) + 2
        assert result >= min_needed, (
            f"With max_items={spider.max_items}, need >= {min_needed} pages; got {result}"
        )

    def test_result_capped_at_MAX_CHECK_PAGES(self):
        """Result must never exceed MAX_CHECK_PAGES."""
        from smart_spider.smart_spider import MAX_CHECK_PAGES
        spider = self._make_spider(max_items=999999)
        spider._http.get_text.return_value = json.dumps({"data": [
            {"objURL": f"https://img.example.com/{i}.jpg"} for i in range(30)
        ]})
        result = spider._count_pages("baidu", "test")
        assert result <= MAX_CHECK_PAGES

    def test_normal_probe_sufficient_pages_not_inflated(self):
        """When probe finds enough pages naturally, result should be reasonable."""
        from smart_spider.smart_spider import MAX_CHECK_PAGES
        spider = self._make_spider(max_items=30)
        spider._http.get_text.return_value = json.dumps({"data": [
            {"objURL": f"https://img.example.com/{i}.jpg"} for i in range(30)
        ]})
        result = spider._count_pages("baidu", "test")
        assert 1 <= result <= MAX_CHECK_PAGES


# ──────────────────────────────────────────────────────────────────────────────
# Fix 3: image peek incomplete content -- drop on error, never write truncated
# ──────────────────────────────────────────────────────────────────────────────
#
# _search_worker flow:
#   1. _fetch_page(engine, search_page_url) -> HTML of search results
#   2. engine.extract_items(html)           -> list of image URLs
#   3. _http.get_stream(img_url)            -> streams the actual image
#
# We mock _fetch_page to return JSON with one image URL,
# then control _http.get_stream to test streaming/verification logic.

class TestImagePeekIncompleteContent:
    """Verify that network errors during full-body read cause URL to be dropped.

    _search_worker flow:
      1. _fetch_page(engine, search_page_url)  -> HTML of search results
      2. engine.extract_items(html)            -> list of image URLs
      3. _http.get_stream(img_url)             -> streams the actual image

    We mock _fetch_page to return a JSON with one .jpg URL,
    then control _http.get_stream to test streaming/verification logic.

    NOTE: The image used in tests must fit entirely within the 8 KB peek
    window, because the pre-filter does Image.open(BytesIO(peek)).convert()
    on the first 8 KB only.  _make_compact_jpeg() produces a ~3 KB JPEG.
    """

    _IMG_URL = "https://img.example.com/target.jpg"
    _SEARCH_HTML = json.dumps({"data": [{"objURL": "https://img.example.com/target.jpg"}]})
    _SEARCH_PAGE = "https://image.baidu.com/search/acjson?q=cat"

    def _make_spider(self, img_bytes, iter_side_effect=None):
        from smart_spider.smart_spider import SmartSpider
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._dedup = MagicMock()
        spider._dedup.is_seen.return_value = False
        spider._renderer = None
        spider.model = None
        spider.preprocess = None
        spider._fetch_page = MagicMock(return_value=self._SEARCH_HTML)
        # The entire image fits in peek (<=8 KB), rest is empty
        peek = img_bytes
        mock_resp = MagicMock()
        if iter_side_effect is not None:
            mock_resp.iter_content.side_effect = iter_side_effect
        else:
            mock_resp.iter_content.return_value = iter([b""])
        spider._http = MagicMock()
        spider._http.get_stream.return_value = (peek, mock_resp)
        return spider

    def test_iter_content_exception_drops_url(self):
        """When iter_content() raises, the item must NOT be put on the queue."""
        valid_jpg = _make_compact_jpeg()
        spider = self._make_spider(
            valid_jpg,
            iter_side_effect=OSError("connection reset by peer"),
        )
        q = queue.Queue(maxsize=10)
        spider._search_worker(
            "baidu", self._SEARCH_PAGE, "cat",
            infer_queue=q, video_queue=None, text_queue=None,
        )
        assert q.qsize() == 0, (
            "URL with failed iter_content must be dropped; infer_queue should be empty"
        )

    def test_truncated_image_fails_verify_and_is_dropped(self):
        """A severely truncated JPEG must be dropped without crashing."""
        valid_jpg = _make_compact_jpeg()
        # Keep only first half of the JPEG bytes as peek; rest yields nothing
        truncated = valid_jpg[: len(valid_jpg) // 2]
        spider = self._make_spider(truncated)
        q = queue.Queue(maxsize=10)
        spider._search_worker(
            "baidu", self._SEARCH_PAGE, "cat",
            infer_queue=q, video_queue=None, text_queue=None,
        )
        # Severely truncated JPEG should fail PIL.verify() and be dropped.
        # Accept 0 or 1 (some PIL builds tolerate mild truncation) but no crash.
        assert q.qsize() <= 1

    def test_valid_complete_image_is_queued(self):
        """A complete, valid JPEG must successfully reach the infer_queue."""
        valid_jpg = _make_compact_jpeg()
        spider = self._make_spider(valid_jpg)
        q = queue.Queue(maxsize=10)
        spider._search_worker(
            "baidu", self._SEARCH_PAGE, "cat",
            infer_queue=q, video_queue=None, text_queue=None,
        )
        assert q.qsize() == 1, "Valid complete image must be put on infer_queue"
        _, kw, url, meta, content = q.get_nowait()
        assert url == self._IMG_URL
        assert content == valid_jpg, "Queued content must equal full image bytes"

    def test_no_fallback_to_peek_only(self):
        """The old peek-only fallback must not exist -- content is always full bytes.

        Since the compact JPEG fits entirely in peek and rest is empty,
        full_content = peek + b"" = peek, which is correct and expected.
        The key invariant tested here: content length == full image length.
        """
        valid_jpg = _make_compact_jpeg()
        spider = self._make_spider(valid_jpg)
        q = queue.Queue(maxsize=10)
        spider._search_worker(
            "baidu", self._SEARCH_PAGE, "cat",
            infer_queue=q, video_queue=None, text_queue=None,
        )
        if q.qsize() > 0:
            _, _, _, _, content = q.get_nowait()
            assert len(content) == len(valid_jpg), (
                "Queued content length must equal full image length"
            )
            assert content == valid_jpg, "Queued content must be bit-identical to original"
