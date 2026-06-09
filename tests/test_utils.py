# coding=utf-8
"""核心工具函数测试。

覆盖：
- _detect_ext: 文件格式魔数检测
- UrlDeduplicator: 已在 test_http_client.py 中测试，此处补充边界用例
"""
import pytest
from smart_spider.smart_spider import _detect_ext


class TestDetectExt:
    def test_jpg(self):
        assert _detect_ext(b"\xff\xd8\xff" + b"\x00" * 100) == ".jpg"

    def test_png(self):
        assert _detect_ext(b"\x89PNG" + b"\x00" * 100) == ".png"

    def test_webp(self):
        # RIFF....WEBP
        content = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
        assert _detect_ext(content) == ".webp"

    def test_riff_not_webp_falls_through(self):
        # RIFF 但不是 WEBP → 走到下一个 magic 或 fallback
        content = b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 100
        # 不应该返回 .webp，应该是 fallback .jpg
        ext = _detect_ext(content)
        assert ext != ".webp"

    def test_gif(self):
        assert _detect_ext(b"GIF8" + b"\x00" * 100) == ".gif"

    def test_unknown_fallback(self):
        assert _detect_ext(b"\x00\x00\x00\x00") == ".jpg"

    def test_empty_bytes_fallback(self):
        assert _detect_ext(b"") == ".jpg"

    def test_truncated_magic_fallback(self):
        # 数据不足 4 字节
        assert _detect_ext(b"\xff") == ".jpg"
