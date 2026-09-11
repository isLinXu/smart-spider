# coding=utf-8
"""Replay content exclusions and detector-backed scene gates on a dataset.

This command is deliberately non-mutating: it writes an auditable validation
report while leaving dataset images and indexes untouched.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from PIL import Image

from .dataset_filter import (
    CLIPPromptScorer,
    DecisionPolicy,
    VisualSignalAnalyzer,
    _atomic_write_json,
    _atomic_write_jsonl,
)
from .scene_quality import get_scene_quality_profile
from .scene_quality_gate import SceneQualityGate, SceneSignalDetector
from .scene_signal_detector import YoloSceneSignalDetector
from .synthetic_image_detector import (
    CLIPPhotographicStyleDetector,
    CompositeSceneSignalDetector,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    OnnxSyntheticImageDetector,
    download_synthetic_image_detector,
)


_GENERIC_PROFILES = {"pedestrian", "road_vehicle"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row must be an object at {path}:{line_number}")
            rows.append(value)
    return rows


def _resolve_image_path(dataset: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (dataset / candidate).resolve()
    try:
        resolved.relative_to(dataset)
    except ValueError as exc:
        raise ValueError(f"image path escapes dataset directory: {raw_path}") from exc
    return resolved


def _semantic_score(record: Mapping[str, Any], content: Mapping[str, Any]) -> float:
    scores = content.get("scores") or {}
    raw = scores.get("relevance")
    if raw is None:
        raw = record.get("sim")
    if raw is None:
        raise ValueError("record has neither sim nor content relevance score")
    return float(raw)


def _rescore_content(
    dataset: Path,
    metadata: list[dict[str, Any]],
    *,
    device: str,
    cache_dir: Optional[str | os.PathLike[str]],
    batch_size: int,
    ocr_all: bool,
) -> list[dict[str, Any]]:
    """Score each keyword with its own prompts to avoid cross-target leakage."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    positions_by_profile: dict[str, list[int]] = defaultdict(list)
    for position, record in enumerate(metadata):
        profile = get_scene_quality_profile(str(record.get("keyword") or ""))
        if profile.name not in _GENERIC_PROFILES:
            raise ValueError(f"unsupported validation profile: {profile.name!r}")
        positions_by_profile[profile.name].append(position)

    analyzer = VisualSignalAnalyzer(enable_ocr=ocr_all, ocr_all=ocr_all)
    decisions: list[dict[str, Any]] = []
    for profile_name, positions in sorted(positions_by_profile.items()):
        profile = get_scene_quality_profile(profile_name)
        scorer = CLIPPromptScorer(
            profile.prompts,
            device=device,
            cache_dir=cache_dir,
        )
        policy = DecisionPolicy(profile.thresholds)
        for offset in range(0, len(positions), batch_size):
            batch_positions = positions[offset:offset + batch_size]
            batch_images: list[Image.Image] = []
            batch_paths: list[str] = []
            for position in batch_positions:
                raw_path = str(metadata[position].get("file_path") or "")
                if not raw_path:
                    raise ValueError(f"record position {position} has no image path")
                path = _resolve_image_path(dataset, raw_path)
                with Image.open(path) as opened:
                    batch_images.append(opened.convert("RGB"))
                batch_paths.append(str(path.relative_to(dataset)))
            scores = scorer.score_images(batch_images)
            for position, relative, image, semantic_scores in zip(
                batch_positions, batch_paths, batch_images, scores
            ):
                signals = analyzer.analyze(image, semantic_scores)
                decision = policy.decide(
                    record_position=position,
                    path=relative,
                    scores=semantic_scores,
                    signals=signals,
                )
                row = decision.to_dict()
                row["profile"] = profile.name
                decisions.append(row)
    decisions.sort(key=lambda item: int(item["record_position"]))
    return decisions


def _final_action(content: Mapping[str, Any], scene_action: str) -> str:
    content_action = str(content.get("action") or "quarantine")
    if content_action == "quarantine":
        category = str(content.get("category") or "")
        reasons = {str(reason) for reason in content.get("reasons") or []}
        # A low CLIP score by itself is uncertainty, not reliable proof of
        # bad content.  Keep it in the review queue instead of auto-deleting.
        if category == "semantic_mismatch" and reasons <= {
            "low_relevance", "low_scene_evidence"
        }:
            return "review"
        return "reject"
    if scene_action == "reject":
        return "reject"
    if scene_action == "review":
        return "review"
    return "accept"


