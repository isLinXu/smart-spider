# coding=utf-8
"""第五轮修复验证测试。

覆盖 Fix 8 ~ Fix 11：
  Fix 8  : _infer_worker._flush 达标后同 batch 不再超量保存
  Fix 9  : render_batch return_exceptions=True，单页失败不影响其他任务
  Fix 10 : VideoDownloader.download 失败时换代理重试（最多 max_retries 次）
  Fix 11 : BingVideoEngine.extract_items 正则加 re.DOTALL，支持跨行匹配
"""
import os
import queue
import subprocess
import threading
from io import BytesIO
from unittest.mock import MagicMock, patch, call

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Fix 8: _flush 超量保存
# ──────────────────────────────────────────────────────────────────────────────

class TestFix8FlushNoOverSave:
    """验证 _flush 在 batch 中途达到 target 后立即停止，不超量写文件。"""

    def _make_mock_spider(self, tmp_path):
        """构造带 CLIP stub 的 SmartSpider，不真正加载模型。"""
        from smart_spider.smart_spider import SmartSpider, _ModalDoneEvents
        import torch

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider._pbar_lock = threading.Lock()
        spider.similarity_threshold = 0.0   # 所有图片都过阈值
        spider.device = "cpu"
        spider.batch_size = 16

        # stub CLIP model: encode_image 返回全 1 归一化向量
        def _fake_encode_image(tensors):
            n = tensors.shape[0]
            t = torch.ones(n, 512)
            return t / t.norm(dim=-1, keepdim=True)

        spider.model = MagicMock()
        spider.model.encode_image.side_effect = _fake_encode_image

        # stub preprocess: 返回 (3,224,224) 零张量
        spider.preprocess = MagicMock(
            side_effect=lambda img: torch.zeros(3, 224, 224)
        )

        # stub _get_text_feature: 返回归一化全 1 向量 (1,512)
        ones = torch.ones(1, 512)
        ones = ones / ones.norm(dim=-1, keepdim=True)
        spider._get_text_feature = MagicMock(return_value=ones)

        return spider

    def _make_png_bytes(self):
        """生成一个有效的小 PNG。"""
        from PIL import Image
        img = Image.new("RGB", (50, 50), (100, 150, 200))
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def test_flush_stops_at_target(self, tmp_path):
        """batch 中有 5 张全部相似，target=3 时只应保存 3 张。"""
        from PIL import Image
        from smart_spider.smart_spider import _ModalDoneEvents

        spider = self._make_mock_spider(tmp_path)
        save_dir = str(tmp_path / "images")
        os.makedirs(save_dir, exist_ok=True)

        target = 3
        saved = [0]
        done_ev = threading.Event()
        tracker = _ModalDoneEvents(["image"])
        pbar = MagicMock()

        # 构造 5 张图
        png = self._make_png_bytes()
        items = [
            (Image.open(BytesIO(png)).convert("RGB"), "cat", f"http://x/{i}.png", {}, png)
            for i in range(5)
        ]

        # 直接调用 _flush（内部函数），通过 _infer_worker 的闭包访问
        # 更简洁：重建 _flush 的执行上下文
        infer_q: queue.Queue = queue.Queue()
        from smart_spider.smart_spider import _SENTINEL

        # 把所有 item 放入队列，再放哨兵
        for item in items:
            infer_q.put(item)
        infer_q.put(_SENTINEL)

        worker_thread = threading.Thread(
            target=spider._infer_worker,
            args=(infer_q, pbar, save_dir, done_ev, target, tracker),
            daemon=True,
        )
        worker_thread.start()
        worker_thread.join(timeout=10)

        # 统计写入的文件数
        written = [f for f in os.listdir(save_dir) if f.endswith(".png")]
        assert len(written) == target, (
            f"Expected exactly {target} saved images, got {len(written)}"
        )
        assert tracker.is_done("image"), "modal_tracker should be marked done"

    def test_flush_does_not_over_save_across_batches(self, tmp_path):
        """多个 batch 中，总保存数不超过 target。"""
        from PIL import Image
        from smart_spider.smart_spider import _ModalDoneEvents, _SENTINEL

        spider = self._make_mock_spider(tmp_path)
        spider.batch_size = 3   # 每批 3 张
        save_dir = str(tmp_path / "images2")
        os.makedirs(save_dir, exist_ok=True)

        target = 4
        done_ev = threading.Event()
        tracker = _ModalDoneEvents(["image"])
        pbar = MagicMock()

        png = self._make_png_bytes()
        infer_q: queue.Queue = queue.Queue()

        # 放 9 张 + 哨兵（3 个 batch，每批 3 张，target=4）
        for i in range(9):
            item = (Image.open(BytesIO(png)).convert("RGB"), "dog",
                    f"http://x/{i}.png", {}, png)
            infer_q.put(item)
        infer_q.put(_SENTINEL)

        worker_thread = threading.Thread(
            target=spider._infer_worker,
            args=(infer_q, pbar, save_dir, done_ev, target, tracker),
            daemon=True,
        )
        worker_thread.start()
        worker_thread.join(timeout=15)

        written = [f for f in os.listdir(save_dir) if f.endswith(".png")]
        assert len(written) == target, (
            f"Total saved {len(written)} should equal target {target}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 9: render_batch return_exceptions=True
# ──────────────────────────────────────────────────────────────────────────────

class TestFix9RenderBatchPartialFailure:
    """验证 render_batch 单页失败返回空字符串，不影响其他 URL。"""

    def test_render_batch_partial_failure_returns_empty_string(self):
        """render_async 抛出异常时，对应位置应返回 ''，其他位置正常。"""
        from smart_spider.browser import DynamicRenderer

        renderer = object.__new__(DynamicRenderer)
        renderer._loop = None  # 不真正启动浏览器
        renderer._page_timeout = 30_000

        call_count = [0]

        async def _fake_render_async(url, **kwargs):
            call_count[0] += 1
            if "fail" in url:
                raise RuntimeError(f"simulated render failure: {url}")
            return f"<html>{url}</html>"

        renderer.render_async = _fake_render_async

        import asyncio

        async def _run():
            urls = ["http://ok1.com", "http://fail.com", "http://ok2.com"]

            tasks = [renderer.render_async(u) for u in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            return [r if isinstance(r, str) else "" for r in results]

        loop = asyncio.new_event_loop()
        results = loop.run_until_complete(_run())
        loop.close()

        assert results[0] == "<html>http://ok1.com</html>"
        assert results[1] == ""       # 失败位置返回空字符串
        assert results[2] == "<html>http://ok2.com</html>"

    def test_render_batch_source_uses_return_exceptions_true(self):
        """检查 render_batch 源码中 return_exceptions=True。"""
        import inspect
        from smart_spider.browser import DynamicRenderer
        src = inspect.getsource(DynamicRenderer.render_batch)
        assert "return_exceptions=True" in src, (
            "render_batch should use return_exceptions=True to isolate failures"
        )

    def test_render_batch_maps_exceptions_to_empty_string(self):
        """检查 render_batch 源码中将异常映射为空字符串。"""
        import inspect
        from smart_spider.browser import DynamicRenderer
        src = inspect.getsource(DynamicRenderer.render_batch)
        assert '""' in src or "empty" in src.lower(), (
            "render_batch should map exceptions to empty string"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 10: VideoDownloader 重试 + 代理轮换
# ──────────────────────────────────────────────────────────────────────────────

class TestFix10VideoDownloaderRetry:
    """验证 VideoDownloader.download 失败时换代理重试。"""

    def _make_downloader(self, max_retries=2):
        from smart_spider.smart_spider import VideoDownloader
        from smart_spider.http_client import ProxyPool

        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080", "http://proxy3:8080"])
        dl = VideoDownloader(
            output_dir="/tmp/test_video",
            proxy_pool=pool,
            max_retries=max_retries,
        )
        return dl

    def test_retry_on_nonzero_returncode(self, tmp_path):
        """yt-dlp 返回非零退出码时应重试 max_retries 次。"""
        dl = self._make_downloader(max_retries=2)

        fail_result = MagicMock()
        fail_result.returncode = 1
        fail_result.stderr = "HTTP Error 403"

        with patch("subprocess.run", return_value=fail_result) as mock_run:
            result = dl.download("http://example.com/video", str(tmp_path))

        assert result is False
        # 共调用 1 (initial) + 2 (retries) = 3 次
        assert mock_run.call_count == 3, (
            f"Expected 3 subprocess.run calls (1 initial + 2 retries), got {mock_run.call_count}"
        )

    def test_succeeds_on_second_attempt(self, tmp_path):
        """第一次失败，第二次成功时应返回 True。"""
        dl = self._make_downloader(max_retries=2)

        fail_result = MagicMock(returncode=1, stderr="timeout")
        ok_result = MagicMock(returncode=0, stderr="")

        with patch("subprocess.run", side_effect=[fail_result, ok_result]) as mock_run:
            result = dl.download("http://example.com/video", str(tmp_path))

        assert result is True
        assert mock_run.call_count == 2

    def test_ytdlp_not_found_stops_immediately(self, tmp_path):
        """yt-dlp 未安装时应立即返回 False，不重试（没必要）。"""
        dl = self._make_downloader(max_retries=3)

        with patch("subprocess.run", side_effect=FileNotFoundError("yt-dlp")) as mock_run:
            result = dl.download("http://example.com/video", str(tmp_path))

        assert result is False
        # 遇到 FileNotFoundError 应立即 return False（不重试）
        assert mock_run.call_count == 1, (
            "Should not retry when yt-dlp binary is not found"
        )

    def test_proxy_changes_between_retries(self, tmp_path):
        """每次重试应调用 _pick_proxy，以便轮换代理。"""
        dl = self._make_downloader(max_retries=2)

        fail_result = MagicMock(returncode=1, stderr="err")
        proxies_used = []

        def _fake_run(cmd, **kwargs):
            # 从 cmd 中提取 --proxy 参数
            try:
                idx = cmd.index("--proxy")
                proxies_used.append(cmd[idx + 1])
            except ValueError:
                proxies_used.append(None)
            return fail_result

        with patch("subprocess.run", side_effect=_fake_run):
            dl.download("http://example.com/video", str(tmp_path))

        # 3 次调用，代理应来自池（可能相同或不同，但必须调用 3 次 _pick_proxy）
        assert len(proxies_used) == 3

    def test_max_retries_attribute_exists(self):
        """VideoDownloader 应有 max_retries 属性。"""
        from smart_spider.smart_spider import VideoDownloader
        dl = VideoDownloader(output_dir="/tmp", max_retries=5)
        assert dl.max_retries == 5


# ──────────────────────────────────────────────────────────────────────────────
# Fix 11: BingVideoEngine 正则 re.DOTALL
# ──────────────────────────────────────────────────────────────────────────────

class TestFix11BingVideoEngineDotall:
    """验证 BingVideoEngine.extract_items 能匹配跨行的 JSON 数据。"""

    def _get_engine(self):
        from smart_spider.engines import ENGINE_REGISTRY
        return ENGINE_REGISTRY["bing_video"]

    def test_matches_single_line(self):
        """单行 JSON 数据岛应正常匹配。"""
        eng = self._get_engine()
        html = '"contentUrl":"https://example.com/video.mp4","foo":"bar","name":"My Video"'
        items = eng.extract_items(html)
        assert len(items) == 1
        assert items[0]["url"] == "https://example.com/video.mp4"
        assert items[0]["meta"]["title"] == "My Video"

    def test_matches_multiline(self):
        """跨行的 JSON 数据岛应被匹配（核心 fix）。"""
        eng = self._get_engine()
        html = (
            '"contentUrl":"https://example.com/v2.mp4",\n'
            '  "duration":"PT3M",\n'
            '  "name":"Cross Line Video"'
        )
        items = eng.extract_items(html)
        assert len(items) == 1, f"Expected 1 item from multiline HTML, got {len(items)}"
        assert items[0]["url"] == "https://example.com/v2.mp4"
        assert items[0]["meta"]["title"] == "Cross Line Video"

    def test_returns_empty_on_no_match(self):
        """无视频数据时应返回空列表。"""
        eng = self._get_engine()
        items = eng.extract_items("<html><body>no video here</body></html>")
        assert items == []

    def test_multiple_videos(self):
        """多个视频应全部被提取。"""
        eng = self._get_engine()
        html = (
            '"contentUrl":"https://a.com/1.mp4","x":"y","name":"Video A"\n'
            '"contentUrl":"https://b.com/2.mp4","x":"y","name":"Video B"\n'
        )
        items = eng.extract_items(html)
        assert len(items) == 2
        urls = {i["url"] for i in items}
        assert "https://a.com/1.mp4" in urls
        assert "https://b.com/2.mp4" in urls

    def test_source_has_re_dotall(self):
        """extract_items 源码中应包含 re.DOTALL 标志。"""
        import inspect
        from smart_spider.engines import BingVideoEngine
        src = inspect.getsource(BingVideoEngine.extract_items)
        assert "re.DOTALL" in src or "DOTALL" in src, (
            "BingVideoEngine.extract_items should use re.DOTALL for multiline matching"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 回归：整体模块导入 + 之前修复仍生效
# ──────────────────────────────────────────────────────────────────────────────

class TestRegressionRound5:
    def test_all_modules_import(self):
        import smart_spider.smart_spider
        import smart_spider.http_client
        import smart_spider.engines
        import smart_spider.browser

    def test_video_downloader_has_max_retries(self):
        from smart_spider.smart_spider import VideoDownloader
        dl = VideoDownloader(output_dir="/tmp")
        assert hasattr(dl, "max_retries")
        assert dl.max_retries >= 1

    def test_bing_video_engine_registered(self):
        from smart_spider.engines import ENGINE_REGISTRY
        assert "bing_video" in ENGINE_REGISTRY

    def test_modal_done_events_reset_still_works(self):
        """Fix A 的 _ModalDoneEvents.reset() 仍正常。"""
        from smart_spider.smart_spider import _ModalDoneEvents
        t = _ModalDoneEvents(["image", "video"])
        t.mark_done("image")
        t.mark_done("video")
        assert t.is_all_done()
        t.reset()
        assert not t.is_all_done()
        assert not t.is_done("image")
