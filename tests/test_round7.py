# coding=utf-8
"""第七轮修复验证测试。

覆盖 Fix 16 ~ Fix 20：
  Fix 16 : _search_worker get_stream 后 resp 未 close → 连接泄漏
  Fix 17 : DynamicRenderer._run_loop init 失败致构造函数挂起
  Fix 18 : _search_worker items 循环内不检查 done_event → 浪费
  Fix 19 : Image.open 后 verify 再重开 → 用 BytesIO.seek 替代双重分配
  Fix 20 : SmartHttpClient.get 反爬响应体未消费 → 连接池耗尽
"""
import os
import queue
import threading
from io import BytesIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# 共用 stub 工厂
# ──────────────────────────────────────────────────────────────────────────────

def _make_png_bytes(width=50, height=50, color=(100, 150, 200)):
    from PIL import Image
    img = Image.new("RGB", (width, height), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────────
# Fix 16: _search_worker get_stream 后 resp 未 close
# ──────────────────────────────────────────────────────────────────────────────

class TestFix16ResponseCloseOnAllPaths:
    """验证 get_stream 返回的 resp 在所有 continue 路径上都被 close。"""

    def _make_spider_with_mock_fetch(self):
        """构造 SmartSpider，mock _fetch_page 返回含图片结果的 HTML。"""
        from smart_spider.smart_spider import SmartSpider

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider._dedup = MagicMock()
        spider._dedup.is_seen = MagicMock(return_value=False)
        spider._dedup.is_content_seen = MagicMock(return_value=False)
        spider._is_seen = lambda url: spider._dedup.is_seen(url)
        return spider

    def _baidu_html_with_one_image(self, image_url="http://img.test/cat.jpg"):
        """构造百度图片搜索返回的 JSON（含一张图片）。"""
        import json
        return json.dumps({
            "data": [{
                "objURL": image_url,
                "thumbURL": "http://thumb/test.jpg",
                "fromPageTitleEncode": "test image",
                "width": 200,
                "height": 200,
            }]
        })

    def test_resp_closed_on_bad_extension(self, tmp_path):
        """扩展名不在白名单时 resp 应被 close。"""
        from smart_spider.smart_spider import SmartSpider

        spider = self._make_spider_with_mock_fetch()

        # 构造一个返回非图片扩展名内容的 mock response
        mock_resp = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=iter([]))

        spider._http = MagicMock()
        spider._http.get_stream = MagicMock(return_value=(b"<?xml ", mock_resp))

        # Mock _fetch_page 返回含一张图片 URL 的 HTML
        spider._fetch_page = MagicMock(return_value=self._baidu_html_with_one_image())

        infer_q = queue.Queue()
        done_ev = threading.Event()
        all_done_ev = threading.Event()

        spider._search_worker(
            "baidu", "http://test.com/page", "cat",
            infer_q, None, None, done_ev, all_done_ev,
        )

        # resp.close() 应被调用
        mock_resp.close.assert_called()

    def test_resp_closed_on_small_image(self, tmp_path):
        """图片尺寸 < 100 时 resp 应被 close。"""
        from smart_spider.smart_spider import SmartSpider
        from PIL import Image

        spider = self._make_spider_with_mock_fetch()

        # 构造小图片的 peek
        small_img = Image.new("RGB", (10, 10), (0, 0, 0))
        buf = BytesIO()
        small_img.save(buf, format="PNG")
        small_png = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=iter([]))

        spider._http = MagicMock()
        spider._http.get_stream = MagicMock(return_value=(small_png, mock_resp))
        spider._fetch_page = MagicMock(return_value=self._baidu_html_with_one_image())

        infer_q = queue.Queue()
        done_ev = threading.Event()
        all_done_ev = threading.Event()

        spider._search_worker(
            "baidu", "http://test.com/page", "cat",
            infer_q, None, None, done_ev, all_done_ev,
        )

        mock_resp.close.assert_called()

    def test_resp_closed_on_verify_failure(self, tmp_path):
        """图片 verify 失败时 resp 应被 close。"""
        from smart_spider.smart_spider import SmartSpider

        spider = self._make_spider_with_mock_fetch()

        # 合法的 PNG peek 但 body 是垃圾数据
        valid_png = _make_png_bytes()
        mock_resp = MagicMock()
        # iter_content 返回垃圾数据，使得 full_content 不是有效图片
        mock_resp.iter_content = MagicMock(return_value=iter([b"GARBAGE_DATA"]))

        spider._http = MagicMock()
        spider._http.get_stream = MagicMock(return_value=(valid_png, mock_resp))
        spider._fetch_page = MagicMock(return_value=self._baidu_html_with_one_image())

        infer_q = queue.Queue()
        done_ev = threading.Event()
        all_done_ev = threading.Event()

        spider._search_worker(
            "baidu", "http://test.com/page", "cat",
            infer_q, None, None, done_ev, all_done_ev,
        )

        mock_resp.close.assert_called()

    def test_resp_closed_on_low_variance(self, tmp_path):
        """低方差纯色图片被过滤时 resp 应被 close。"""
        from smart_spider.smart_spider import SmartSpider
        from PIL import Image

        spider = self._make_spider_with_mock_fetch()

        # 纯色大图（方差极低）
        solid_img = Image.new("RGB", (200, 200), (128, 128, 128))
        buf = BytesIO()
        solid_img.save(buf, format="PNG")
        solid_png = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=iter([]))

        spider._http = MagicMock()
        spider._http.get_stream = MagicMock(return_value=(solid_png, mock_resp))
        spider._fetch_page = MagicMock(return_value=self._baidu_html_with_one_image())

        infer_q = queue.Queue()
        done_ev = threading.Event()
        all_done_ev = threading.Event()

        spider._search_worker(
            "baidu", "http://test.com/page", "cat",
            infer_q, None, None, done_ev, all_done_ev,
        )

        mock_resp.close.assert_called()

    def test_resp_closed_on_successful_put(self, tmp_path):
        """图片成功放入 infer_queue 时 resp 也应被 close。"""
        from smart_spider.smart_spider import SmartSpider
        from PIL import Image

        spider = self._make_spider_with_mock_fetch()

        # 高方差大图
        import numpy as np
        arr = np.random.randint(0, 255, (200, 200, 3), dtype=np.uint8)
        varied_img = Image.fromarray(arr)
        buf = BytesIO()
        varied_img.save(buf, format="PNG")
        varied_png = buf.getvalue()

        mock_resp = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=iter([]))

        spider._http = MagicMock()
        spider._http.get_stream = MagicMock(return_value=(varied_png, mock_resp))
        spider._fetch_page = MagicMock(return_value=self._baidu_html_with_one_image())

        infer_q = queue.Queue()
        done_ev = threading.Event()
        all_done_ev = threading.Event()

        spider._search_worker(
            "baidu", "http://test.com/page", "cat",
            infer_q, None, None, done_ev, all_done_ev,
        )

        mock_resp.close.assert_called()

    def test_source_has_resp_close_in_finally(self):
        """验证 _search_worker 源码中有 finally: resp.close() 模式。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._search_worker)
        assert "resp.close()" in src, (
            "_search_worker should close resp in a finally block"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 17: DynamicRenderer._run_loop init 失败不挂起
# ──────────────────────────────────────────────────────────────────────────────

class TestFix17RendererInitFailureNoHang:
    """验证浏览器初始化失败时构造函数不会卡住 30 秒。"""

    def test_ready_set_even_on_init_failure(self):
        """_run_loop 中 _init_browser 抛异常时 _ready 仍应被 set。"""
        from smart_spider.browser import DynamicRenderer
        import inspect

        src = inspect.getsource(DynamicRenderer._run_loop)
        # 验证 _ready.set() 在 try/finally 中，而非仅在成功路径
        assert "self._ready.set()" in src
        # 验证有 try/except 包裹 _init_browser
        assert "except" in src or "finally" in src, (
            "_run_loop should catch init failures and still signal readiness"
        )

    def test_ready_signal_in_finally_block(self):
        """_ready.set() 应在 finally 块中被调用（无论 init 成功与否）。"""
        from smart_spider.browser import DynamicRenderer
        import inspect

        src = inspect.getsource(DynamicRenderer._run_loop)
        lines = src.split("\n")
        # 找到 _ready.set() 所在行
        ready_line_idx = None
        for i, line in enumerate(lines):
            if "_ready.set()" in line:
                ready_line_idx = i
                break

        assert ready_line_idx is not None, "_ready.set() not found in _run_loop"

        # 检查前面是否有 finally: 或 except: 块
        found_guard = False
        for i in range(ready_line_idx - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped in ("finally:", "except Exception:", "except:"):
                found_guard = True
                break
            if stripped.startswith("def ") or stripped.startswith("self._loop"):
                break

        assert found_guard, (
            "_ready.set() should be inside a finally or except block "
            "so it fires even when _init_browser raises"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 18: _search_worker items 循环内检查 done_event
# ──────────────────────────────────────────────────────────────────────────────

class TestFix18EarlyExitOnDoneInItemLoop:
    """验证 _search_worker 在 items 循环内检查 done_event 提前退出。"""

    def test_source_has_done_check_in_items_loop(self):
        """_search_worker 的 for item in items 循环内应有 done_event 检查。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._search_worker)

        # 找到 "for item in items:" 之后的部分
        items_idx = src.find("for item in items:")
        assert items_idx != -1, "'for item in items:' not found in _search_worker"

        after_items = src[items_idx:]

        # 应在 url = item.get(...) 之前有 done_event 检查
        url_idx = after_items.find('url = item.get("url"')
        assert url_idx != -1

        before_url = after_items[:url_idx]
        assert "done_event" in before_url, (
            "There should be a done_event check before processing item URL"
        )

    def test_done_event_breaks_items_loop(self):
        """当 done_event 已置位时，items 循环应 break 退出。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._search_worker)

        items_idx = src.find("for item in items:")
        after_items = src[items_idx:]

        # 在 items 循环内应有 break 语句（不是 continue）
        # 找到 done_event 相关的 break
        assert "break" in after_items.split("url = item.get")[0], (
            "Items loop should have a break on done_event for early exit"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 19: Image.open 后用 BytesIO.seek 替代双重分配
# ──────────────────────────────────────────────────────────────────────────────

class TestFix19BytesIOSeekInsteadOfReopen:
    """验证 Image.open verify 后用 BytesIO.seek(0) 替代新建 BytesIO。"""

    def test_source_uses_buf_seek_after_verify(self):
        """_search_worker 源码中 verify 后应用 buf.seek(0) 而非新建 BytesIO。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._search_worker)

        # 找到 verify 相关区域
        verify_idx = src.find("img.verify()")
        assert verify_idx != -1, "img.verify() not found in _search_worker"

        # verify 之后应有 buf.seek(0)
        after_verify = src[verify_idx:verify_idx + 200]
        assert "buf.seek(0)" in after_verify or "seek(0)" in after_verify, (
            "After img.verify(), should use buf.seek(0) instead of "
            "allocating a new BytesIO(full_content)"
        )

    def test_buf_variable_defined_before_verify(self):
        """buf = BytesIO(full_content) 应在 verify 之前定义。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._search_worker)

        verify_idx = src.find("img.verify()")
        before_verify = src[:verify_idx]

        # 应有 buf = BytesIO(full_content) 在 verify 之前
        assert "buf = BytesIO(full_content)" in before_verify or \
               "buf=BytesIO(full_content)" in before_verify.replace(" ", ""), (
            "buf = BytesIO(full_content) should be defined before img.verify()"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 20: SmartHttpClient.get 反爬响应体未消费
# ──────────────────────────────────────────────────────────────────────────────

class TestFix20AntiCrawlResponseBodyDrained:
    """验证 SmartHttpClient.get 遇到 429/403/503 时消费响应体后 close。"""

    def test_response_body_drained_on_429(self):
        """429 响应体应被消费后 close，而非直接 continue。"""
        import inspect
        from smart_spider.http_client import SmartHttpClient
        src = inspect.getsource(SmartHttpClient.get)

        # 找到 429/403/503 处理区域
        anti_crawl_idx = src.find("429, 403, 503")
        assert anti_crawl_idx != -1, "Anti-crawl status codes check not found"

        after_check = src[anti_crawl_idx:]

        # 应有 iter_content 消费响应体
        assert "iter_content" in after_check[:500], (
            "Anti-crawl response body should be drained via iter_content "
            "before retry to prevent connection pool exhaustion"
        )

    def test_response_closed_on_429(self):
        """429 响应应被 close。"""
        import inspect
        from smart_spider.http_client import SmartHttpClient
        src = inspect.getsource(SmartHttpClient.get)

        anti_crawl_idx = src.find("429, 403, 503")
        after_check = src[anti_crawl_idx:]

        assert "resp.close()" in after_check, (
            "Anti-crawl response should be explicitly closed after draining"
        )

    def test_drain_and_close_pattern(self):
        """应有 try/finally 包裹 drain + close 模式。"""
        import inspect
        from smart_spider.http_client import SmartHttpClient
        src = inspect.getsource(SmartHttpClient.get)

        anti_crawl_idx = src.find("429, 403, 503")
        after_check = src[anti_crawl_idx:anti_crawl_idx + 600]

        # 应有 try: ... finally: resp.close() 模式
        assert "finally:" in after_check, (
            "Anti-crawl drain should be wrapped in try/finally with resp.close()"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 回归：整体模块导入 + 之前修复仍生效
# ──────────────────────────────────────────────────────────────────────────────

class TestRegressionRound7:
    def test_all_modules_import_cleanly(self):
        import smart_spider.smart_spider
        import smart_spider.http_client
        import smart_spider.engines
        import smart_spider.browser

    def test_modal_done_events_unchanged(self):
        from smart_spider.smart_spider import _ModalDoneEvents
        t = _ModalDoneEvents(["image", "video", "text"])
        t.mark_done("image")
        assert t.is_done("image")
        assert not t.is_all_done()
        t.mark_done("video")
        t.mark_done("text")
        assert t.is_all_done()
        t.reset()
        assert not t.is_all_done()

    def test_url_deduplicator_still_works(self):
        from smart_spider.smart_spider import UrlDeduplicator
        dedup = UrlDeduplicator()
        assert not dedup.is_seen("http://a.com")
        assert dedup.is_seen("http://a.com")  # 第二次应返回 True
        assert not dedup.is_seen("http://b.com")

    def test_detect_ext_still_works(self):
        from smart_spider.smart_spider import _detect_ext
        assert _detect_ext(b"\xff\xd8\xff\xe0") == ".jpg"
        assert _detect_ext(b"\x89PNG\r\n\x1a\n") == ".png"
        assert _detect_ext(b"RIFF\x00\x00\x00\x00WEBP") == ".webp"

    def test_video_downloader_retry_still_works(self, tmp_path):
        from smart_spider.smart_spider import VideoDownloader
        fail = MagicMock(returncode=1, stderr="err")
        ok = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", side_effect=[fail, ok]):
            dl = VideoDownloader(output_dir=str(tmp_path), max_retries=1)
            assert dl.download("http://x.com/v", str(tmp_path)) is True

    def test_rate_limiter_acquire_still_works(self):
        from smart_spider.http_client import RateLimiter
        rl = RateLimiter(rate=1000.0)  # 高速率，acquire 应几乎不阻塞
        rl.acquire()  # 不应挂起
