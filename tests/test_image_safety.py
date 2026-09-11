# coding=utf-8
"""Image header probe and decode-budget tests."""
from io import BytesIO

import pytest
from PIL import Image

from smart_spider.image_safety import (
    UnsafeImageError,
    assert_header_within_budget,
    decode_image_bytes,
    probe_image_header,
    sha256_of_bytes,
    write_bytes_with_sha256,
)


def _encode(size, fmt="JPEG", color=(10, 20, 30)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,size", [("JPEG", (123, 45)), ("PNG", (80, 40)), ("GIF", (64, 32)), ("WEBP", (50, 25))])
def test_probe_image_header_reads_dimensions(fmt, size):
    payload = _encode(size, fmt=fmt)
    probe = probe_image_header(payload)
    assert probe.width == size[0]
    assert probe.height == size[1]
    assert probe.pixels == size[0] * size[1]


def test_assert_header_rejects_oversized_before_decode():
    payload = _encode((200, 200), fmt="PNG")
    with pytest.raises(UnsafeImageError, match="header declares"):
        assert_header_within_budget(payload, max_pixels=1000)


def test_decode_image_bytes_still_returns_rgb():
    payload = _encode((32, 24), fmt="JPEG")
    image = decode_image_bytes(payload, max_pixels=10_000)
    assert image.size == (32, 24)
    assert image.mode == "RGB"


def test_write_bytes_with_sha256_matches_digest(tmp_path):
    payload = b"abc123"
    path = tmp_path / "blob.bin"
    digest = write_bytes_with_sha256(str(path), payload, fsync=True)
    assert path.read_bytes() == payload
    assert digest == sha256_of_bytes(payload)
