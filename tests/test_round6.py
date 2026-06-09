# coding=utf-8
"""第六轮修复验证测试。

覆盖 Fix 12 ~ Fix 15：
  Fix 12 : _flush 达标后跳过 CLIP encode（性能）
  Fix 13 : download() try/finally 保证资源释放
  Fix 14 : _infer_worker 达标后排空队列不积累 batch
  Fix 15 : ProxyPool round_robin _idx 取模后回绕，不跳步
"""
import os
import queue
import threading
from io import BytesIO
from unittest.mock import MagicMock, call, patch

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


def _make_infer_spider(target_saved=3):
    """构造一个带 CLIP stub 的 SmartSpider，不加载真实模型。"""
    import torch
    from smart_spider.smart_spider import SmartSpider

    spider = object.__new__(SmartSpider)
    SmartSpider._ensure_stub_attrs(spider)
    spider._pbar_lock = threading.Lock()
    spider.similarity_threshold = 0.0   # 全部过阈值
    spider.device = "cpu"
    spider.batch_size = 4

    call_counts = {"encode": 0}

    def _fake_encode(tensors):
        call_counts["encode"] += 1
        n = tensors.shape[0]
        t = torch.ones(n, 512)
        return t / t.norm(dim=-1, keepdim=True)

    spider.model = MagicMock()
    spider.model.encode_image.side_effect = _fake_encode
    spider.preprocess = MagicMock(
        side_effect=lambda img: torch.zeros(3, 224, 224)
    )
    ones = torch.ones(1, 512)
    ones = ones / ones.norm(dim=-1, keepdim=True)
    spider._get_text_feature = MagicMock(return_value=ones)

    return spider, call_counts


# ──────────────────────────────────────────────────────────────────────────────
# Fix 12: _flush 达标后不再调用 encode_image
# ──────────────────────────────────────────────────────────────────────────────

