# coding=utf-8
"""Command line for annotation-package export, YOLO import, and review handoff."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Sequence

from .annotation_contracts import ReviewDecision, apply_review_decisions
from .annotation_yolo import (
    export_annotation_task,
    export_reviewed_yolo_labels,
    import_yolo_annotations,
    read_annotation_records,
    write_annotation_records,
)


def _read_split_map(path: str | None) -> dict[str, str] | None:
    if not path:
        return None
    value = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("split map must be a JSON object from sample IDs to split names")
    return value


def _read_decisions(path: str) -> list[ReviewDecision]:
    decisions = []
    with Path(path).expanduser().open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid review decision JSON at line {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"review decision at line {line_number} must be an object")
            decisions.append(ReviewDecision(
                sample_id=str(value.get("sample_id") or ""),
                asset_sha256=str(value.get("asset_sha256") or ""),
                expected_revision=value.get("expected_revision", -1),
                decision=str(value.get("decision") or ""),
                reviewer=str(value.get("reviewer") or ""),
                reason=str(value.get("reason") or ""),
                decided_at=str(value.get("decided_at") or ""),
            ))
    if not decisions:
        raise ValueError("review decisions file is empty")
    return decisions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-spider-annotations",
        description="Create verified annotation tasks and exchange reviewed YOLO labels.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export_task = commands.add_parser("export-task", help="copy verified manifest images into a review package")
    export_task.add_argument("--manifest", action="append", required=True, help="manifest.jsonl; repeat for per-class manifests")
    export_task.add_argument("--ontology", required=True, help="versioned ontology JSON")
    export_task.add_argument("--output", required=True, help="new output directory; existing paths are refused")
    export_task.add_argument("--split-map", help="optional JSON object of sample_id to stable split assignment")
    export_task.add_argument("--max-asset-bytes", type=int, default=64 * 1024 * 1024)
    export_task.add_argument("--max-pixels", type=int, default=100_000_000)

    import_yolo = commands.add_parser("import-yolo", help="import YOLO labels as pending annotation records")
    import_yolo.add_argument("--package", required=True, help="annotation package created by export-task")
    import_yolo.add_argument("--labels", help="YOLO labels root mirroring images/all/<sample>.txt")
    import_yolo.add_argument("--output", required=True, help="new annotation JSONL ledger")

    review = commands.add_parser("review", help="apply revision-fenced approval or rejection decisions")
    review.add_argument("--annotations", required=True, help="annotation JSONL ledger")
    review.add_argument("--decisions", required=True, help="JSONL decisions with reviewer and expected_revision")
    review.add_argument("--output", required=True, help="new annotation JSONL ledger containing the audit event")

    export_labels = commands.add_parser("export-reviewed", help="export YOLO labels after explicit review approval")
    export_labels.add_argument("--annotations", required=True, help="approved annotation JSONL ledger")
    export_labels.add_argument("--output", required=True, help="new label handoff directory")
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "export-task":
        return export_annotation_task(
            args.manifest,
            args.ontology,
            args.output,
            split_assignments=_read_split_map(args.split_map),
            max_asset_bytes=args.max_asset_bytes,
            max_pixels=args.max_pixels,
        )
    if args.command == "import-yolo":
        records = import_yolo_annotations(args.package, labels_dir=args.labels)
        output = write_annotation_records(records, args.output)
        return {
            "output": str(output),
            "sample_count": len(records),
            "pending_review": sum(record.review_status == "pending" for record in records),
            "annotation_approved": False,
        }
    if args.command == "review":
        records = read_annotation_records(args.annotations)
        reviewed = apply_review_decisions(records, _read_decisions(args.decisions))
        output = write_annotation_records(reviewed, args.output)
        return {
            "output": str(output),
            "sample_count": len(reviewed),
            "approved": sum(record.review_status == "approved" for record in reviewed),
            "rejected": sum(record.review_status == "rejected" for record in reviewed),
            "still_pending": sum(record.review_status == "pending" for record in reviewed),
        }
    if args.command == "export-reviewed":
        return export_reviewed_yolo_labels(
            read_annotation_records(args.annotations),
            args.output,
        )
    raise ValueError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = _run(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
