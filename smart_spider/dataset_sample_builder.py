# coding=utf-8
"""Accepted-image SampleRecord / metadata assembly (commit-time payload)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .compliance import apply_compliance_to_provenance
from .dataset_contracts import (
    URL_NORMALIZE_VERSION,
    Modality,
    ModalityAsset,
    QualityMetrics,
    SampleRecord,
)
from .dataset_governance import perceptual_fingerprint


@dataclass(frozen=True)
class AcceptedImageFacts:
    """Path-independent facts captured after admission and format conversion."""

    url: str
    keyword: str
    source: str
    job_id: str
    dataset_id: str = ""
    config_fingerprint: str = ""
    clip_model_name: Optional[str] = None
    query_image: str = ""
    image_similarity_threshold: float = 0.0
    sim: float = 0.0
    image_sim: Optional[float] = None
    output_ext: str = ".jpg"
    source_ext: str = ""
    width: int = 0
    height: int = 0
    format_conversion: dict[str, Any] | None = None
    scene_quality_decision: Any = None


def build_image_quality_metrics(
    *,
    image: Any,
    thumbnail: Any,
    output_content: bytes,
    output_format: str,
    scene_profile: Any,
    semantic_threshold: float,
    scene_decision: Any,
) -> QualityMetrics:
    fingerprint = perceptual_fingerprint(image)
    return QualityMetrics(
        modality=Modality.IMAGE.value,
        width=image.width,
        height=image.height,
        file_size=len(output_content),
        format=output_format,
        variance=float(np.var(thumbnail)),
        phash=fingerprint.phash,
        validated=True,
        attributes={
            "dhash": fingerprint.dhash,
            "scene_quality": scene_profile.to_dict() if scene_profile else None,
            "scene_semantic_threshold": semantic_threshold,
            "scene_quality_gate": (
                scene_decision.to_dict() if scene_decision is not None else None
            ),
        },
    )


def build_accepted_image_records(
    idx: int,
    save_path: str,
    relative_path: str,
    final_hash: str,
    *,
    facts: AcceptedImageFacts,
    quality: QualityMetrics,
    label_resolution: Any,
    label_policy: Any,
    compliance_policy: Any,
) -> tuple[SampleRecord, dict[str, Any]]:
    """Build SampleRecord + metadata.jsonl row for one committed image."""
    conversion = dict(facts.format_conversion or {})
    output_ext = facts.output_ext
    image_sim = facts.image_sim
    scene_decision = facts.scene_quality_decision
    sample = SampleRecord(
        sample_id=f"sha256:{final_hash}",
        file=relative_path,
        labels=label_resolution.labels,
        quality=quality,
        provenance=apply_compliance_to_provenance(
            {
                "source": facts.source,
                "query": facts.keyword,
                "url": facts.url,
                "url_normalize_version": URL_NORMALIZE_VERSION,
                "clip_model": facts.clip_model_name,
            },
            compliance_policy,
        ),
        pipeline={
            "job_id": facts.job_id,
            "dataset_id": facts.dataset_id,
            "config_fingerprint": facts.config_fingerprint,
            "label_policy": label_policy.to_dict(),
            "format_conversion": conversion,
            "image_query": {
                "path": facts.query_image,
                "similarity": round(image_sim, 4) if image_sim is not None else None,
                "threshold": facts.image_similarity_threshold,
            } if facts.query_image else None,
            "status": "accepted",
        },
        modalities=[ModalityAsset(
            modality=Modality.IMAGE,
            role="image",
            uri=relative_path,
            mime_type=(
                "image/jpeg"
                if output_ext == ".jpg"
                else f"image/{output_ext.lstrip('.') }"
            ),
        )],
        task_type="image_classification",
    )
    metadata = {
        "index": idx,
        "url": facts.url,
        "file_path": save_path,
        "sha256": final_hash,
        "batch": Path(relative_path).parent.name,
        "keyword": facts.keyword,
        "source": facts.source,
        "sim": round(facts.sim, 4),
        "image_sim": round(image_sim, 4) if image_sim is not None else None,
        "width": facts.width,
        "height": facts.height,
        "ext": output_ext,
        "source_ext": facts.source_ext,
        "format_conversion": conversion,
        "labels": [item.to_dict() for item in label_resolution.labels],
        "label_candidates": [item.to_dict() for item in label_resolution.candidates],
        "quality": quality.to_dict(),
        "scene_quality_gate": (
            scene_decision.to_dict() if scene_decision is not None else None
        ),
    }
    return sample, metadata


def bind_record_builder(
    *,
    facts: AcceptedImageFacts,
    quality: QualityMetrics,
    label_resolution: Any,
    label_policy: Any,
    compliance_policy: Any,
):
    """Return the ``build_records(idx, path, relative, hash)`` callback for materialize."""

    def build_records(
        idx: int,
        save_path: str,
        relative_path: str,
        final_hash: str,
    ) -> tuple[SampleRecord, dict[str, Any]]:
        return build_accepted_image_records(
            idx,
            save_path,
            relative_path,
            final_hash,
            facts=facts,
            quality=quality,
            label_resolution=label_resolution,
            label_policy=label_policy,
            compliance_policy=compliance_policy,
        )

    return build_records
