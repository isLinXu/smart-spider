# coding=utf-8
"""任务级 crawl 计划：剩余配额、关键词续跑、站点装配、spider_tools 发现。"""
from __future__ import annotations

import os
from typing import Any, Optional, Sequence

from .dataset_discovery import DiscoveredImage


def remaining_quota(saved_count: int, total_count: int) -> int:
    """How many more samples the job still wants."""
    return int(total_count) - int(saved_count)


def remaining_keywords(
    keywords: Sequence[str],
    done: Sequence[str],
) -> list[str]:
    """Keywords not yet marked done (membership matches the legacy list check)."""
    done_list = list(done)
    return [keyword for keyword in keywords if keyword not in done_list]


def keyword_progress_snapshot(
    *,
    keywords: Sequence[str],
    done: Sequence[str],
    saved_count: int,
    total_target: int,
) -> dict[str, Any]:
    """Payload for ``ProgressManager.save`` after one keyword finishes."""
    done_list = list(done)
    return {
        "saved_count": int(saved_count),
        "total_target": int(total_target),
        "keywords_done": done_list,
        "keywords_remaining": remaining_keywords(keywords, done_list),
    }


def site_crawler_kwargs(
    *,
    parser_name: str,
    output_dir: str,
    start_page: int,
    end_page: int,
    max_workers: int,
    min_width: int,
    min_height: int,
    timeout: int,
    connect_timeout: Optional[float],
    read_timeout: Optional[float],
    max_image_bytes: int,
    max_image_pixels: int,
) -> dict[str, Any]:
    """Keyword arguments for ``SiteCrawler`` besides ``site_parser``."""
    return {
        "output_dir": os.path.join(output_dir, f"_site_tmp_{parser_name}"),
        "start_page": start_page,
        "end_page": end_page,
        "max_workers": max_workers,
        "min_width": min_width,
        "min_height": min_height,
        "timeout": timeout,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
        "max_image_bytes": max_image_bytes,
        "max_image_pixels": max_image_pixels,
    }


def spider_tools_to_discovered(
    urls: Sequence[Any],
    *,
    default_keyword: str,
) -> list[DiscoveredImage]:
    """Map spider_tools URL objects onto the shared discovered-image shape."""
    found: list[DiscoveredImage] = []
    fallback = str(default_keyword or "")
    for item in urls:
        tags = getattr(item, "tags", None) or ()
        keyword = str(tags[0]) if tags else fallback
        found.append(
            DiscoveredImage(
                url=str(getattr(item, "url", "") or ""),
                keyword=keyword,
                source=f"st:{getattr(item, 'site', '')}",
            )
        )
    return found
