# coding=utf-8
"""第四轮修复验证测试。

覆盖 Fix 4 ~ Fix 7：
  Fix 4 : img 使用完整 full_content 而非 peek 残缺数据做 CLIP 推理
  Fix 5 : video_worker / text_worker queue.get() 带超时，防止死锁
  Fix 6 : t.join(timeout=60) + _seen_lock 幽灵代码清理
  Fix 7 : pytest 配置单一化（pytest.ini 已删除）
"""
import io
import queue
import struct
import threading
import time
import types
from io import BytesIO
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# 辅助：生成合法的最小 JPEG / PNG bytes
# ---------------------------------------------------------------------------

def _make_minimal_jpeg(width: int = 200, height: int = 200) -> bytes:
    """生成仅包含 SOI + APP0 + 尺寸信息的最小合法 JPEG header（PIL 可读取尺寸）。
    
    完整像素数据不写入，verify() 会失败 —— 但尺寸读取（Image.open + .width/.height）会成功。
    这模拟"peek 阶段能读出尺寸但无法验证完整性"的真实情况。
    """
    # 最小 JPEG：SOI + APP0 + SOF0（含宽高）+ EOI
    # 这足够让 PIL 读出 width / height
    soi = b"\xff\xd8"
    app0 = (b"\xff\xe0" + struct.pack(">H", 16) +
            b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    sof0 = (b"\xff\xc0" + struct.pack(">H", 17) +
            b"\x08" +  # precision
            struct.pack(">HH", height, width) +
            b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01")
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + eoi


def _make_solid_png(width: int = 4, height: int = 4, color: int = 128) -> bytes:
    """生成一张纯色 PNG（用于 std < 10 过滤测试）。"""
    from PIL import Image
    img = Image.new("RGB", (width, height), (color, color, color))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_real_png(width: int = 200, height: int = 200) -> bytes:
    """生成一张有内容的真实 PNG（棋盘格，32×32 下采样后 std 远 > 10）。"""
    import numpy as np
    from PIL import Image
    # 棋盘格：黑白交替，压缩后仍保持高方差
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    block = 20
    for r in range(height):
        for c in range(width):
            if (r // block + c // block) % 2 == 0:
                arr[r, c] = [255, 255, 255]
            else:
                arr[r, c] = [0, 0, 0]
    img = Image.fromarray(arr, "RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fix 4：img 使用完整 full_content 而非 peek 残缺数据
# ---------------------------------------------------------------------------

class TestFix4ImgFromFullContent:
    """验证 _search_worker 将 full_content 解码的 img 入队，而非 peek 残缺数据。"""

    def _make_spider(self):
        """构造仅包含必要属性的 SmartSpider stub。"""
        from smart_spider.smart_spider import (
            SmartSpider, _QUEUE_PUT_TIMEOUT, _detect_ext, _SENTINEL
        )
        from smart_spider.engines import MediaType, RenderMode

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider.media_types = {"image"}
        spider.similarity_threshold = 0.20
        spider.batch_size = 16
        spider._pbar_lock = threading.Lock()
        spider._dedup = MagicMock()
        spider._dedup.is_seen.return_value = False
        return spider

    def test_infer_queue_receives_full_image(self):
        """入队的 img 应由 full_content 解码，宽高与真实图像一致。"""
        from smart_spider.engines import MediaType, RenderMode

        full_png = _make_real_png(300, 250)  # 真实 300×250 PNG
        peek_bytes = 8192
        peek = full_png[:peek_bytes]
        rest = full_png[peek_bytes:]

        # 构造 mock response
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([rest])

        spider = self._make_spider()

        # mock get_stream 返回 (peek, resp)
        spider._http = MagicMock()
        spider._http.get_stream.return_value = (peek, mock_resp)

        # mock 引擎
        mock_eng = MagicMock()
        mock_eng.media_type = MediaType.IMAGE
        mock_eng.render_mode = RenderMode.STATIC
        mock_eng.extract_items.return_value = [{"url": "http://example.com/img.png", "meta": {}}]

        infer_q = queue.Queue(maxsize=10)

        with patch("smart_spider.smart_spider.get_engine", return_value=mock_eng):
            spider._fetch_page = MagicMock(return_value="<html/>")
            spider._is_seen = MagicMock(return_value=False)
            spider._search_worker(
                engine_name="bing",
                page_url="http://example.com/search",
                keyword="test",
                infer_queue=infer_q,
                video_queue=None,
                text_queue=None,
            )

        assert not infer_q.empty(), "infer_queue should have received one item"
        img, kw, url, meta, content = infer_q.get_nowait()

        # img 应从 full_content 解码，尺寸为 300×250
        assert img.width == 300, f"Expected width=300, got {img.width}"
        assert img.height == 250, f"Expected height=250, got {img.height}"
        # content 应是完整 bytes
        assert content == full_png, "full_content must equal the complete image bytes"

    def test_peek_only_image_is_dropped_on_verification_failure(self):
        """如果 full_content verify() 失败，URL 应被丢弃（不入队）。"""
        from smart_spider.engines import MediaType, RenderMode

        # peek 是合法 JPEG header，但 rest 是随机垃圾（模拟截断）
        peek = _make_minimal_jpeg(200, 200)
        garbage_rest = b"\x00" * 100  # 不是有效 JPEG 后缀

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([garbage_rest])

        spider = self._make_spider()
        spider._http = MagicMock()
        spider._http.get_stream.return_value = (peek, mock_resp)

        mock_eng = MagicMock()
        mock_eng.media_type = MediaType.IMAGE
        mock_eng.render_mode = RenderMode.STATIC
        mock_eng.extract_items.return_value = [{"url": "http://x.com/bad.jpg", "meta": {}}]

        infer_q = queue.Queue(maxsize=10)

        with patch("smart_spider.smart_spider.get_engine", return_value=mock_eng):
            spider._fetch_page = MagicMock(return_value="<html/>")
            spider._is_seen = MagicMock(return_value=False)
            spider._search_worker(
                engine_name="bing",
                page_url="http://x.com/search",
                keyword="test",
                infer_queue=infer_q,
                video_queue=None,
                text_queue=None,
            )

        assert infer_q.empty(), "Corrupted image should be dropped, queue must be empty"

    def test_low_variance_image_is_dropped(self):
        """方差过低（纯色）的完整图片应被丢弃。"""
        from smart_spider.engines import MediaType, RenderMode

        solid_png = _make_solid_png(200, 200, color=100)
        peek = solid_png[:8192]
        rest = solid_png[8192:]

        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = iter([rest] if rest else [])

        spider = self._make_spider()
        spider._http = MagicMock()
        spider._http.get_stream.return_value = (peek, mock_resp)

        mock_eng = MagicMock()
        mock_eng.media_type = MediaType.IMAGE
        mock_eng.render_mode = RenderMode.STATIC
        mock_eng.extract_items.return_value = [{"url": "http://x.com/solid.png", "meta": {}}]

        infer_q = queue.Queue(maxsize=10)

        with patch("smart_spider.smart_spider.get_engine", return_value=mock_eng):
            spider._fetch_page = MagicMock(return_value="<html/>")
            spider._is_seen = MagicMock(return_value=False)
            spider._search_worker(
                engine_name="bing",
                page_url="http://x.com/search",
                keyword="test",
                infer_queue=infer_q,
                video_queue=None,
                text_queue=None,
            )

        assert infer_q.empty(), "Solid-color (low-variance) image should be dropped"


# ---------------------------------------------------------------------------
# Fix 5：video_worker / text_worker queue.get() 带超时防死锁
# ---------------------------------------------------------------------------

class TestFix5WorkerGetTimeout:
    """验证 video/text worker 不会因 queue.get() 无超时而永久挂起。"""

    def _build_spider_stub(self):
        from smart_spider.smart_spider import SmartSpider
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider._video_dl = MagicMock()
        spider._video_dl.download.return_value = True
        spider._text_extractor = MagicMock()
        spider._text_extractor.extract.return_value = None  # 无输出
        return spider

    def test_video_worker_exits_on_sentinel_with_timeout(self):
        """video_worker 在收到 _SENTINEL 前应能正常超时循环，收到后正常退出。"""
        from smart_spider.smart_spider import SmartSpider, _SENTINEL
        spider = self._build_stub_for_video()

        vq = queue.Queue()
        done_ev = threading.Event()
        pbar = MagicMock()

        # 先放一个延迟 0.1s 的 sentinel
        def _delayed_sentinel():
            time.sleep(0.15)
            vq.put(_SENTINEL)

        threading.Thread(target=_delayed_sentinel, daemon=True).start()

        t = threading.Thread(
            target=spider._video_worker,
            args=(vq, "/tmp", pbar, done_ev, 1, None),
            daemon=True,
        )
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive(), "video_worker should have exited after receiving SENTINEL"

    def _build_stub_for_video(self):
        from smart_spider.smart_spider import SmartSpider
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider._video_dl = MagicMock()
        spider._video_dl.download.return_value = False
        return spider

    def test_text_worker_exits_on_sentinel_with_timeout(self):
        """text_worker 在收到 _SENTINEL 前应能正常超时循环，收到后正常退出。"""
        from smart_spider.smart_spider import SmartSpider, _SENTINEL
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider._text_extractor = MagicMock()
        spider._text_extractor.extract.return_value = None

        tq = queue.Queue()
        done_ev = threading.Event()
        pbar = MagicMock()

        def _delayed_sentinel():
            time.sleep(0.15)
            tq.put(_SENTINEL)

        threading.Thread(target=_delayed_sentinel, daemon=True).start()

        t = threading.Thread(
            target=spider._text_worker,
            args=(tq, "/tmp", pbar, done_ev, 1, None),
            daemon=True,
        )
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive(), "text_worker should have exited after receiving SENTINEL"

    def test_video_worker_does_not_block_on_empty_queue(self):
        """video_worker 不应在空队列上永久阻塞 —— 带超时后可被中断。"""
        from smart_spider.smart_spider import _SENTINEL
        spider = self._build_stub_for_video()

        vq = queue.Queue()
        done_ev = threading.Event()
        pbar = MagicMock()

        t = threading.Thread(
            target=spider._video_worker,
            args=(vq, "/tmp", pbar, done_ev, 1, None),
            daemon=True,
        )
        t.start()
        time.sleep(0.05)
        assert t.is_alive(), "video_worker should still be running (waiting for items)"
        # 发送毒丸后应该快速退出
        vq.put(_SENTINEL)
        t.join(timeout=2.0)
        assert not t.is_alive(), "video_worker should exit after SENTINEL"

    def test_text_worker_does_not_block_on_empty_queue(self):
        """text_worker 不应在空队列上永久阻塞。"""
        from smart_spider.smart_spider import SmartSpider, _SENTINEL
        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider._text_extractor = MagicMock()
        spider._text_extractor.extract.return_value = None

        tq = queue.Queue()
        done_ev = threading.Event()
        pbar = MagicMock()

        t = threading.Thread(
            target=spider._text_worker,
            args=(tq, "/tmp", pbar, done_ev, 1, None),
            daemon=True,
        )
        t.start()
        time.sleep(0.05)
        assert t.is_alive()
        tq.put(_SENTINEL)
        t.join(timeout=2.0)
        assert not t.is_alive()


# ---------------------------------------------------------------------------
# Fix 6：t.join(timeout) + _seen_lock 幽灵代码清理
# ---------------------------------------------------------------------------

class TestFix6JoinAndSeenLock:
    """验证 _seen_lock 已被移除，且 SmartSpider 实例无此属性。"""

    def test_no_seen_lock_attribute(self):
        """SmartSpider.__init__ 不应再创建 _seen_lock 属性。"""
        from smart_spider.smart_spider import SmartSpider
        import inspect
        src = inspect.getsource(SmartSpider.__init__)
        assert "_seen_lock" not in src, (
            "_seen_lock 幽灵代码应已删除，但仍在 __init__ 中出现"
        )

    def test_join_has_timeout_in_source(self):
        """download() 中所有 t.join() 调用均应带 timeout 参数。"""
        from smart_spider.smart_spider import SmartSpider
        import inspect, re
        src = inspect.getsource(SmartSpider.download)
        # 找出所有 .join( 调用
        joins = re.findall(r't\.join\(([^)]*)\)', src)
        for args in joins:
            assert "timeout" in args, (
                f"t.join({args!r}) 缺少 timeout 参数，存在死锁风险"
            )

    def test_pbar_lock_still_exists(self):
        """_pbar_lock 应仍然存在（不能误删）。"""
        from smart_spider.smart_spider import SmartSpider
        import inspect
        src = inspect.getsource(SmartSpider.__init__)
        assert "_pbar_lock" in src


# ---------------------------------------------------------------------------
# Fix 7：pytest 配置单一化
# ---------------------------------------------------------------------------

class TestFix7PytestConfig:
    """验证 pytest.ini 已删除，配置由 pyproject.toml 统一管理。"""

    def test_pytest_ini_does_not_exist(self, tmp_path):
        """项目根目录下不应存在 pytest.ini。"""
        import os
        project_root = os.path.dirname(os.path.dirname(__file__))
        pytest_ini = os.path.join(project_root, "pytest.ini")
        assert not os.path.exists(pytest_ini), (
            "pytest.ini 应已删除，配置统一放在 pyproject.toml [tool.pytest.ini_options]"
        )

    def test_pyproject_toml_has_pytest_config(self):
        """pyproject.toml 应包含 [tool.pytest.ini_options] 配置节。"""
        import os
        project_root = os.path.dirname(os.path.dirname(__file__))
        pyproject = os.path.join(project_root, "pyproject.toml")
        assert os.path.exists(pyproject), "pyproject.toml 应存在"
        content = open(pyproject).read()
        assert "[tool.pytest.ini_options]" in content, (
            "pyproject.toml 应包含 [tool.pytest.ini_options] 节"
        )


# ---------------------------------------------------------------------------
# 回归：之前所有模块仍可正常导入
# ---------------------------------------------------------------------------

class TestRegressionImports:
    def test_smart_spider_imports_ok(self):
        import smart_spider.smart_spider as m
        assert hasattr(m, "SmartSpider")
        assert hasattr(m, "_ModalDoneEvents")
        assert hasattr(m, "UrlDeduplicator")

    def test_http_client_imports_ok(self):
        import smart_spider.http_client as m
        assert hasattr(m, "SmartHttpClient")
        assert hasattr(m, "ProxyPool")
        assert hasattr(m, "RateLimiter")

    def test_engines_imports_ok(self):
        import smart_spider.engines as m
        assert hasattr(m, "ENGINE_REGISTRY")
        assert len(m.ENGINE_REGISTRY) >= 12

    def test_browser_imports_ok(self):
        import smart_spider.browser as m
        assert hasattr(m, "DynamicRenderer")
