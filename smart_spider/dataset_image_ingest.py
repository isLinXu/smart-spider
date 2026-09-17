# coding=utf-8
"""有界图片拉取、解码与落盘格式转换（与 CLIP/配额/仓储解耦）。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable, Optional

import numpy as np
from loguru import logger
from PIL import Image

from .image_safety import UnsafeImageError, assert_header_within_budget, decode_image_bytes
from .smart_spider import _detect_ext

ALLOWED_IMAGE_EXTS = (".jpg", ".png", ".webp", ".gif", ".avif", ".avis")


@dataclass
class ImageIngestResult:
    """Fetch/decode outcome before CLIP, scene gate, or repository commit."""

    status: str  # ok | reject | fail
    reason: str = ""
    inc_filtered: bool = False
    inc_failed: bool = False
    content: bytes = b""
    content_hash: str = ""
    image: Optional[Image.Image] = None
    ext: str = ""
    thumbnail: Any = None
    response: Any = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def prepare_output_image(
    image: Image.Image,
    source_content: bytes,
    source_ext: str,
    *,
    output_format: Optional[str],
    jpeg_quality: int,
) -> tuple[bytes, str, str, dict[str, Any]]:
    """Generate on-disk bytes after the candidate has passed filters.

    Content dedup still uses the downloaded source bytes. Conversion only
    affects the dataset artifact, so WebP/GIF sources default to JPEG.
    """
    source_format = (image.format or source_ext.lstrip(".") or "unknown").lower()
    if output_format is None:
        return source_content, source_ext, source_format, {
            "enabled": False,
            "from_format": source_format,
            "to_format": source_format,
        }

    output = BytesIO()
    image.save(
        output,
        format="JPEG",
        quality=jpeg_quality,
        optimize=True,
    )
    return output.getvalue(), ".jpg", "jpeg", {
        "enabled": True,
        "from_format": source_format,
        "to_format": "jpeg",
        "quality": jpeg_quality,
    }


def ingest_remote_image(
    http: Any,
    url: str,
    *,
    max_file_size: int,
    max_image_pixels: int,
    min_file_size: int,
    min_width: int,
    min_height: int,
    min_variance: float,
    detect_ext: Optional[Callable[[bytes], str]] = None,
    peek_bytes: int = 8192,
) -> ImageIngestResult:
    """Stream one image, enforce size/pixel/variance budgets, and decode it.

    The HTTP response stays open for the caller to close. Network errors from
    ``get_stream`` propagate so the crawler can count them as download failures.
    """
    detect = detect_ext or _detect_ext
    peek, resp = http.get_stream(url, peek_bytes=peek_bytes)
    ext = detect(peek)
    if ext not in ALLOWED_IMAGE_EXTS:
        return ImageIngestResult(
            status="reject",
            reason="unsupported_image_format",
            response=resp,
        )

    response_headers = getattr(resp, "headers", {}) or {}
    content_length = response_headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_file_size:
                return ImageIngestResult(
                    status="reject",
                    reason="file_too_large",
                    inc_filtered=True,
                    response=resp,
                )
        except (TypeError, ValueError):
            pass

    try:
        hasher = hashlib.sha256()
        hasher.update(peek)
        try:
            assert_header_within_budget(peek, max_pixels=max_image_pixels)
        except UnsafeImageError:
            return ImageIngestResult(
                status="reject",
                reason="image_too_large_pixels",
                inc_failed=True,
                response=resp,
            )
        chunks = [peek]
        total_bytes = len(peek)
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total_bytes += len(chunk)
            if total_bytes > max_file_size:
                return ImageIngestResult(
                    status="reject",
                    reason="file_too_large",
                    inc_filtered=True,
                    response=resp,
                )
            hasher.update(chunk)
            chunks.append(chunk)
            if total_bytes < 65536 or len(chunks) == 2:
                try:
                    assert_header_within_budget(
                        b"".join(chunks), max_pixels=max_image_pixels
                    )
                except UnsafeImageError:
                    return ImageIngestResult(
                        status="reject",
                        reason="image_too_large_pixels",
                        inc_failed=True,
                        response=resp,
                    )
        full_content = b"".join(chunks)
        source_content_hash = hasher.hexdigest()
    except Exception as read_err:
        logger.warning(f"Image body read failed: {url[:55]}: {read_err}")
        return ImageIngestResult(
            status="fail",
            reason=f"read_error: {read_err}",
            response=resp,
        )

    if len(full_content) < min_file_size:
        return ImageIngestResult(
            status="reject",
            reason="file_too_small",
            inc_filtered=True,
            content=full_content,
            content_hash=source_content_hash,
            ext=ext,
            response=resp,
        )

    try:
        img = decode_image_bytes(full_content, max_pixels=max_image_pixels)
    except UnsafeImageError:
        logger.debug(f"Image verification failed: {url[:55]}")
        return ImageIngestResult(
            status="reject",
            reason="invalid_image",
            inc_failed=True,
            content=full_content,
            content_hash=source_content_hash,
            ext=ext,
            response=resp,
        )

    if img.width < min_width or img.height < min_height:
        return ImageIngestResult(
            status="reject",
            reason="image_too_small",
            inc_filtered=True,
            content=full_content,
            content_hash=source_content_hash,
            image=img,
            ext=ext,
            response=resp,
        )

    thumbnail = np.array(img.resize((32, 32)))
    if np.std(thumbnail) < min_variance:
        return ImageIngestResult(
            status="reject",
            reason="low_variance",
            inc_filtered=True,
            content=full_content,
            content_hash=source_content_hash,
            image=img,
            ext=ext,
            thumbnail=thumbnail,
            response=resp,
        )

    return ImageIngestResult(
        status="ok",
        content=full_content,
        content_hash=source_content_hash,
        image=img,
        ext=ext,
        thumbnail=thumbnail,
        response=resp,
    )
