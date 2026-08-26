# coding=utf-8
"""把现有搜索引擎和站点解析器接入多模态 Discovery 接口。"""
from __future__ import annotations

from typing import Any, Optional

from .dataset_contracts import CandidateResource, Modality
from .engines import ENGINE_REGISTRY, MediaType, RenderMode, get_engine
from .multimodal_pipeline import DiscoveryTask, SourceResponse
from .site_parser import get_site_parser


def _engine_modality(media_type: MediaType) -> Modality:
    return {
        MediaType.IMAGE: Modality.IMAGE,
        MediaType.TEXT: Modality.WEBPAGE,
        MediaType.VIDEO: Modality.VIDEO,
        MediaType.AUDIO: Modality.AUDIO,
    }.get(media_type, Modality.WEBPAGE)


class SearchDiscoverySource:
    """使用现有 SearchEngine 注册表发现图片、网页、视频候选。"""

    def __init__(self, http_client: Any, engines: Optional[list[str]] = None):
        self.http_client = http_client
        self.engines = engines

    def __call__(self, task: DiscoveryTask) -> SourceResponse:
        if not task.query:
            return SourceResponse(error="missing_query")
        names = self.engines or task.metadata.get("engines")
        if not names:
            names = list(ENGINE_REGISTRY)
        pages = max(1, int(task.metadata.get("pages", 1)))
        max_candidates = max(1, int(task.metadata.get("max_candidates", 1000)))
        candidates: list[CandidateResource] = []
        errors = []
        response_lengths: dict[str, int] = {}
        item_counts: dict[str, int] = {}
        dynamic = False

        for name in names:
            if len(candidates) >= max_candidates:
                break
            try:
                engine = get_engine(name)
            except KeyError as exc:
                errors.append(str(exc))
                continue
            modality = _engine_modality(engine.media_type)
            if modality not in task.modalities:
                continue
            dynamic = dynamic or engine.render_mode == RenderMode.DYNAMIC

            for page in range(pages):
                if len(candidates) >= max_candidates:
                    break
                offset = page * max(int(engine.page_step), 1)
                search_url = engine.build_search_url(task.query, offset)
                try:
                    html = self.http_client.get_text(search_url, engine=name)
                    items = engine.extract_items(html)
                    response_lengths[name] = max(response_lengths.get(name, 0), len(html or ""))
                    item_counts[name] = item_counts.get(name, 0) + len(items)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    continue
                for item in items:
                    item_url = item.get("url", "")
                    if not item_url:
                        continue
                    meta = dict(item.get("meta") or {})
                    candidates.append(CandidateResource(
                        url=item_url,
                        source=name,
                        modality=modality,
                        query=task.query,
                        page=page,
                        title=str(meta.get("title", "")),
                        alt=str(meta.get("alt", "")),
                        source_meta={
                            "engine": name,
                            "search_url": search_url,
                            "render_mode": engine.render_mode.value,
                            **meta,
                        },
                    ))

        return SourceResponse(
            candidates=candidates,
            dynamic=dynamic,
            html_length=max(response_lengths.values(), default=0),
            error=(
                "; ".join(errors)
                if errors and not candidates
                else "no_parseable_candidates" if not candidates
                else ""
            ),
            metadata={
                "engines": names,
                "query": task.query,
                "response_lengths": response_lengths,
                "item_counts": item_counts,
                "errors": errors,
            },
        )


class SiteDiscoverySource:
    """使用现有 SiteParser 发现网页详情页，正文和图片由后续页面 worker 处理。"""

    def __init__(
        self,
        http_client: Any,
        parser_name: str,
        start_page: int = 1,
        end_page: int = 5,
    ):
        self.http_client = http_client
        self.parser_name = parser_name
        self.start_page = start_page
        self.end_page = end_page

    def __call__(self, task: DiscoveryTask) -> SourceResponse:
        parser = get_site_parser(self.parser_name)
        candidates: list[CandidateResource] = []
        errors = []
        for page in range(self.start_page, self.end_page + 1):
            listing_url = parser.build_listing_url(page)
            try:
                html = self.http_client.get_text(listing_url)
                articles = parser.collect_pages(html)
            except Exception as exc:
                errors.append(f"page {page}: {exc}")
                continue
            for article in articles:
                url = article.get("url", "")
                if not url:
                    continue
                candidates.append(CandidateResource(
                    url=url,
                    source=f"site:{self.parser_name}",
                    modality=Modality.WEBPAGE,
                    query=task.query,
                    page=page,
                    title=str(article.get("title", "")),
                    source_meta={
                        "parser": self.parser_name,
                        "article_id": article.get("id", ""),
                        "listing_url": listing_url,
                    },
                ))
        return SourceResponse(
            candidates=candidates,
            error="; ".join(errors),
            metadata={"parser": self.parser_name},
        )
