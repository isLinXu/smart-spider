# coding=utf-8
"""Second-model review for semantic filter quarantine candidates."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image, UnidentifiedImageError
from tqdm import tqdm

from .dataset_filter import (
    DatasetImageFilter,
    _atomic_write_json,
    _atomic_write_jsonl,
    _manifest_path,
    _record_path,
)


BLIP_POSITIVE_PROMPTS = (
    "a delivery truck with its rear cargo doors open",
    "workers loading boxes through the open back doors of a truck",
    "a forklift loading cargo through the open rear doors of a truck",
    "a truck backed into a warehouse loading dock",
    "a worker opening or closing a truck cargo door",
)
BLIP_NEGATIVE_PROMPTS = (
    "a truck being repaired, washed, or inspected",
    "a fire truck or emergency rescue vehicle",
    "an ambulance parked outside a building",
    "a person opening the driver cab door or hood of a truck",
    "a truck parked or driving on a road",
    "a closed truck photographed from outside",
    "a dump truck or crane truck at a construction site",
    "a shipping container at a port or rail yard",
    "an industrial lift table or loading dock equipment product",
    "an advertisement poster, photo collage, diagram, or product image",
    "people moving furniture through a house doorway",
    "an empty warehouse loading dock with no truck",
    "inside a truck cab or passenger vehicle",
    "a truck carrying cargo on an open flatbed trailer",
)


@dataclass(frozen=True)
class BLIPReviewScores:
    positive: float
    content_evidence: float
    negative: float
    margin: float
    positive_prompt: str
    negative_prompt: str


@dataclass(frozen=True)
class BLIPReviewThresholds:
    min_positive: float = 0.95
    min_content_evidence: float = 0.2
    min_margin: float = 0.2

    def accepts(self, scores: BLIPReviewScores) -> bool:
        return (
            scores.positive >= self.min_positive
            and scores.content_evidence >= self.min_content_evidence
            and scores.margin >= self.min_margin
        )


class BLIPITMReviewer:
    """Score explicit target and mismatch prompts with BLIP ITM."""

    def __init__(
        self,
        *,
        model_name: str = "Salesforce/blip-itm-base-coco",
        device: str = "auto",
        batch_size: int = 20,
        cache_dir: Optional[str | os.PathLike[str]] = None,
        local_files_only: bool = False,
        positive_prompts: Sequence[str] = BLIP_POSITIVE_PROMPTS,
        negative_prompts: Sequence[str] = BLIP_NEGATIVE_PROMPTS,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("BLIP batch size must be positive")
        try:
            import torch
            from transformers import BlipForImageTextRetrieval, BlipProcessor
        except ImportError as exc:
            raise RuntimeError(
                "BLIP review requires transformers; install smart-spider[filter]"
            ) from exc

        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.torch = torch
        self.device = device
        self.model_name = model_name
        self.batch_size = batch_size
        self.positive_prompts = tuple(positive_prompts)
        self.negative_prompts = tuple(negative_prompts)
        if len(self.positive_prompts) < 2 or not self.negative_prompts:
            raise ValueError("BLIP review requires multiple positive and negative prompts")
        self.processor = BlipProcessor.from_pretrained(
            model_name,
            local_files_only=local_files_only,
        )
        self.model = BlipForImageTextRetrieval.from_pretrained(
            model_name,
            local_files_only=local_files_only,
        ).eval().to(device)
        signature_payload = json.dumps(
            {
                "model": model_name,
                "positive": self.positive_prompts,
                "negative": self.negative_prompts,
            },
            sort_keys=True,
        )
        self.signature = hashlib.sha256(signature_payload.encode("utf-8")).hexdigest()
        self.cache_dir: Optional[Path] = None
        if cache_dir:
            self.cache_dir = Path(cache_dir).expanduser().resolve() / self.signature[:16]
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_hits = 0
        self.cache_misses = 0

    @staticmethod
    def _image_key(image: Image.Image) -> str:
        rgb = image.convert("RGB")
        digest = hashlib.sha256()
        digest.update(str(rgb.size).encode("ascii"))
        digest.update(rgb.tobytes())
        return digest.hexdigest()

    def _load_cached(self, image: Image.Image) -> Optional[BLIPReviewScores]:
        if self.cache_dir is None:
            return None
        path = self.cache_dir / f"{self._image_key(image)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("signature") != self.signature:
                raise ValueError("cache signature mismatch")
            scores = BLIPReviewScores(**payload["scores"])
            self.cache_hits += 1
            return scores
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.cache_misses += 1
            return None

    def _store_cached(self, image: Image.Image, scores: BLIPReviewScores) -> None:
        if self.cache_dir is None:
            return
        path = self.cache_dir / f"{self._image_key(image)}.json"
        if path.exists():
            return
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_dir,
                prefix=f".{path.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(
                    {"signature": self.signature, "scores": asdict(scores)},
                    handle,
                    ensure_ascii=False,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def _score_prompt(
        self,
        images: Sequence[Image.Image],
        prompt: str,
    ) -> list[float]:
        scores: list[float] = []
        for start in range(0, len(images), self.batch_size):
            chunk = images[start:start + self.batch_size]
            inputs = self.processor(
                images=list(chunk),
                text=[prompt] * len(chunk),
                return_tensors="pt",
                padding=True,
            ).to(self.device)
            with self.torch.inference_mode():
                output = self.model(**inputs, use_itm_head=True)
                probabilities = self.torch.softmax(output.itm_score, dim=-1)[:, 1]
            scores.extend(float(value) for value in probabilities.detach().cpu())
        return scores

    def score_images(self, images: Sequence[Image.Image]) -> list[BLIPReviewScores]:
        if not images:
            return []
        cached = [self._load_cached(image) for image in images]
        missing = [index for index, scores in enumerate(cached) if scores is None]
        if missing:
            missing_images = [images[index] for index in missing]
            positive_rows = [
                self._score_prompt(missing_images, prompt)
                for prompt in self.positive_prompts
            ]
            negative_rows = [
                self._score_prompt(missing_images, prompt)
                for prompt in self.negative_prompts
            ]
            for column, image_index in enumerate(missing):
                positive_values = [row[column] for row in positive_rows]
                negative_values = [row[column] for row in negative_rows]
                positive_index = max(range(len(positive_values)), key=positive_values.__getitem__)
                negative_index = max(range(len(negative_values)), key=negative_values.__getitem__)
                positive = positive_values[positive_index]
                negative = negative_values[negative_index]
                scores = BLIPReviewScores(
                    positive=positive,
                    content_evidence=max(positive_values[:-1]),
                    negative=negative,
                    margin=positive - negative,
                    positive_prompt=self.positive_prompts[positive_index],
                    negative_prompt=self.negative_prompts[negative_index],
                )
                cached[image_index] = scores
                self._store_cached(images[image_index], scores)
        return [scores for scores in cached if scores is not None]


def _clip_violation(decision: dict[str, Any], thresholds: dict[str, Any]) -> float:
    scores = decision.get("scores") or {}
    if not scores:
        return float("inf")
    relevance = float(scores.get("relevance") or 0.0)
    mismatch = float(scores.get("mismatch") or 0.0)
    scene = float(scores.get("scene_evidence") or 0.0)
    violations: list[float] = []
    min_relevance = thresholds.get("min_relevance")
    mismatch_margin = thresholds.get("mismatch_margin")
    minimum_scene = thresholds.get("minimum_scene_evidence")
    scene_margin = thresholds.get("scene_evidence_margin")
    if min_relevance is not None and relevance < float(min_relevance):
        violations.append(float(min_relevance) - relevance)
    if mismatch_margin is not None and mismatch - relevance >= float(mismatch_margin):
        violations.append(mismatch - relevance - float(mismatch_margin))
    if minimum_scene is not None and scene < float(minimum_scene):
        violations.append(float(minimum_scene) - scene)
    if scene_margin is not None and mismatch - scene >= float(scene_margin):
        violations.append(mismatch - scene - float(scene_margin))
    return max(violations or [0.0])


def _read_labels(path: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            image_path = str(row.get("path") or "").strip()
            label = str(row.get("label") or "").strip()
            if not image_path or label not in {"relevant", "irrelevant"}:
                raise ValueError(f"invalid review label row: {row}")
            if image_path in labels:
                raise ValueError(f"duplicate review label path: {image_path}")
            labels[image_path] = label
    if not labels:
        raise ValueError("review labels file is empty")
    return labels


def _classification_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    labeled = [row for row in rows if row.get("label")]
    if not labeled:
        return {}
    true_positive = sum(row["label"] == "relevant" and row["candidate"] for row in labeled)
    false_positive = sum(row["label"] == "irrelevant" and row["candidate"] for row in labeled)
    true_negative = sum(row["label"] == "irrelevant" and not row["candidate"] for row in labeled)
    false_negative = sum(row["label"] == "relevant" and not row["candidate"] for row in labeled)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    return {
        "labeled": len(labeled),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(2 * precision * recall / (precision + recall), 6) if precision + recall else 0.0,
    }


def run_quarantine_review(
    dataset_dir: str | os.PathLike[str],
    source_run_id: str,
    reviewer: BLIPITMReviewer,
    *,
    thresholds: Optional[BLIPReviewThresholds] = None,
    candidate_limit: Optional[int] = None,
    labels_path: Optional[str | os.PathLike[str]] = None,
    run_id: Optional[str] = None,
) -> dict[str, Any]:
    """Review semantic quarantine records and write a non-mutating rescue report."""
    dataset = Path(dataset_dir).expanduser().resolve()
    if not dataset.is_dir():
        raise NotADirectoryError(f"dataset directory not found: {dataset}")
    if not DatasetImageFilter.RUN_ID_PATTERN.fullmatch(source_run_id):
        raise ValueError("invalid source quarantine run id")
    if candidate_limit is not None and candidate_limit <= 0:
        raise ValueError("candidate limit must be positive")
    thresholds = thresholds or BLIPReviewThresholds()
    source_dir = dataset / "_quarantine" / source_run_id
    manifest_path = source_dir / "quarantine_manifest.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(f"quarantine manifest not found: {manifest_path}")
    source_report = json.loads((source_dir / "report.json").read_text(encoding="utf-8"))
    clip_thresholds = source_report.get("thresholds") or {}
    records = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    labels = _read_labels(Path(labels_path).expanduser().resolve()) if labels_path else {}
    if labels:
        available = {str(record.get("original_path") or "") for record in records}
        missing = sorted(set(labels) - available)
        if missing:
            raise ValueError(f"review labels are not in source quarantine: {', '.join(missing[:5])}")
        selected = [record for record in records if record.get("original_path") in labels]
    else:
        semantic = [
            record
            for record in records
            if (record.get("decision") or {}).get("category") == "semantic_mismatch"
        ]
        selected = sorted(
            semantic,
            key=lambda record: _clip_violation(record.get("decision") or {}, clip_thresholds),
        )
        if candidate_limit is not None:
            selected = selected[:candidate_limit]

    run_id = run_id or datetime.now().strftime("blip_review_%Y%m%d_%H%M%S")
    if not DatasetImageFilter.RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id may only contain letters, digits, dot, underscore, and dash")
    output_dir = dataset / "_review_runs" / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    progress = tqdm(total=len(selected), desc="BLIPReview")
    try:
        for start in range(0, len(selected), reviewer.batch_size):
            chunk = selected[start:start + reviewer.batch_size]
            images: list[Image.Image] = []
            valid: list[dict[str, Any]] = []
            for record in chunk:
                path = source_dir / str(record.get("quarantine_path") or "")
                try:
                    with Image.open(path) as opened:
                        images.append(opened.convert("RGB"))
                    valid.append(record)
                except (FileNotFoundError, UnidentifiedImageError, OSError) as exc:
                    rows.append({
                        "path": str(record.get("original_path") or ""),
                        "candidate": False,
                        "error": str(exc),
                    })
                    progress.update(1)
            review_scores = reviewer.score_images(images)
            if len(review_scores) != len(valid):
                raise RuntimeError("BLIP reviewer returned a row count different from input images")
            for record, scores in zip(valid, review_scores):
                original = str(record.get("original_path") or "")
                decision = record.get("decision") or {}
                rows.append({
                    "path": original,
                    "label": labels.get(original, ""),
                    "candidate": thresholds.accepts(scores),
                    "clip_violation": round(_clip_violation(decision, clip_thresholds), 6),
                    "clip_category": decision.get("category") or "",
                    "clip_reasons": decision.get("reasons") or [],
                    "scores": asdict(scores),
                })
                progress.update(1)
    finally:
        progress.close()
    rows.sort(
        key=lambda row: (
            not bool(row.get("candidate")),
            -float((row.get("scores") or {}).get("margin") or -1.0),
        )
    )
    _atomic_write_jsonl(output_dir / "review.jsonl", rows)
    report: dict[str, Any] = {
        "run_id": run_id,
        "source_run_id": source_run_id,
        "dataset": str(dataset),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scanned": len(rows),
        "candidates": sum(bool(row.get("candidate")) for row in rows),
        "thresholds": asdict(thresholds),
        "model": reviewer.model_name,
        "device": reviewer.device,
        "cache": {
            "hits": reviewer.cache_hits,
            "misses": reviewer.cache_misses,
        },
        "metrics": _classification_metrics(rows),
        "mutated_dataset": False,
    }
    _atomic_write_json(output_dir / "report.json", report)
    return report


def export_review_candidates(
    dataset_dir: str | os.PathLike[str],
    source_run_id: str,
    approved_paths: Sequence[str],
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Export active images plus approved quarantine candidates to a new dataset."""
    dataset = Path(dataset_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if not dataset.is_dir():
        raise NotADirectoryError(f"dataset directory not found: {dataset}")
    if output.exists():
        raise FileExistsError(f"export output already exists: {output}")
    if not DatasetImageFilter.RUN_ID_PATTERN.fullmatch(source_run_id):
        raise ValueError("invalid source quarantine run id")
    requested = [str(Path(value.strip())) for value in approved_paths if value.strip()]
    if not requested:
        raise ValueError("approved review candidate list is empty")
    if len(requested) != len(set(requested)):
        raise ValueError("approved review candidate list contains duplicate paths")

    source_dir = dataset / "_quarantine" / source_run_id
    quarantine_records = {
        str(record.get("original_path") or ""): record
        for record in (
            json.loads(line)
            for line in (source_dir / "quarantine_manifest.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        )
    }
    missing = sorted(set(requested) - set(quarantine_records))
    if missing:
        raise ValueError(f"approved paths are not in source quarantine: {', '.join(missing[:5])}")

    active_metadata = [
        json.loads(line)
        for line in (dataset / "metadata.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    active_manifest = [
        json.loads(line)
        for line in (dataset / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    active_manifest_by_path: dict[str, dict[str, Any]] = {}
    for record in active_manifest:
        _, relative = DatasetImageFilter._resolve_for_verify(dataset, _manifest_path(record))
        active_manifest_by_path[relative] = record
    backup_manifest = [
        json.loads(line)
        for line in (source_dir / "backups" / "manifest.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    backup_manifest_by_path: dict[str, dict[str, Any]] = {}
    for record in backup_manifest:
        _, relative = DatasetImageFilter._resolve_for_verify(dataset, _manifest_path(record))
        backup_manifest_by_path[relative] = record

    export_items: list[tuple[Path, str, dict[str, Any], dict[str, Any]]] = []
    active_paths: set[str] = set()
    for metadata in active_metadata:
        absolute, relative = DatasetImageFilter._resolve_for_verify(dataset, _record_path(metadata))
        manifest = active_manifest_by_path.get(relative)
        if manifest is None:
            raise ValueError(f"active manifest record missing for export: {relative}")
        active_paths.add(relative)
        export_items.append((absolute, relative, metadata, manifest))
    collisions = sorted(active_paths.intersection(requested))
    if collisions:
        raise ValueError(f"approved paths are already active: {', '.join(collisions[:5])}")
    for relative in requested:
        quarantine = quarantine_records[relative]
        source = source_dir / str(quarantine.get("quarantine_path") or "")
        manifest = backup_manifest_by_path.get(relative)
        if manifest is None:
            raise ValueError(f"backup manifest record missing for export: {relative}")
        export_items.append((source, relative, quarantine["metadata"], manifest))

    try:
        output.mkdir(parents=True, exist_ok=False)
        exported_metadata: list[dict[str, Any]] = []
        exported_manifest: list[dict[str, Any]] = []
        for source, relative, metadata, manifest in export_items:
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            normalized_metadata = dict(metadata)
            normalized_metadata["file_path"] = relative
            exported_metadata.append(normalized_metadata)
            normalized_manifest = dict(manifest)
            if normalized_manifest.get("file"):
                normalized_manifest["file"] = relative
            normalized_modalities = []
            for modality in normalized_manifest.get("modalities") or []:
                normalized_modality = dict(modality)
                if normalized_modality.get("path"):
                    normalized_modality["path"] = relative
                if normalized_modality.get("uri"):
                    normalized_modality["uri"] = relative
                normalized_modalities.append(normalized_modality)
            normalized_manifest["modalities"] = normalized_modalities
            exported_manifest.append(normalized_manifest)
        _atomic_write_jsonl(output / "metadata.jsonl", exported_metadata)
        _atomic_write_jsonl(output / "manifest.jsonl", exported_manifest)
        report = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_dataset": str(dataset),
            "source_run_id": source_run_id,
            "output_dataset": str(output),
            "active_images": len(active_metadata),
            "recovered_images": len(requested),
            "total_images": len(export_items),
        }
        _atomic_write_json(output / "_recovery_export_report.json", report)
        return report
    except Exception:
        if output.exists():
            shutil.rmtree(output)
        raise
