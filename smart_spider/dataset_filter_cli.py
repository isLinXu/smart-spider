# coding=utf-8
"""Command-line interface for reversible dataset filtering."""
from __future__ import annotations

import argparse
import json
from typing import Optional

from .dataset_filter import (
    CLIPPromptScorer,
    DatasetImageFilter,
    DecisionPolicy,
    FilterThresholds,
    PromptSet,
    VisualSignalAnalyzer,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Use CLIP and advertisement signals to filter an image dataset",
    )
    parser.add_argument("--dataset", required=True, help="Dataset directory")
    parser.add_argument("--query", default="货车装卸区 开关门", help="Target scene query")
    parser.add_argument("--model", default="ViT-B/32", help="CLIP model name")
    parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or mps")
    parser.add_argument(
        "--cache-dir",
        help="Optional directory for reusable CLIP image embeddings",
    )
    parser.add_argument("--batch-size", type=int, default=32, help="CLIP batch size")
    parser.add_argument("--limit", type=int, help="Evenly sample N records for a dry run")
    parser.add_argument("--run-id", help="Explicit run identifier")
    parser.add_argument(
        "--profile",
        choices=("conservative", "balanced", "strict", "precision"),
        default="balanced",
        help="Threshold profile: conservative, balanced, strict, or precision",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move rejected files to quarantine and rewrite active indexes",
    )
    parser.add_argument(
        "--dedupe-exact",
        action="store_true",
        help="Find exact SHA-256 duplicates without initializing CLIP",
    )
    parser.add_argument(
        "--dedupe-near",
        action="store_true",
        help="Find perceptual near duplicates; dry-run unless --apply is supplied",
    )
    parser.add_argument("--near-distance", type=int, default=4)
    parser.add_argument("--near-aspect-delta", type=float, default=0.03)
    parser.add_argument("--near-color-distance", type=float, default=35.0)
    parser.add_argument(
        "--reject-list",
        help="Text file of active relative image paths rejected by visual review",
    )
    parser.add_argument(
        "--review-quarantine",
        metavar="RUN_ID",
        help="Use BLIP to produce a non-mutating rescue report for a quarantine run",
    )
    parser.add_argument("--review-labels", help="Optional path,label CSV for evaluation")
    parser.add_argument("--review-limit", type=int, help="Review the N closest semantic rejects")
    parser.add_argument(
        "--export-review-list",
        help="Export active data plus approved paths from --review-quarantine",
    )
    parser.add_argument("--export-output", help="New dataset directory for review export")
    parser.add_argument(
        "--blip-model",
        default="Salesforce/blip-itm-base-coco",
        help="Hugging Face BLIP image-text matching model",
    )
    parser.add_argument("--blip-cache-dir", help="Optional reusable BLIP score cache")
    parser.add_argument("--blip-local-only", action="store_true")
    parser.add_argument("--blip-min-positive", type=float, default=0.95)
    parser.add_argument("--blip-min-content", type=float, default=0.2)
    parser.add_argument("--blip-min-margin", type=float, default=0.2)
    parser.add_argument("--restore", metavar="RUN_ID", help="Restore an applied run")
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Enable Tesseract OCR for likely text/advertisement candidates",
    )
    parser.add_argument("--ocr-all", action="store_true", help="Run OCR on every image")
    parser.add_argument("--min-relevance", type=float)
    parser.add_argument("--mismatch-margin", type=float)
    parser.add_argument("--advertisement-score", type=float)
    parser.add_argument("--advertisement-margin", type=float)
    parser.add_argument("--advertisement-support-score", type=float)
    parser.add_argument("--advertisement-support-margin", type=float)
    parser.add_argument("--text-area-ratio", type=float)
    parser.add_argument("--minimum-scene-evidence", type=float)
    parser.add_argument("--scene-evidence-margin", type=float)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Validate indexes, active quarantine files, and image readability",
    )
    parser.add_argument(
        "--verify-hashes",
        action="store_true",
        help="Also compare active image SHA-256 values with metadata",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.apply and args.limit is not None:
        parser.error("--apply cannot be combined with --limit")
    if args.verify_hashes:
        args.verify = True
    if args.verify and (args.apply or args.restore):
        parser.error("--verify cannot be combined with --apply or --restore")
    if args.dedupe_exact and (
        args.verify or args.restore or args.dedupe_near or args.limit is not None
    ):
        parser.error(
            "--dedupe-exact cannot be combined with --verify, --restore, --dedupe-near, or --limit"
        )
    if args.dedupe_near and (
        args.verify or args.restore or args.dedupe_exact or args.limit is not None
    ):
        parser.error(
            "--dedupe-near cannot be combined with --verify, --restore, --dedupe-exact, or --limit"
        )
    if args.reject_list and (
        args.verify
        or args.restore
        or args.dedupe_exact
        or args.dedupe_near
        or args.review_quarantine
        or args.limit is not None
    ):
        parser.error(
            "--reject-list cannot be combined with --verify, --restore, --dedupe-exact, or --limit"
        )
    if args.review_quarantine and (
        args.verify or args.restore or args.dedupe_exact or args.dedupe_near
    ):
        parser.error(
            "--review-quarantine cannot be combined with --verify, --restore, --dedupe-exact, or --dedupe-near"
        )
    if args.review_quarantine and args.apply:
        parser.error("--review-quarantine is report-only and cannot be combined with --apply")
    if args.review_labels and not args.review_quarantine:
        parser.error("--review-labels requires --review-quarantine")
    if bool(args.export_review_list) != bool(args.export_output):
        parser.error("--export-review-list and --export-output must be supplied together")
    if args.export_review_list and not args.review_quarantine:
        parser.error("--export-review-list requires --review-quarantine")
    if args.export_review_list and (args.review_labels or args.review_limit is not None):
        parser.error("review export cannot be combined with --review-labels or --review-limit")
    if args.review_limit is not None and args.review_limit <= 0:
        parser.error("--review-limit must be positive")
    if args.restore:
        report = DatasetImageFilter.restore(args.dataset, args.restore)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.verify:
        report = DatasetImageFilter.verify(
            args.dataset,
            run_id=args.run_id,
            verify_hashes=args.verify_hashes,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.dedupe_exact:
        report = DatasetImageFilter.run_exact_dedupe(
            args.dataset,
            apply=args.apply,
            run_id=args.run_id,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.dedupe_near:
        report = DatasetImageFilter.run_near_dedupe(
            args.dataset,
            max_distance=args.near_distance,
            max_aspect_delta=args.near_aspect_delta,
            max_color_distance=args.near_color_distance,
            apply=args.apply,
            run_id=args.run_id,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.reject_list:
        with open(args.reject_list, encoding="utf-8") as handle:
            reject_paths = [
                line.strip()
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            ]
        report = DatasetImageFilter.run_review_rejections(
            args.dataset,
            reject_paths,
            apply=args.apply,
            run_id=args.run_id,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.review_quarantine:
        from .dataset_review import (
            BLIPITMReviewer,
            BLIPReviewThresholds,
            export_review_candidates,
            run_quarantine_review,
        )

        if args.export_review_list:
            with open(args.export_review_list, encoding="utf-8") as handle:
                approved_paths = [
                    line.strip()
                    for line in handle
                    if line.strip() and not line.lstrip().startswith("#")
                ]
            report = export_review_candidates(
                args.dataset,
                args.review_quarantine,
                approved_paths,
                args.export_output,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        reviewer = BLIPITMReviewer(
            model_name=args.blip_model,
            device=args.device,
            batch_size=args.batch_size,
            cache_dir=args.blip_cache_dir,
            local_files_only=args.blip_local_only,
        )
        report = run_quarantine_review(
            args.dataset,
            args.review_quarantine,
            reviewer,
            thresholds=BLIPReviewThresholds(
                min_positive=args.blip_min_positive,
                min_content_evidence=args.blip_min_content,
                min_margin=args.blip_min_margin,
            ),
            candidate_limit=args.review_limit,
            labels_path=args.review_labels,
            run_id=args.run_id,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    prompts = PromptSet.truck_loading(args.query)
    scorer = CLIPPromptScorer(
        prompts,
        model_name=args.model,
        device=args.device,
        cache_dir=args.cache_dir,
    )
    analyzer = VisualSignalAnalyzer(
        enable_ocr=args.ocr or args.ocr_all,
        ocr_all=args.ocr_all,
    )
    base = FilterThresholds.from_profile(args.profile)
    thresholds = FilterThresholds(
        min_relevance=base.min_relevance if args.min_relevance is None else args.min_relevance,
        mismatch_margin=base.mismatch_margin if args.mismatch_margin is None else args.mismatch_margin,
        advertisement_score=base.advertisement_score if args.advertisement_score is None else args.advertisement_score,
        advertisement_margin=base.advertisement_margin if args.advertisement_margin is None else args.advertisement_margin,
        advertisement_support_score=(
            base.advertisement_support_score
            if args.advertisement_support_score is None
            else args.advertisement_support_score
        ),
        advertisement_support_margin=(
            base.advertisement_support_margin
            if args.advertisement_support_margin is None
            else args.advertisement_support_margin
        ),
        text_area_ratio=base.text_area_ratio if args.text_area_ratio is None else args.text_area_ratio,
        minimum_scene_evidence=(
            base.minimum_scene_evidence
            if args.minimum_scene_evidence is None
            else args.minimum_scene_evidence
        ),
        scene_evidence_margin=(
            base.scene_evidence_margin
            if args.scene_evidence_margin is None
            else args.scene_evidence_margin
        ),
    )
    if not 0.0 <= thresholds.text_area_ratio <= 1.0:
        parser.error("--text-area-ratio must be between 0 and 1")
    policy = DecisionPolicy(thresholds)
    dataset_filter = DatasetImageFilter(
        args.dataset,
        scorer,
        analyzer,
        policy=policy,
        batch_size=args.batch_size,
    )
    report = dataset_filter.run(limit=args.limit, apply=args.apply, run_id=args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
