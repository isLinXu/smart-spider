"""Optional Jina enrichment backends for the multimodal annotation router.

Models are loaded only when an enabled backend is first used. Image inference
reads the crawler's local staging file; it never fetches an asset URL again.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import numpy as np

from .dataset_contracts import LabelDecision, Modality, SampleRecord

OMNI_MODEL = "jinaai/jina-embeddings-v5-omni-nano"
RERANKER_V2 = "jinaai/jina-reranker-v2-base-multilingual"
RERANKER_V35 = "jinaai/jina-reranker-v3.5"


def _vector(value: Any) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value, dtype=np.float32).reshape(-1)
    if not array.size or not np.isfinite(array).all():
        raise ValueError("Jina returned an empty or non-finite embedding")
    norm = float(np.linalg.norm(array))
    if norm <= 0:
        raise ValueError("Jina returned a zero embedding")
    return array / norm


def _text_parts(sample: SampleRecord, max_chars: int) -> list[str]:
    parts: list[str] = []
    title = str(sample.provenance.get("title") or "").strip()
    if title:
        parts.append(title[:max_chars])
    for asset in sample.modalities:
        if asset.modality == Modality.TEXT and asset.text.strip():
            parts.append(asset.text.strip()[:max_chars])
        elif asset.modality == Modality.IMAGE:
            alt = str(asset.metadata.get("alt") or "").strip()
            if alt:
                parts.append(alt[:max_chars])
    return parts


class JinaOmniAnnotationBackend:
    """Return uncalibrated label suggestions using the classification adapter.

    Suggestions are advisory even for fixed labels. A calibrated label policy or
    human review must decide whether to promote them to ground truth.
    """

    def __init__(
        self,
        labels: Iterable[str],
        *,
        model_id: str = OMNI_MODEL,
        revision: str | None = None,
        top_k: int = 3,
        min_similarity: float = 0.0,
        max_text_chars: int = 6000,
        max_text_parts: int = 4,
        max_images: int = 4,
        model: Any = None,
    ) -> None:
        self.labels = tuple(dict.fromkeys(str(x).strip() for x in labels if str(x).strip()))
        if not self.labels:
            raise ValueError("Jina omni needs at least one label description")
        if (
            top_k <= 0 or max_text_chars <= 0 or max_text_parts <= 0 or max_images <= 0
            or not -1 <= min_similarity <= 1
        ):
            raise ValueError("invalid Jina omni limit or min_similarity")
        self.model_id = model_id
        self.revision = revision
        self.top_k = top_k
        self.min_similarity = min_similarity
        self.max_text_chars = max_text_chars
        self.max_text_parts = max_text_parts
        self.max_images = max_images
        self._model = model
        self._label_vectors: dict[str, np.ndarray] = {}

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("install smart-spider[jina] for Jina omni") from exc
            options: dict[str, Any] = {
                "trust_remote_code": True,
                "model_kwargs": {"default_task": "classification", "modality": "vision"},
            }
            if self.revision:
                options["revision"] = self.revision
            self._model = SentenceTransformer(self.model_id, **options)
        return self._model

    def _encode(self, value: Any) -> np.ndarray:
        # encode_document supplies the Document: prefix required by this adapter.
        return _vector(self._get_model().encode_document(value))

    def _labels(self) -> dict[str, np.ndarray]:
        for label in self.labels:
            if label not in self._label_vectors:
                self._label_vectors[label] = self._encode(label)
        return self._label_vectors

    def annotate_batch(self, samples: list[SampleRecord]) -> dict[str, list[LabelDecision]]:
        labels = self._labels()
        results: dict[str, list[LabelDecision]] = {}
        for sample in samples:
            image_vectors: list[tuple[str, str, np.ndarray]] = []
            if sample._annotation_image_paths:
                from PIL import Image

                for asset_id, path in list(sample._annotation_image_paths.items())[: self.max_images]:
                    with Image.open(path) as image:
                        image_vectors.append(("image", asset_id, self._encode(image.convert("RGB"))))
            # Crawled page text and alt text may describe neighboring content.
            # When validated image bytes are available, ground image suggestions
            # in those bytes; text remains the fallback for text-only samples.
            evidence_vectors = image_vectors
            if not evidence_vectors:
                evidence_vectors = [
                    ("text", f"text:{index}", self._encode(part))
                    for index, part in enumerate(
                        _text_parts(sample, self.max_text_chars)[: self.max_text_parts]
                    )
                ]
            scored: list[tuple[float, str, str, str]] = []
            for label, vector in labels.items():
                if not evidence_vectors:
                    continue
                score, modality, asset_id = max(
                    ((float(np.dot(vector, candidate)), kind, source) for kind, source, candidate in evidence_vectors),
                    key=lambda item: item[0],
                )
                if score >= self.min_similarity:
                    scored.append((score, label, modality, asset_id))
            scored.sort(key=lambda item: (-item[0], item[1]))
            results[sample.sample_id] = [
                LabelDecision(
                    name=label,
                    score=score,
                    source="jina_omni",
                    evidence={
                        "model": self.model_id,
                        "revision": self.revision or "default",
                        "task": "classification",
                        "modality": modality,
                        "asset_id": asset_id,
                        "metric": "cosine_similarity",
                    },
                    advisory=True,
                )
                for score, label, modality, asset_id in scored[: self.top_k]
            ]
        return results


class JinaOmniRetrievalEncoder:
    """Image documents and image/text queries in the omni retrieval space."""

    def __init__(
        self,
        *,
        model_id: str = OMNI_MODEL,
        revision: str | None = None,
        device: str = "cpu",
        model: Any = None,
    ) -> None:
        self.model_id = model_id
        self.revision = revision
        self.device = device
        self._model = model

    def _get_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("install smart-spider[jina] for Jina omni retrieval") from exc
            options: dict[str, Any] = {
                "trust_remote_code": True,
                "model_kwargs": {"default_task": "retrieval", "modality": "vision"},
                "device": self.device,
            }
            if self.revision:
                options["revision"] = self.revision
            self._model = SentenceTransformer(self.model_id, **options)
        return self._model

    def encode_images(self, images: Iterable[Any]) -> np.ndarray:
        vectors = [_vector(self._get_model().encode_document(image)) for image in images]
        return np.stack(vectors) if vectors else np.empty((0, 0), dtype=np.float32)

    def encode_query_image(self, image: Any) -> np.ndarray:
        return _vector(self._get_model().encode_query(image))

    def encode_query_text(self, text: str) -> np.ndarray:
        if not text.strip():
            raise ValueError("text query must not be empty")
        return _vector(self._get_model().encode_query(text))


class JinaTextRerankBackend:
    """Rank fetched text samples within each query group, preserving evidence.

    Ranking is observational. Scores are not treated as calibrated acceptance
    thresholds, and a batch's rank is meaningful only inside that batch.
    """

    def __init__(
        self,
        model_id: str = RERANKER_V35,
        *,
        revision: str | None = None,
        max_text_chars: int = 6000,
        model: Any = None,
    ) -> None:
        if model_id not in (RERANKER_V2, RERANKER_V35):
            raise ValueError(f"unsupported Jina reranker: {model_id}")
        if max_text_chars <= 0:
            raise ValueError("max_text_chars must be positive")
        self.model_id = model_id
        self.revision = revision
        self.max_text_chars = max_text_chars
        self._model = model

    def _get_model(self):
        if self._model is None:
            try:
                from transformers import AutoModel, AutoModelForSequenceClassification
            except ImportError as exc:
                raise RuntimeError("install smart-spider[jina] for Jina reranking") from exc
            loader = AutoModelForSequenceClassification if self.model_id == RERANKER_V2 else AutoModel
            options: dict[str, Any] = {"trust_remote_code": True}
            if self.revision:
                options["revision"] = self.revision
            self._model = loader.from_pretrained(self.model_id, **options)
            self._model.eval()
        return self._model

    def annotate_batch(self, samples: list[SampleRecord]) -> dict[str, list[LabelDecision]]:
        groups: dict[str, list[tuple[SampleRecord, str]]] = defaultdict(list)
        for sample in samples:
            query = str(sample.provenance.get("query") or "").strip()
            parts = _text_parts(sample, self.max_text_chars)
            if query and parts:
                groups[query].append((sample, "\n".join(parts)[: self.max_text_chars]))
        model = self._get_model() if groups else None
        pending: list[tuple[SampleRecord, dict[str, Any]]] = []
        for query, entries in groups.items():
            documents = [document for _, document in entries]
            if self.model_id == RERANKER_V2:
                raw = model.compute_score([[query, doc] for doc in documents], max_length=1024)
                scores = np.asarray(raw, dtype=float).reshape(-1)
                if len(scores) != len(entries):
                    raise ValueError("Jina v2 returned the wrong number of scores")
                ranked = sorted(range(len(entries)), key=lambda i: -scores[i])
                by_index = {i: float(scores[i]) for i in ranked}
            else:
                ranked_results = model.rerank(query, documents)
                ranked = [int(item["index"]) for item in ranked_results]
                if sorted(ranked) != list(range(len(entries))):
                    raise ValueError("Jina v3.5 returned invalid candidate indexes")
                by_index = {int(item["index"]): float(item["relevance_score"]) for item in ranked_results}
            if not all(math.isfinite(score) for score in by_index.values()):
                raise ValueError("Jina reranker returned a non-finite score")
            for rank, index in enumerate(ranked, start=1):
                sample = entries[index][0]
                pending.append((sample, {
                    "model": self.model_id,
                    "revision": self.revision or "default",
                    "query": query,
                    "score": by_index[index],
                    "rank": rank,
                    "candidate_count": len(entries),
                    "scope": "annotation_batch",
                }))
        for sample, evidence in pending:
            sample.pipeline["jina_rerank"] = evidence
        return {sample.sample_id: [] for sample in samples}
