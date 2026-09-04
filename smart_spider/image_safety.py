# coding=utf-8
"""Shared image decoding limits for every crawler download path."""
from __future__ import annotations

import threading
import warnings
from io import BytesIO

from PIL import Image


class UnsafeImageError(ValueError):
    """Raised for invalid images or images exceeding the decoded pixel budget."""


_PIL_LIMIT_LOCK = threading.RLock()


def decode_image_bytes(content: bytes, *, max_pixels: int) -> Image.Image:
    """Verify and fully decode bytes while rejecting Pillow decompression bombs.

    Pillow's pixel limit is process-global.  The short critical section keeps
    concurrent download workers from changing one another's limit, and copies
    the decoded RGB image before restoring the global setting.
    """
    if max_pixels <= 0:
        raise ValueError("max_pixels must be positive")
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise UnsafeImageError("image content must be bytes-like")
    with _PIL_LIMIT_LOCK:
        original_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = int(max_pixels)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(bytes(content))) as probe:
                    probe.verify()
                with Image.open(BytesIO(bytes(content))) as source:
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
