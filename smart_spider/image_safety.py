# coding=utf-8
"""Shared image decoding limits and cheap header probes for download paths."""
from __future__ import annotations

import hashlib
import struct
import threading
import warnings
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO, Iterable, Optional, Union

from PIL import Image


class UnsafeImageError(ValueError):
    """Raised for invalid images or images exceeding the decoded pixel budget."""


_PIL_LIMIT_LOCK = threading.RLock()


@dataclass(frozen=True)
class ImageHeaderProbe:
    """Cheap metadata extracted from image magic bytes without full decode."""

    format: Optional[str]
    width: Optional[int]
    height: Optional[int]

    @property
    def pixels(self) -> Optional[int]:
        if self.width is None or self.height is None:
            return None
        return int(self.width) * int(self.height)


def probe_image_header(content: bytes) -> ImageHeaderProbe:
    """Parse JPEG / PNG / GIF / WEBP dimensions from a prefix when possible.

    Returns an incomplete probe (``format``/size None) when the container is
    unknown or the prefix is truncated. Callers must still fully decode to
    verify pixel budgets for formats that cannot be probed.
    """
    data = bytes(content)
    if len(data) < 12:
        return ImageHeaderProbe(format=None, width=None, height=None)

    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return ImageHeaderProbe("PNG", int(width), int(height))

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return ImageHeaderProbe("GIF", int(width), int(height))

    if data[:2] == b"\xff\xd8":
        parsed = _jpeg_size(data)
        if parsed is not None:
            width, height = parsed
            return ImageHeaderProbe("JPEG", width, height)
        return ImageHeaderProbe("JPEG", None, None)

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        parsed = _webp_size(data)
        if parsed is not None:
            width, height = parsed
            return ImageHeaderProbe("WEBP", width, height)
        return ImageHeaderProbe("WEBP", None, None)

    return ImageHeaderProbe(format=None, width=None, height=None)


def assert_header_within_budget(
    content: bytes,
    *,
    max_pixels: int,
) -> ImageHeaderProbe:
    """Reject oversized images as soon as header dimensions are known."""
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    probe = probe_image_header(content)
    pixels = probe.pixels
    if pixels is not None and pixels > max_pixels:
        raise UnsafeImageError(
            f"image header declares {pixels} pixels; limit is {max_pixels}"
        )
    return probe


def sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(bytes(content)).hexdigest()


def hash_chunks(chunks: Iterable[bytes], *, hasher: Optional[Any] = None) -> str:
    digest = hasher or hashlib.sha256()
    for chunk in chunks:
        if chunk:
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_with_sha256(
    destination: Union[str, BinaryIO],
    content: bytes,
    *,
    fsync: bool = False,
) -> str:
    """Write bytes once while computing SHA-256 in the same pass."""
    import os

    data = bytes(content)
    digest = hashlib.sha256()
    digest.update(data)
    close = False
    if isinstance(destination, (str, bytes)):
        handle: BinaryIO = open(destination, "wb")
        close = True
    else:
        handle = destination
    try:
        handle.write(data)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
    finally:
        if close:
            handle.close()
    return digest.hexdigest()


def decode_image_bytes(content: bytes, *, max_pixels: int) -> Image.Image:
    """Verify and fully decode bytes while rejecting Pillow decompression bombs.

    A cheap header probe rejects obviously oversized JPEG/PNG/GIF/WEBP payloads
    before Pillow opens the stream. Pillow's pixel limit is process-global; the
    short critical section keeps concurrent download workers from changing one
    another's limit, and copies the decoded RGB image before restoring it.
    """
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise UnsafeImageError("image content must be bytes-like")
    payload = bytes(content)
    assert_header_within_budget(payload, max_pixels=max_pixels)
    with _PIL_LIMIT_LOCK:
        original_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = int(max_pixels)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(payload)) as probe:
                    probe.verify()
                with Image.open(BytesIO(payload)) as source:
                    width, height = source.size
                    if width * height > max_pixels:
                        raise UnsafeImageError(
                            f"image has {width * height} pixels; limit is {max_pixels}"
                        )
                    decoded = source.convert("RGB").copy()
                    decoded.format = source.format
                    return decoded
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise UnsafeImageError(f"image exceeds decoded pixel limit: {exc}") from exc
        except UnsafeImageError:
            raise
        except Exception as exc:
            raise UnsafeImageError(f"invalid image: {exc}") from exc
        finally:
            Image.MAX_IMAGE_PIXELS = original_limit


def _jpeg_size(data: bytes) -> Optional[tuple[int, int]]:
    index = 2
    length = len(data)
    while index + 9 <= length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > length:
            return None
        segment = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment < 2:
            return None
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            return width, height
        index += 2 + segment
    return None


def _webp_size(data: bytes) -> Optional[tuple[int, int]]:
    if len(data) < 30:
        return None
    kind = data[12:16]
    if kind == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if kind == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    return None