class TestFix12FlushSkipsEncodeAfterTarget:
    """验证 _flush 在 saved[0] >= target 时跳过 encode_image。"""

    def test_encode_not_called_after_target_reached(self, tmp_path):
        """达标后送入的 batch 不应再调用 model.encode_image。"""
        from PIL import Image
        from smart_spider.smart_spider import _ModalDoneEvents, _SENTINEL

        spider, call_counts = _make_infer_spider()
        save_dir = str(tmp_path / "imgs")
        os.makedirs(save_dir, exist_ok=True)

        target = 2
        done_ev = threading.Event()
        tracker = _ModalDoneEvents(["image"])
        pbar = MagicMock()

        png = _make_png_bytes()
        infer_q: queue.Queue = queue.Queue()

        # 放 8 张（两个完整 batch，target=2），再放哨兵
        for i in range(8):
            item = (Image.open(BytesIO(png)).convert("RGB"),
                    "cat", f"http://x/{i}.png", {}, png)
            infer_q.put(item)
        infer_q.put(_SENTINEL)

        t = threading.Thread(
            target=spider._infer_worker,
            args=(infer_q, pbar, save_dir, done_ev, target, tracker),
            daemon=True,
        )
        t.start()
        t.join(timeout=15)

        # 第 1 个 batch (4 张) 会调用 encode，达标后后续 batch 不应再 encode
        # target=2，第 1 个 batch 的 flush 完成后 saved=2 -> 后续 batch 直接 return
        assert call_counts["encode"] == 1, (
            f"encode_image should be called exactly once (for first batch), "
            f"but was called {call_counts['encode']} times"
        )

    def test_flush_early_return_in_source(self):
        """验证 _flush 源码中在 encode_image 调用之前有 saved[0] >= target 的提前返回。"""
        import inspect, re
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._infer_worker)

        def _first_real_pos(pattern: str) -> int:
            """返回第一个非注释行上 pattern 的字符位置。"""
            for m in re.finditer(pattern, src):
                pos = m.start()
                line_start = src.rfind("\n", 0, pos) + 1
                line = src[line_start:src.find("\n", pos)]
                if not line.lstrip().startswith("#"):
                    return pos
            return -1

        encode_pos = _first_real_pos(r"encode_image")
        check_pos  = _first_real_pos(r"saved\[0\] >= target")

        assert check_pos != -1, "saved[0] >= target guard not found in _infer_worker"
        assert encode_pos != -1, "encode_image call not found in _infer_worker"
        assert check_pos < encode_pos, (
            f"saved[0] >= target check (pos={check_pos}) must come BEFORE "
            f"encode_image call (pos={encode_pos}) in _flush"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 13: download() try/finally 保证资源释放
# ──────────────────────────────────────────────────────────────────────────────

class TestFix13DownloadTryFinally:
    """验证 download() 异常时仍调用 close()。"""

    def test_dedup_closed_on_exception(self, tmp_path):
        """download() 抛异常时 _dedup.close() 仍被调用。"""
        from smart_spider.smart_spider import SmartSpider

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider.keywords = ["test"]
        spider.output_dir = str(tmp_path)
        spider.media_types = {"text"}   # 最简单的模态
        spider.max_items = 1
        spider.max_workers = 1
        spider.search_engines = []      # 无引擎 -> executor 立即完成
        spider._pbar_lock = threading.Lock()
        spider._renderer = None

        dedup_mock = MagicMock()
        spider._dedup = dedup_mock

        # 让 for keyword 循环内部抛异常（模拟意外崩溃）
        with patch(
            "smart_spider.smart_spider._ModalDoneEvents",
            side_effect=RuntimeError("simulated crash"),
        ):
            with pytest.raises(RuntimeError, match="simulated crash"):
                spider.download()

        # 即使异常，dedup.close() 必须被调用
        dedup_mock.close.assert_called_once()

    def test_renderer_closed_on_exception(self, tmp_path):
        """download() 抛异常时 _renderer.close() 仍被调用。"""
        from smart_spider.smart_spider import SmartSpider

        spider = object.__new__(SmartSpider)
        SmartSpider._ensure_stub_attrs(spider)
        spider.keywords = ["test"]
        spider.output_dir = str(tmp_path)
        spider.media_types = {"text"}
        spider.max_items = 1
        spider.max_workers = 1
        spider.search_engines = []
        spider._pbar_lock = threading.Lock()

        renderer_mock = MagicMock()
        spider._renderer = renderer_mock
        spider._dedup = MagicMock()

        with patch(
            "smart_spider.smart_spider._ModalDoneEvents",
            side_effect=RuntimeError("simulated crash"),
        ):
            with pytest.raises(RuntimeError):
                spider.download()

        renderer_mock.close.assert_called_once()

    def test_download_source_has_try_finally(self):
        """download() 源码应包含 try/finally 块。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider.download)
        assert "try:" in src and "finally:" in src, (
            "download() must use try/finally to guarantee resource cleanup"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 14: _infer_worker 达标后排空队列不积累 batch
# ──────────────────────────────────────────────────────────────────────────────

class TestFix14InferWorkerDrainAfterTarget:
    """验证达标后 items 不再被 append 到 batch（不积累内存）。"""

    def test_no_batch_accumulation_after_target(self, tmp_path):
        """达标后 model.encode_image 不应被额外调用（间接证明 batch 不积累）。"""
        from PIL import Image
        from smart_spider.smart_spider import _ModalDoneEvents, _SENTINEL

        spider, call_counts = _make_infer_spider()
        spider.batch_size = 2   # 小 batch，容易触发积累
        save_dir = str(tmp_path / "drain_test")
        os.makedirs(save_dir, exist_ok=True)

        target = 1   # 只需 1 张
        done_ev = threading.Event()
        tracker = _ModalDoneEvents(["image"])
        pbar = MagicMock()

        png = _make_png_bytes()
        infer_q: queue.Queue = queue.Queue()

        # 放 10 张（target=1，达标后 9 张应被丢弃）
        for i in range(10):
            item = (Image.open(BytesIO(png)).convert("RGB"),
                    "dog", f"http://x/{i}.png", {}, png)
            infer_q.put(item)
        infer_q.put(_SENTINEL)

        t = threading.Thread(
            target=spider._infer_worker,
            args=(infer_q, pbar, save_dir, done_ev, target, tracker),
            daemon=True,
        )
        t.start()
        t.join(timeout=15)

        # 只应保存 1 张
        saved_files = [f for f in os.listdir(save_dir) if f.endswith(".png")]
        assert len(saved_files) == 1

        # encode 应只被调用一次（第一个 batch=2 张时）
        # 后续 batch 要么被 _flush 的 saved>=target 快速返回，
        # 要么根本没有积累（fix 14 核心）
        assert call_counts["encode"] == 1, (
            f"encode_image called {call_counts['encode']} times; "
            "should be 1 (first batch only)"
        )

    def test_source_drains_queue_without_accumulation(self):
        """验证 _infer_worker while 循环里有 saved[0] >= target -> continue 的排空逻辑。"""
        import inspect
        from smart_spider.smart_spider import SmartSpider
        src = inspect.getsource(SmartSpider._infer_worker)
        # 找 while True 循环部分（在 _flush 定义之后）
        while_part = src[src.rfind("while True"):]
        assert "saved[0] >= target" in while_part and "continue" in while_part, (
            "_infer_worker while loop should have a 'saved[0] >= target: continue' drain path"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Fix 15: ProxyPool round_robin 不跳步
# ──────────────────────────────────────────────────────────────────────────────

class TestFix15ProxyPoolRoundRobin:
    """验证 ProxyPool round_robin 在代理被隔离后仍均匀轮询。"""

    def _make_pool(self, urls):
        from smart_spider.http_client import ProxyPool
        return ProxyPool(urls, max_fail=2)

    def test_full_cycle_visits_all_proxies(self):
        """3 个代理，3 次 get() 应各不相同（完整轮询一圈）。"""
        pool = self._make_pool(["http://a:1", "http://b:2", "http://c:3"])
        visited = [pool.get().url for _ in range(3)]
        assert set(visited) == {"http://a:1", "http://b:2", "http://c:3"}, (
            f"Round-robin should visit all proxies in one cycle, got {visited}"
        )

    def test_round_robin_wraps_correctly(self):
        """6 次 get() 应循环两圈，每个代理恰好被选 2 次。"""
        pool = self._make_pool(["http://a:1", "http://b:2", "http://c:3"])
        counts: dict = {}
        for _ in range(6):
            url = pool.get().url
            counts[url] = counts.get(url, 0) + 1
        for url in ["http://a:1", "http://b:2", "http://c:3"]:
            assert counts[url] == 2, f"{url} selected {counts[url]} times, expected 2"

    def test_no_skip_after_proxy_isolated(self):
        """一个代理被隔离后，其余代理不应有跳步现象。"""
        from smart_spider.http_client import ProxyPool
        pool = self._make_pool(["http://a:1", "http://b:2", "http://c:3"])

        # 先消费几次让 _idx 推进到 2
        pool.get()  # a
        pool.get()  # b
        # 现在 _idx=2（指向 c）

        # 隔离 c（通过 report_fail 使其进入 OPEN 状态）
        c_entry = next(e for e in pool._entries if e.url == "http://c:3")
        pool.report_fail(c_entry)
        pool.report_fail(c_entry)  # max_fail=2 -> cb_state becomes "open"

        # 接下来的轮询应只在 a, b 之间进行，不跳步
        visited = [pool.get().url for _ in range(4)]
        for url in visited:
            assert url in ("http://a:1", "http://b:2"), (
                f"After isolating c, only a and b should be returned, got {url}"
            )
        # a 和 b 各应被选 2 次（2 圈）
        assert visited.count("http://a:1") == 2
        assert visited.count("http://b:2") == 2

    def test_idx_wraps_in_source(self):
        """验证 ProxyPool.get 源码中 _idx 用取模回绕（不只是 _idx += 1）。"""
        import inspect
        from smart_spider.http_client import ProxyPool
        src = inspect.getsource(ProxyPool.get)
        # 新实现: _idx = (_idx + 1) % len(active)
        assert "% len(active)" in src, (
            "ProxyPool.get should wrap _idx with % len(active) to avoid skip"
        )

    def test_single_proxy_pool(self):
        """单代理池始终返回同一个代理。"""
        pool = self._make_pool(["http://only:9999"])
        for _ in range(5):
            assert pool.get().url == "http://only:9999"


# ──────────────────────────────────────────────────────────────────────────────
# 回归
# ──────────────────────────────────────────────────────────────────────────────

class TestRegressionRound6:
    def test_all_modules_import_cleanly(self):
        import smart_spider.smart_spider
        import smart_spider.http_client
        import smart_spider.engines
        import smart_spider.browser

    def test_video_downloader_retry_still_works(self, tmp_path):
        """Fix 10 的重试逻辑不应被本轮修改破坏。"""
        from smart_spider.smart_spider import VideoDownloader
        dl = VideoDownloader(output_dir=str(tmp_path), max_retries=1)
        fail = MagicMock(returncode=1, stderr="err")
        ok = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", side_effect=[fail, ok]):
            assert dl.download("http://x.com/v", str(tmp_path)) is True

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
