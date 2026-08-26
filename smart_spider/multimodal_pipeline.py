# coding=utf-8
"""第二阶段多模态流水线：页面抽取、来源路由和标注后端编排。

本模块不直接创建浏览器或调用云端模型，所有外部能力通过 callable/protocol
注入。这样静态 HTTP、Browser Use、本地模型和云模型可以独立测试、替换和限额。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol
from urllib.parse import urljoin

from .dataset_contracts import (
    CandidateResource,
    LabelDecision,
    LabelPolicy,
    LabelResolution,
    Modality,
    ModalityAsset,
    ModalityRelation,
    QualityMetrics,
    SampleRecord,
    normalize_url,
    utc_now,
)


def clean_text(value: str, max_chars: int = 120_000) -> str:
    """清理 HTML 文本中的空白，并限制单条样本大小。"""
    value = value or ""
    lines = []
    for line in re.split(r"\n+", value):
        line = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)[:max_chars].strip()


class _PageHTMLParser(HTMLParser):
    """不依赖第三方库的页面正文、标题和图片候选解析器。"""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}
    _BLOCK_TAGS = {
        "article", "br", "div", "h1", "h2", "h3", "h4", "li", "p",
        "section", "tr", "header", "footer",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.images: list[dict[str, str]] = []
        self.meta: dict[str, str] = {}
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        tag = tag.lower()
        attrs_map = {key.lower(): (value or "") for key, value in attrs}
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")
        if tag == "img":
            src = (
                attrs_map.get("src")
                or attrs_map.get("data-src")
                or attrs_map.get("data-original")
                or attrs_map.get("data-lazy-src")
            )
            if not src and attrs_map.get("srcset"):
                src = attrs_map["srcset"].split(",")[0].strip().split(" ")[0]
            if src and not src.startswith("data:"):
                self.images.append({"src": src, "alt": attrs_map.get("alt", "")})
        if tag == "meta":
            key = attrs_map.get("name") or attrs_map.get("property")
            value = attrs_map.get("content", "")
            if key and value:
                self.meta[key.lower()] = value.strip()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if not self._skip_depth and tag == "title":
            self._in_title = False
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth or not data.strip():
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


class PageSampleExtractor:
    """把静态或浏览器得到的 HTML 转成图文多模态样本。"""

    def __init__(
        self,
        min_text_chars: int = 20,
        max_text_chars: int = 120_000,
        max_images: int = 50,
        include_page_asset: bool = True,
    ):
        self.min_text_chars = min_text_chars
        self.max_text_chars = max_text_chars
        self.max_images = max_images
        self.include_page_asset = include_page_asset

    def extract_page(
        self,
        url: str,
        html: str,
        source: str = "static_html",
        query: str = "",
        task_type: Optional[str] = None,
    ) -> SampleRecord:
        parser = _PageHTMLParser()
        parser.feed(html or "")
        parser.close()

        title = clean_text(" ".join(parser.title_parts), max_chars=500)
        title = title or parser.meta.get("og:title", "")
        text = clean_text("\n".join(parser.text_parts), self.max_text_chars)
        if len(text) < self.min_text_chars:
            text = ""

        image_candidates = []
        seen_images = set()
        for item in parser.images:
            image_url = urljoin(url, item["src"])
            key = normalize_url(image_url)
            if not key or key in seen_images:
                continue
            seen_images.add(key)
            image_candidates.append({"url": image_url, "alt": clean_text(item.get("alt", ""), 500)})
            if len(image_candidates) >= self.max_images:
                break

        page_key = hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()[:16]
        assets: list[ModalityAsset] = []
        relations: list[ModalityRelation] = []
        page_asset_id = f"page-{page_key}"
        if self.include_page_asset:
            assets.append(ModalityAsset(
                modality=Modality.WEBPAGE,
                role="source",
                uri=url,
                asset_id=page_asset_id,
                metadata={"title": title, "source": source},
            ))

        text_asset_id = ""
        if text:
            text_asset_id = f"text-{page_key}"
            assets.append(ModalityAsset(
                modality=Modality.TEXT,
                role="context",
                text=text,
                asset_id=text_asset_id,
                metadata={"title": title, "language": parser.meta.get("content-language", "")},
            ))
            if self.include_page_asset:
                relations.append(ModalityRelation(
                    source=page_asset_id,
                    target=text_asset_id,
                    relation="contains",
                    evidence={"extractor": "html"},
                ))

        for index, item in enumerate(image_candidates, start=1):
            image_id = f"image-{page_key}-{index}"
            assets.append(ModalityAsset(
                modality=Modality.IMAGE,
                role="image",
                uri=item["url"],
                asset_id=image_id,
                metadata={"alt": item["alt"]},
            ))
            if self.include_page_asset:
                relations.append(ModalityRelation(
                    source=page_asset_id,
                    target=image_id,
                    relation="contains",
                    evidence={"extractor": "html"},
                ))
            if text_asset_id:
                relations.append(ModalityRelation(
                    source=text_asset_id,
                    target=image_id,
                    relation="caption-of" if item["alt"] else "context-of",
                    evidence={"alt": item["alt"]} if item["alt"] else {},
                ))

        asset_fingerprint = "\n".join(
            f"{asset.modality.value}:{asset.uri}:{asset.text[:200]}" for asset in assets
        )
        sample_id = "sha256:" + hashlib.sha256(
            f"{normalize_url(url)}\n{text}\n{asset_fingerprint}".encode("utf-8")
        ).hexdigest()
        has_text = bool(text_asset_id)
        has_image = bool(image_candidates)
        resolved_task_type = task_type or (
            "image_text_alignment" if has_text and has_image
            else "text_document" if has_text
            else "image_collection" if has_image
            else "webpage_context"
        )

        return SampleRecord(
            sample_id=sample_id,
            file=image_candidates[0]["url"] if image_candidates else "",
            quality=QualityMetrics(
                modality="multimodal",
                validated=bool(text or image_candidates or self.include_page_asset),
                attributes={
                    "text_chars": len(text),
                    "image_count": len(image_candidates),
                    "title": title,
                },
            ),
            provenance={
                "source": source,
                "query": query,
                "url": url,
                "title": title,
                "captured_at": utc_now(),
            },
            pipeline={"extractor": "PageSampleExtractor", "status": "extracted"},
            modalities=assets,
            relations=relations,
            task_type=resolved_task_type,
        )

    def discover_candidates(
        self,
        url: str,
        html: str,
        source: str = "static_html",
        query: str = "",
    ) -> list[CandidateResource]:
        """从页面中产出可交给 Fetch Worker 的文本/图片候选。"""
        sample = self.extract_page(url, html, source=source, query=query)
        return self.candidates_from_sample(sample, source=source, query=query)

    @staticmethod
    def candidates_from_sample(
        sample: SampleRecord,
        source: str = "static_html",
        query: str = "",
    ) -> list[CandidateResource]:
        """从已生成的样本提取候选，避免重复解析 HTML。"""
        candidates = []
        for asset in sample.modalities:
            if asset.modality not in {Modality.IMAGE, Modality.TEXT} or not asset.uri:
                continue
            candidates.append(CandidateResource(
                url=asset.uri,
                source=source,
                modality=asset.modality,
                query=query,
                title=sample.provenance.get("title", ""),
                alt=asset.metadata.get("alt", ""),
                source_meta={"sample_id": sample.sample_id, "role": asset.role},
            ))
        return candidates


class RouteAction(str, Enum):
    STATIC = "static"
    BROWSER = "browser"


@dataclass
class SourceResponse:
    """来源适配器的统一返回值。"""

    candidates: list[CandidateResource] = field(default_factory=list)
    status_code: int = 200
    html_length: int = 0
    blocked: bool = False
    dynamic: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    sample: Optional[SampleRecord] = None


@dataclass
class RouteDecision:
    action: RouteAction
    reason: str
    score: float = 0.0


@dataclass
class DiscoveryTask:
    query: str = ""
    start_url: str = ""
    source: str = ""
    modalities: tuple[Modality, ...] = (Modality.TEXT, Modality.IMAGE)
    min_candidates: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    candidates: list[CandidateResource]
    decision: RouteDecision
    static_response: SourceResponse
    browser_response: Optional[SourceResponse] = None


class SourceCallable(Protocol):
    def __call__(self, task: DiscoveryTask) -> SourceResponse:
        ...


class StaticPageSource:
    """使用 SmartHttpClient 获取 HTML 并产出页面候选。"""

    def __init__(self, http_client: Any, extractor: Optional[PageSampleExtractor] = None):
        self.http_client = http_client
        self.extractor = extractor or PageSampleExtractor()

    def __call__(self, task: DiscoveryTask) -> SourceResponse:
        if not task.start_url:
            return SourceResponse(error="missing_start_url")
        response = self.http_client.get(task.start_url)
        try:
            html = getattr(response, "text", "") or ""
            sample = self.extractor.extract_page(
                task.start_url,
                html,
                source=task.source or "static_html",
                query=task.query,
            )
            candidates = self.extractor.candidates_from_sample(
                sample,
                source=task.source or "static_html",
                query=task.query,
            )
            return SourceResponse(
                candidates=candidates,
                status_code=int(getattr(response, "status_code", 200) or 200),
                html_length=len(html),
                metadata={"url": task.start_url},
                sample=sample,
            )
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()


class BrowserPageSource:
    """把 BrowserController.navigate 或 Browser Use 自定义 fetch 注入来源路由。"""

    def __init__(self, fetch_html: Callable[[str], str], extractor: Optional[PageSampleExtractor] = None):
        self.fetch_html = fetch_html
        self.extractor = extractor or PageSampleExtractor()

    def __call__(self, task: DiscoveryTask) -> SourceResponse:
        if not task.start_url:
            return SourceResponse(error="missing_start_url")
        html = self.fetch_html(task.start_url) or ""
        sample = self.extractor.extract_page(
            task.start_url,
            html,
            source=task.source or "browser_use",
            query=task.query,
        )
        candidates = self.extractor.candidates_from_sample(
            sample,
            source=task.source or "browser_use",
            query=task.query,
        )
        return SourceResponse(
            candidates=candidates,
            html_length=len(html),
            dynamic=True,
            metadata={"url": task.start_url},
            sample=sample,
        )


class AdaptiveSourceRouter:
    """静态来源优先，按质量信号切换 Browser Use。"""

    def __init__(
        self,
        min_html_length: int = 200,
        fallback_on_empty: bool = True,
        blocked_statuses: tuple[int, ...] = (401, 403, 429, 503),
    ):
        self.min_html_length = min_html_length
        self.fallback_on_empty = fallback_on_empty
        self.blocked_statuses = blocked_statuses

    def decide(self, response: SourceResponse, min_candidates: int = 1) -> RouteDecision:
        if response.error:
            return RouteDecision(RouteAction.BROWSER, "static_error", 1.0)
        if response.blocked or response.status_code in self.blocked_statuses:
            return RouteDecision(RouteAction.BROWSER, "blocked_or_rate_limited", 1.0)
        if response.dynamic:
            return RouteDecision(RouteAction.BROWSER, "dynamic_page_signal", 0.9)
        if (
            self.fallback_on_empty
            and len(response.candidates) < min_candidates
            and response.html_length < self.min_html_length
        ):
            return RouteDecision(RouteAction.BROWSER, "insufficient_static_content", 0.75)
        return RouteDecision(RouteAction.STATIC, "static_quality_sufficient", 0.0)

    def discover(
        self,
        task: DiscoveryTask,
        static_source: SourceCallable,
        browser_source: Optional[SourceCallable] = None,
    ) -> DiscoveryResult:
        try:
            static_response = static_source(task)
        except Exception as exc:
            static_response = SourceResponse(error=str(exc))
        decision = self.decide(static_response, task.min_candidates)
        browser_response = None
        candidates = list(static_response.candidates)

        if decision.action == RouteAction.BROWSER and browser_source is not None:
            try:
                browser_response = browser_source(task)
            except Exception as exc:
                browser_response = SourceResponse(error=str(exc))
            candidates.extend(browser_response.candidates)

        unique: dict[str, CandidateResource] = {}
        for candidate in candidates:
            if not candidate.url or candidate.modality not in task.modalities:
                continue
            unique.setdefault(candidate.candidate_id, candidate)
        return DiscoveryResult(
            candidates=list(unique.values()),
            decision=decision,
            static_response=static_response,
            browser_response=browser_response,
        )


class AnnotationBackend(Protocol):
    def annotate(self, sample: SampleRecord) -> Iterable[LabelDecision]:
        ...


class BatchAnnotationBackend(Protocol):
    """可选的批量标注后端接口。

    返回值按 ``sample_id`` 对应决策列表；后端也可以在单样本场景返回
    决策列表，路由器会兼容这种简化实现。
    """

    def annotate_batch(
        self, samples: list[SampleRecord]
    ) -> Mapping[str, Iterable[LabelDecision]]:
        ...


@dataclass
class AnnotationResult:
    resolution: LabelResolution
    used_backends: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


class AnnotationRouter:
    """本地、云端和规则标注后端的可插拔路由器。"""

    def __init__(self, policy: Optional[LabelPolicy] = None, backends: Optional[dict[str, AnnotationBackend]] = None):
        self.policy = policy or LabelPolicy()
        self.backends = backends or {}

    def annotate(self, sample: SampleRecord, backend_order: Optional[list[str]] = None) -> AnnotationResult:
        batch = self.annotate_batch([sample], backend_order=backend_order)
        return batch[sample.sample_id]

    @staticmethod
    def _normalize_decisions(
        decisions: Iterable[LabelDecision], source: str
    ) -> list[LabelDecision]:
        normalized = []
        for decision in decisions or []:
            if isinstance(decision, dict):
                decision = LabelDecision.from_dict(decision)
            if not isinstance(decision, LabelDecision):
                raise TypeError("annotation backend must return LabelDecision objects")
            if not decision.source or decision.source == "unknown":
                decision = LabelDecision(
                    name=decision.name,
                    score=decision.score,
                    source=source,
                    evidence=dict(decision.evidence),
                )
            normalized.append(decision)
        return normalized

    @staticmethod
    def _normalize_batch_result(
        result: Any, samples: list[SampleRecord]
    ) -> dict[str, Iterable[LabelDecision]]:
        if isinstance(result, Mapping):
            return {
                sample.sample_id: result.get(sample.sample_id, [])
                for sample in samples
            }
        if len(samples) == 1:
            return {samples[0].sample_id: result or []}
        if isinstance(result, (list, tuple)) and len(result) == len(samples):
            return {
                sample.sample_id: decisions or []
                for sample, decisions in zip(samples, result)
            }
        raise TypeError(
            "batch annotation must return a mapping, or one decision list per sample"
        )

    def annotate_batch(
        self,
        samples: Iterable[SampleRecord],
        backend_order: Optional[list[str]] = None,
    ) -> dict[str, AnnotationResult]:
        """批量执行标注；没有批量后端时自动逐样本降级。"""
        samples = list(samples)
        names = backend_order or list(self.backends)
        decisions: dict[str, list[LabelDecision]] = {
            sample.sample_id: [] for sample in samples
        }
        used: dict[str, list[str]] = {sample.sample_id: [] for sample in samples}
        errors: dict[str, dict[str, str]] = {
            sample.sample_id: {} for sample in samples
        }
        for name in names:
            backend = self.backends.get(name)
            if backend is None:
                for sample in samples:
                    errors[sample.sample_id][name] = "backend_not_configured"
                continue
            batch_method = getattr(backend, "annotate_batch", None)
            try:
                if callable(batch_method):
                    results = self._normalize_batch_result(
                        batch_method(samples), samples
                    )
                    for sample in samples:
                        decisions[sample.sample_id].extend(
                            self._normalize_decisions(results[sample.sample_id], name)
                        )
                        used[sample.sample_id].append(name)
                else:
                    for sample in samples:
                        result = backend.annotate(sample)
                        decisions[sample.sample_id].extend(
                            self._normalize_decisions(result, name)
                        )
                        used[sample.sample_id].append(name)
            except Exception as exc:
                for sample in samples:
                    errors[sample.sample_id][name] = str(exc)
        return {
            sample.sample_id: AnnotationResult(
                resolution=self.policy.resolve(decisions[sample.sample_id]),
                used_backends=used[sample.sample_id],
                errors=errors[sample.sample_id],
            )
            for sample in samples
        }
