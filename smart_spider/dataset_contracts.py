# coding=utf-8
"""训练数据生产流水线的稳定数据契约。

这些对象是来源发现、下载验证、模型标注和 manifest 写入之间的边界。
它们只依赖标准库，便于后续在单进程、多进程和分布式 worker 中复用。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Optional
from urllib.parse import urldefrag, urlsplit, urlunsplit


def utc_now() -> str:
    """返回可排序的 UTC ISO-8601 时间字符串。"""
    return datetime.now(timezone.utc).isoformat()


def normalize_url(url: str) -> str:
    """规范化候选 URL，去掉片段和首尾空白。"""
    value = (url or "").strip()
    if not value:
        return ""
    value, _ = urldefrag(value)
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


class LabelMode(str, Enum):
    """标签生产模式。"""

    FIXED = "fixed"
    DISCOVERY = "discovery"
    HYBRID = "hybrid"


class Modality(str, Enum):
    """样本中可组合的模态。"""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    WEBPAGE = "webpage"


@dataclass
class CandidateResource:
    """发现阶段产出的统一候选资源。"""

    url: str
    source: str
    modality: Modality = Modality.IMAGE
    query: str = ""
    referer: str = ""
    page: int = 0
    title: str = ""
    alt: str = ""
    source_meta: dict[str, Any] = field(default_factory=dict)
    discovered_at: str = field(default_factory=utc_now)

    @property
    def candidate_id(self) -> str:
        return hashlib.sha256(normalize_url(self.url).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["modality"] = self.modality.value
        data["candidate_id"] = self.candidate_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateResource":
        fields = {
            "url", "source", "query", "referer", "page", "title", "alt",
            "source_meta", "discovered_at",
        }
        values = {key: data[key] for key in fields if key in data}
        values["modality"] = Modality(data.get("modality", Modality.IMAGE))
        return cls(**values)


@dataclass
class QualityMetrics:
    """任意模态的质量门禁结果，保留图像字段兼容旧任务。"""

    modality: str = "image"
    width: int = 0
    height: int = 0
    file_size: int = 0
    format: str = ""
    variance: Optional[float] = None
    blur_score: Optional[float] = None
    phash: str = ""
    validated: bool = False
    reasons: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "QualityMetrics":
        if not data:
            return cls()
        fields = {
            "modality", "width", "height", "file_size", "format", "variance",
            "blur_score", "phash", "validated", "reasons", "attributes",
        }
        return cls(**{key: data[key] for key in fields if key in data})


@dataclass
class ModalityAsset:
    """样本中的一个文本、图片或其他模态资产。"""

    modality: Modality
    role: str = "input"
    uri: str = ""
    text: str = ""
    mime_type: str = ""
    asset_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["modality"] = self.modality.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModalityAsset":
        return cls(
            modality=Modality(data.get("modality", data.get("type", Modality.TEXT))),
            role=str(data.get("role", "input")),
            uri=str(data.get("uri", data.get("file", ""))),
            text=str(data.get("text", "")),
            mime_type=str(data.get("mime_type", "")),
            asset_id=str(data.get("asset_id", "")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class ModalityRelation:
    """描述模态资产之间语义关系，例如 caption-of、context-of。"""

    source: str
    target: str
    relation: str
    score: Optional[float] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModalityRelation":
        score = data.get("score")
        return cls(
            source=str(data.get("source", "")),
            target=str(data.get("target", "")),
            relation=str(data.get("relation", "")),
            score=float(score) if score is not None else None,
            evidence=dict(data.get("evidence") or {}),
        )


@dataclass
class LabelDecision:
    """单个标签及其证据。多标签样本由多个实例组成。"""

    name: str
    score: float = 0.0
    source: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LabelDecision":
        return cls(
            name=str(data.get("name", "")),
            score=float(data.get("score", 0.0)),
            source=str(data.get("source", "unknown")),
            evidence=dict(data.get("evidence") or {}),
        )


@dataclass
class LabelResolution:
    """标签策略的输出：正式标签与待审核候选标签分离。"""

    labels: list[LabelDecision] = field(default_factory=list)
    candidates: list[LabelDecision] = field(default_factory=list)
    rejected: list[LabelDecision] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "labels": [item.to_dict() for item in self.labels],
            "candidates": [item.to_dict() for item in self.candidates],
            "rejected": [item.to_dict() for item in self.rejected],
        }


@dataclass
class LabelPolicy:
    """可选标签策略，默认保守地不自动升级发现标签。"""

    mode: LabelMode = LabelMode.HYBRID
    fixed_labels: list[str] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    min_score: float = 0.5
    discovery_threshold: float = 0.75
    promote_discovered: bool = False

    def __post_init__(self):
        if not isinstance(self.mode, LabelMode):
            self.mode = LabelMode(str(self.mode))
        self.aliases = {
            self._key(key): (value or "").strip()
            for key, value in self.aliases.items()
        }
        self.fixed_labels = self._canonical_names(self.fixed_labels)

    @staticmethod
    def _key(value: str) -> str:
        return " ".join((value or "").strip().casefold().split())

    def _canonical_name(self, value: str) -> str:
        key = self._key(value)
        return self.aliases.get(key, (value or "").strip())

    def _canonical_names(self, values: Iterable[str]) -> list[str]:
        result = []
        seen = set()
        for value in values:
            name = (value or "").strip()
            if not name:
                continue
            key = self._key(name)
            if key not in seen:
                seen.add(key)
                result.append(name)
        return result

    def resolve(self, decisions: Iterable[LabelDecision]) -> LabelResolution:
        """按模式解析标签，始终保留多标签结果。"""
        normalized: list[LabelDecision] = []
        best_by_name: dict[str, LabelDecision] = {}
        for raw in decisions:
            name = self._canonical_name(raw.name)
            if not name:
                continue
            item = LabelDecision(name, float(raw.score), raw.source, dict(raw.evidence))
            key = self._key(name)
            previous = best_by_name.get(key)
            if previous is None or item.score > previous.score:
                best_by_name[key] = item
        normalized.extend(best_by_name.values())

        fixed_keys = {self._key(label) for label in self.fixed_labels}
        result = LabelResolution()
        for item in normalized:
            key = self._key(item.name)
            is_fixed = key in fixed_keys
            if self.mode == LabelMode.FIXED:
                if is_fixed and item.score >= self.min_score:
                    result.labels.append(item)
                else:
                    result.rejected.append(item)
            elif self.mode == LabelMode.DISCOVERY:
                if item.score >= self.discovery_threshold and self.promote_discovered:
                    result.labels.append(item)
                elif item.score >= self.discovery_threshold:
                    result.candidates.append(item)
                else:
                    result.rejected.append(item)
            else:  # hybrid
                if is_fixed and item.score >= self.min_score:
                    result.labels.append(item)
                elif item.score >= self.discovery_threshold:
                    result.candidates.append(item)
                else:
                    result.rejected.append(item)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "fixed_labels": list(self.fixed_labels),
            "aliases": dict(self.aliases),
            "min_score": self.min_score,
            "discovery_threshold": self.discovery_threshold,
            "promote_discovered": self.promote_discovered,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "LabelPolicy":
        if not data:
            return cls()
        return cls(
            mode=data.get("mode", LabelMode.HYBRID),
            fixed_labels=list(data.get("fixed_labels") or []),
            aliases=dict(data.get("aliases") or {}),
            min_score=float(data.get("min_score", 0.5)),
            discovery_threshold=float(data.get("discovery_threshold", 0.75)),
            promote_discovered=bool(data.get("promote_discovered", False)),
        )


@dataclass
class SampleRecord:
    """最终 manifest 的一条通用多模态样本记录。

    ``file`` 保留给旧的图像分类任务；新任务使用 ``modalities`` 表达
    任意数量的文本、图片、音频、视频或网页上下文资产。
    """

    sample_id: str
    file: str = ""
    labels: list[LabelDecision] = field(default_factory=list)
    quality: QualityMetrics = field(default_factory=QualityMetrics)
    provenance: dict[str, Any] = field(default_factory=dict)
    pipeline: dict[str, Any] = field(default_factory=dict)
    modalities: list[ModalityAsset] = field(default_factory=list)
    relations: list[ModalityRelation] = field(default_factory=list)
    task_type: str = "multimodal"

    def to_dict(self) -> dict[str, Any]:
        modalities = list(self.modalities)
        if not modalities and self.file:
            modalities = [ModalityAsset(Modality.IMAGE, role="image", uri=self.file)]
        return {
            "id": self.sample_id,
            "file": self.file,
            "task_type": self.task_type,
            "modalities": [item.to_dict() for item in modalities],
            "relations": [item.to_dict() for item in self.relations],
            "labels": [label.to_dict() for label in self.labels],
            "quality": self.quality.to_dict(),
            "provenance": self.provenance,
            "pipeline": self.pipeline,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SampleRecord":
        file = str(data.get("file", ""))
        modality_data = data.get("modalities", [])
        modalities = [ModalityAsset.from_dict(item) for item in modality_data]
        if not modalities and file:
            modalities = [ModalityAsset(Modality.IMAGE, role="image", uri=file)]
        return cls(
            sample_id=str(data.get("id", data.get("sample_id", ""))),
            file=file,
            labels=[LabelDecision.from_dict(item) for item in data.get("labels", [])],
            quality=QualityMetrics.from_dict(data.get("quality")),
            provenance=dict(data.get("provenance") or {}),
            pipeline=dict(data.get("pipeline") or {}),
            modalities=modalities,
            relations=[ModalityRelation.from_dict(item) for item in data.get("relations", [])],
            task_type=str(data.get("task_type", "multimodal")),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
