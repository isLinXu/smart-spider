# coding=utf-8
"""搜索页发现：引擎排序、候选窗口截断、页偏移、停采判断与页窗口调度。"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class DiscoveredImage:
    """A single image URL found by a search page or site parser."""

    url: str
    keyword: str
    source: str
    meta: dict[str, Any] = field(default_factory=dict)


def order_search_engines(
    names: Sequence[str],
    source_metrics: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[str]:
    """Prioritize engines with observed page success and candidate yield."""
    ordered = list(names)
    snapshot = dict(source_metrics or {})
    if not snapshot:
        return ordered
    position = {name: index for index, name in enumerate(ordered)}

    def key(name: str) -> tuple[int, float, float, int]:
        row = snapshot.get(name)
        if not row or not row.get("attempts"):
            return (1, 0.0, 0.0, position[name])
        attempts = max(int(row["attempts"]), 1)
        yield_per_attempt = float(row.get("candidates", 0)) / attempts
        return (
            0,
            -float(row.get("success_rate", 0.0)),
            -yield_per_attempt,
            position[name],
        )

    return sorted(ordered, key=key)


def bound_page_items(items: Sequence[Any], max_pending: int) -> list[Any]:
    """Keep one bounded candidate window from a malformed/huge search page."""
    if max_pending <= 0:
        raise ValueError("max_pending must be positive")
    if len(items) > max_pending:
        return list(items[:max_pending])
    return list(items)


def page_offsets(page_step: int, pages_needed: int) -> Iterable[int]:
    step = max(int(page_step), 1)
    pages = max(int(pages_needed), 1)
    return range(0, pages * step, step)


def items_to_discovered(
    items: Sequence[Mapping[str, Any]],
    *,
    keyword: str,
    source: str,
) -> list[DiscoveredImage]:
    found: list[DiscoveredImage] = []
    for item in items:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        found.append(
            DiscoveredImage(url=url, keyword=keyword, source=source, meta=dict(meta))
        )
    return found


def urls_to_discovered(
    urls: Iterable[Any],
    *,
    keyword: str,
    source: str,
) -> list[DiscoveredImage]:
    """Wrap raw image URLs as ``DiscoveredImage`` rows."""
    return items_to_discovered(
        [{"url": url} for url in urls],
        keyword=keyword,
        source=source,
    )


def collection_limit_reached(
    *,
    stopped: bool,
    saved_count: int,
    total_count: int,
    scene_target_reached: bool = False,
) -> bool:
    """Stop discovery/download when shutdown, quota, or scene cap is hit."""
    return bool(stopped) or saved_count >= total_count or bool(scene_target_reached)


def estimate_pages_needed(
    remaining: int,
    page_step: int,
    *,
    cap: int = 50,
) -> int:
    """Pages to fetch for the remaining quota (legacy: remaining//step + 2, capped)."""
    pages = max(1, int(remaining) // max(int(page_step), 1) + 2)
    return min(pages, int(cap))


def discover_page_images(
    items: Sequence[Mapping[str, Any]],
    *,
    keyword: str,
    source: str,
    max_pending: int,
) -> tuple[int, list[DiscoveredImage]]:
    """Bound one search page and return ``(raw_count, discovered)``."""
    return len(items), items_to_discovered(
        bound_page_items(items, max_pending),
        keyword=keyword,
        source=source,
    )


def drain_inflight_window(
    spawn: Callable[[], Any],
    *,
    window: int,
    on_error: Callable[[BaseException], None],
    on_inflight: Callable[[int], None],
) -> None:
    """Keep up to ``window`` spawned futures in flight until ``spawn`` returns None."""
    if window <= 0:
        raise ValueError("window must be positive")
    futures: set[Any] = set()

    def fill() -> None:
        while len(futures) < window:
            future = spawn()
            if future is None:
                return
            futures.add(future)
            on_inflight(len(futures))

    fill()
    while futures:
        done, _ = wait(futures, return_when=FIRST_COMPLETED)
        futures.difference_update(done)
        for future in done:
            try:
                future.result()
            except Exception as exc:
                on_error(exc)
        fill()
        on_inflight(len(futures))