def _content_rejection_kind(content: Mapping[str, Any]) -> str:
    """Explain whether a quarantine is automatic or intentionally abstained."""
    if str(content.get("action") or "") != "quarantine":
        return "none"
    category = str(content.get("category") or "")
    reasons = {str(reason) for reason in content.get("reasons") or []}
    if category == "semantic_mismatch" and reasons <= {
        "low_relevance", "low_scene_evidence"
    }:
        return "review"
    return "reject"


def run_validation(
    dataset_dir: str | os.PathLike[str],
    decisions_path: Optional[str | os.PathLike[str]],
    detector: SceneSignalDetector,
    *,
    output_dir: str | os.PathLike[str],
    limit: Optional[int] = None,
    clip_device: str = "auto",
    clip_cache_dir: Optional[str | os.PathLike[str]] = None,
    batch_size: int = 16,
    ocr_all: bool = True,
) -> dict[str, Any]:
    """Run a non-mutating combined gate replay and write JSON/JSONL evidence."""
    dataset = Path(dataset_dir).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    metadata = _read_jsonl(dataset / "metadata.jsonl")
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        metadata = metadata[:limit]
    if decisions_path is None:
        decisions_file = output / "content_decisions.jsonl"
        content_decisions = _rescore_content(
            dataset,
            metadata,
            device=clip_device,
            cache_dir=clip_cache_dir,
            batch_size=batch_size,
            ocr_all=ocr_all,
        )
        _atomic_write_jsonl(decisions_file, content_decisions)
        content_mode = "per_target_rescore"
    else:
        decisions_file = Path(decisions_path).expanduser().resolve()
        content_decisions = _read_jsonl(decisions_file)
        content_mode = "existing_filter_run"

    content_by_position: dict[int, dict[str, Any]] = {}
    for item in content_decisions:
        position = int(item.get("record_position", -1))
        if position in content_by_position:
            raise ValueError(f"duplicate content decision position: {position}")
        content_by_position[position] = item

    content_counts: Counter[str] = Counter()
    scene_counts: Counter[str] = Counter()
    final_counts: Counter[str] = Counter()
    content_reasons: Counter[str] = Counter()
    scene_reasons: Counter[str] = Counter()
    cross_tab: Counter[str] = Counter()
    target_counts: dict[str, Counter[str]] = defaultdict(Counter)
    rows: list[dict[str, Any]] = []

    for position, record in enumerate(metadata):
        content = content_by_position.get(position)
        if content is None:
            raise ValueError(f"missing content decision for record position {position}")
        keyword = str(record.get("keyword") or "").strip()
        profile = get_scene_quality_profile(keyword)
        if profile.name not in _GENERIC_PROFILES:
            raise ValueError(f"unsupported validation keyword/profile: {keyword!r}")
        raw_path = str(content.get("path") or record.get("file_path") or "")
        if not raw_path:
            raise ValueError(f"record position {position} has no image path")
        image_path = _resolve_image_path(dataset, raw_path)
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        semantic_score = _semantic_score(record, content)
        scene = SceneQualityGate(profile, detector).evaluate_image(image, semantic_score)
        content_action = str(content.get("action") or "quarantine")
        final_action = _final_action(content, scene.action)

        content_counts[content_action] += 1
        scene_counts[scene.action] += 1
        final_counts[final_action] += 1
        cross_tab[f"{content_action}/{scene.action}"] += 1
        target_counts[profile.name][final_action] += 1
        content_reasons.update(str(reason) for reason in content.get("reasons") or [])
        scene_reasons.update(scene.reasons)
        rows.append({
            "record_position": position,
            "index": record.get("index", position),
            "keyword": keyword,
            "profile": profile.name,
            "path": raw_path,
            "semantic_score": semantic_score,
            "content_gate": {
                "action": content_action,
                "category": content.get("category"),
                "reasons": list(content.get("reasons") or []),
                "final_effect": _content_rejection_kind(content),
            },
            "scene_gate": scene.to_dict(),
            "final_action": final_action,
        })

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "non_mutating_replay",
        "dataset": str(dataset),
        "content_decisions": str(decisions_file),
        "content_mode": content_mode,
        "output_dir": str(output),
        "scanned": len(rows),
        "content_gate_actions": dict(sorted(content_counts.items())),
        "scene_gate_actions": dict(sorted(scene_counts.items())),
        "final_actions": dict(sorted(final_counts.items())),
        "content_scene_cross_tab": dict(sorted(cross_tab.items())),
        "targets": {
            name: dict(sorted(counts.items()))
            for name, counts in sorted(target_counts.items())
        },
        "content_reasons": dict(content_reasons.most_common()),
        "scene_reasons": dict(scene_reasons.most_common()),
        "model_versions": dict(sorted(detector.model_versions.items())),
        "interpretation_note": (
            "These are gate outcomes on an unlabeled replay set, not accuracy metrics. "
            "Manual labels are required to calculate precision and recall."
        ),
    }
    _atomic_write_jsonl(output / "decisions.jsonl", rows)
    _atomic_write_json(output / "report.json", report)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay text/illustration exclusions plus YOLO scene evidence",
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument(
        "--filter-run",
        help="Existing offline filter run id/path; omit to rescore with per-target prompts",
    )
    parser.add_argument(
        "--yolo-model",
        default=os.environ.get("SMART_SPIDER_YOLO_MODEL", "yolo11n.pt"),
    )
    parser.add_argument("--device", help="YOLO device, for example cpu, mps, or 0")
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument("--clip-device", default="auto")
    parser.add_argument("--cache-dir", default=".filter_cache")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument(
        "--synthetic-screen",
        action="store_true",
        help="screen AI-generated images and route high-confidence hits to review",
    )
    parser.add_argument(
        "--style-screen",
        action="store_true",
        help="screen illustrations/renders and route high-confidence hits to review",
    )
    parser.add_argument(
        "--synthetic-model",
        help="local ONNX model path; omitted downloads the pinned model revision",
    )
    parser.add_argument(
        "--synthetic-config",
        help="local detector config.json; defaults beside --synthetic-model",
    )
    parser.add_argument("--synthetic-model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--synthetic-model-revision", default=DEFAULT_MODEL_REVISION)
    parser.add_argument("--synthetic-cache-dir")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--run-id", default="pedestrian_vehicle_combined_validation")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    dataset = Path(args.dataset).expanduser().resolve()
    decisions = None
    if args.filter_run:
        supplied = Path(args.filter_run).expanduser()
        decisions = (
            supplied.resolve()
            if supplied.is_file()
            else dataset / "_filter_runs" / args.filter_run / "decisions.jsonl"
        )
    output = dataset / "_quality_validation" / args.run_id
    try:
        scene_detector: SceneSignalDetector = YoloSceneSignalDetector(
            model_name=args.yolo_model,
            confidence=args.confidence,
            device=args.device,
        )
        if args.synthetic_screen:
            if args.synthetic_model:
                model_path = Path(args.synthetic_model).expanduser().resolve()
                config_path = (
                    Path(args.synthetic_config).expanduser().resolve()
                    if args.synthetic_config
                    else None
                )
            else:
                model_path, config_path = download_synthetic_image_detector(
                    model_id=args.synthetic_model_id,
                    revision=args.synthetic_model_revision,
                    cache_dir=args.synthetic_cache_dir,
                )
            synthetic_detector = OnnxSyntheticImageDetector(
                model_path,
                config_path=config_path,
                model_id=args.synthetic_model_id,
                revision=args.synthetic_model_revision,
            )
            scene_detector = CompositeSceneSignalDetector(
                scene_detector,
                synthetic_detector,
            )
        if args.style_screen:
            scene_detector = CompositeSceneSignalDetector(
                scene_detector,
                CLIPPhotographicStyleDetector(
                    device=args.clip_device,
                    cache_dir=args.cache_dir,
                ),
            )
        report = run_validation(
            dataset,
            decisions,
            scene_detector,
            output_dir=output,
            limit=args.limit,
            clip_device=args.clip_device,
            clip_cache_dir=args.cache_dir,
            batch_size=args.batch_size,
            ocr_all=not args.no_ocr,
        )
    except (FileNotFoundError, OSError, ValueError, ImportError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
