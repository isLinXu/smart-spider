"""Batch DINO-X Pro detection labeling for local image datasets.

The labeler keeps source images untouched and writes resumable JSONL, COCO, and
YOLO-style outputs.  The DINO-X runtime lives in the local model-deploy tree
because it is intentionally an optional dependency of smart-spider.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DEFAULT_LABEL = "货车车尾"


def _load_runtime(model_deploy: Path):
    model_deploy = model_deploy.expanduser().resolve()
    if not model_deploy.is_dir():
        raise FileNotFoundError(f"model-deploy directory does not exist: {model_deploy}")
    sys.path.insert(0, str(model_deploy))
    from dino_x_inference.image import preprocess_image
    from dino_x_inference.postprocess import decode_detections
    from dino_x_inference.runtime import (
        build_and_load_model,
        move_inputs,
        precision_policy,
    )
    from dino_x_inference.text import load_prompt_cache

    return (
        build_and_load_model,
        move_inputs,
        precision_policy,
        preprocess_image,
        decode_detections,
        load_prompt_cache,
    )


def discover_images(
    roots: Sequence[Path], include_quarantine: bool = False
) -> list[tuple[Path, Path, str]]:
    """Return sorted ``(root, image, source_id)`` records without duplicates."""

    records: list[tuple[Path, Path, str]] = []
    seen: set[Path] = set()
    for raw_root in roots:
        root = raw_root.expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"dataset directory does not exist: {root}")
        for image in sorted(root.rglob("*")):
            if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            relative = image.relative_to(root)
            parts = relative.parts
            if any(part.startswith("_review") for part in parts):
                continue
            if not include_quarantine and any(
                part.startswith("_quarantine") for part in parts
            ):
                continue
            resolved = image.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            source_id = f"{root.name}/{relative.as_posix()}"
            records.append((root, resolved, source_id))
    records.sort(key=lambda item: item[2])
    return records


def _safe_label_path(output_root: Path, root: Path, image: Path) -> Path:
    relative = image.relative_to(root).with_suffix(".txt")
    return output_root / "labels" / root.name / relative


def _yolo_line(detection: Mapping[str, Any], width: int, height: int) -> str:
    x0, y0, x1, y1 = [float(value) for value in detection["bbox_xyxy"]]
    x0 = min(max(x0, 0.0), float(width))
    x1 = min(max(x1, 0.0), float(width))
    y0 = min(max(y0, 0.0), float(height))
    y1 = min(max(y1, 0.0), float(height))
    box_width = max(0.0, x1 - x0)
    box_height = max(0.0, y1 - y0)
    center_x = (x0 + x1) / 2.0 / max(width, 1)
    center_y = (y0 + y1) / 2.0 / max(height, 1)
    normalized_width = box_width / max(width, 1)
    normalized_height = box_height / max(height, 1)
    return "0 %.6f %.6f %.6f %.6f\n" % (
        center_x,
        center_y,
        normalized_width,
        normalized_height,
    )


def _coco_annotation(
    annotation_id: int,
    image_id: int,
    detection: Mapping[str, Any],
) -> dict[str, Any]:
    x0, y0, x1, y1 = [float(value) for value in detection["bbox_xyxy"]]
    width = max(0.0, x1 - x0)
    height = max(0.0, y1 - y0)
    return {
        "id": annotation_id,
        "image_id": image_id,
        "category_id": 1,
        "bbox": [x0, y0, width, height],
        "area": width * height,
        "iscrowd": 0,
        "score": float(detection["score"]),
    }


def _jsonl_paths(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from error
            source_id = row.get("source_id")
            if isinstance(source_id, str) and row.get("status") == "ok":
                completed.add(source_id)
    return completed


def _choose_device(requested: str) -> str:
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _choose_precision(requested: str, device: str) -> str:
    if requested != "auto":
        return requested
    return "bf16" if device == "cuda" else "fp32"


def _default_roots(project_root: Path) -> list[Path]:
    candidates = sorted(
        path
        for path in project_root.iterdir()
        if path.is_dir()
        and (
            path.name.startswith("dataset_truck_loading_area_door_operation")
            or path.name.startswith("output_truck_loading_area_door_operation")
        )
    )
    if not candidates:
        raise FileNotFoundError("no truck-loading-area image directories found")
    return candidates


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    import torch

    project_root = Path(args.project_root).expanduser().resolve()
    roots = (
        [Path(value).expanduser().resolve() for value in args.dataset]
        if args.dataset
        else _default_roots(project_root)
    )
    records = discover_images(roots, include_quarantine=args.include_quarantine)
    if args.limit is not None:
        records = records[: args.limit]
    if not records:
        raise ValueError("no image files selected")

    output_root = Path(args.output).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_root / "detections.jsonl"
    completed = _jsonl_paths(jsonl_path)

    build_and_load_model, move_inputs, precision_policy, preprocess_image, decode_detections, load_prompt_cache = _load_runtime(Path(args.model_deploy))
    checkpoint = Path(args.checkpoint).expanduser().resolve()
    prompt_cache = Path(args.prompt_cache).expanduser().resolve()
    if not checkpoint.is_file():
        raise FileNotFoundError(f"checkpoint does not exist: {checkpoint}")
    if not prompt_cache.is_file():
        raise FileNotFoundError(f"prompt cache does not exist: {prompt_cache}")

    device = _choose_device(args.device)
    precision = _choose_precision(args.precision, device)
    prompt_payload = load_prompt_cache(
        prompt_cache,
        expected_model="pro",
        expected_prompt=args.label,
    )
    if prompt_payload["metadata"]["phrases"] != [args.label]:
        raise ValueError("prompt cache must contain exactly the requested label")

    print(f"加载 DINO-X Pro：{checkpoint}", flush=True)
    load_started = time.perf_counter()
    model, checkpoint_load = build_and_load_model(
        "pro", checkpoint, queries=args.queries
    )
    model.eval().to(device)
    load_seconds = time.perf_counter() - load_started
    print(
        f"模型已加载 device={device} precision={precision} queries={args.queries} "
        f"耗时={load_seconds:.1f}s，待处理={len(records)}，已完成={len(completed)}",
        flush=True,
    )

    run_config = {
        "schema": "dinox_pro.label_run.v1",
        "label": args.label,
        "model": "pro",
        "checkpoint": str(checkpoint),
        "prompt_cache": str(prompt_cache),
        "model_deploy": str(Path(args.model_deploy).expanduser().resolve()),
        "device": device,
        "precision": precision,
        "size": args.size,
        "queries": args.queries,
        "box_threshold": args.box_threshold,
        "nms_iou_threshold": args.nms_iou_threshold,
        "include_quarantine": args.include_quarantine,
        "selected_images": len(records),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checkpoint_load": checkpoint_load,
    }
    _write_json(output_root / "run_config.json", run_config)
    (output_root / "classes.txt").write_text(args.label + "\n", encoding="utf-8")

    processed = 0
    skipped = 0
    errors = 0
    detections_total = 0
    annotation_id = 1
    image_id = 1
    coco_images: list[dict[str, Any]] = []
    coco_annotations: list[dict[str, Any]] = []
    if (output_root / "coco_annotations.json").exists():
        existing = json.loads((output_root / "coco_annotations.json").read_text(encoding="utf-8"))
        coco_images = list(existing.get("images", []))
        coco_annotations = list(existing.get("annotations", []))
        image_id = max([int(row["id"]) for row in coco_images] or [0]) + 1
        annotation_id = max([int(row["id"]) for row in coco_annotations] or [0]) + 1
    else:
        # Rebuild the COCO index from a previous interrupted JSONL run.
        for previous in _iter_jsonl(jsonl_path):
            if previous.get("status") != "ok" or "width" not in previous:
                continue
            coco_images.append(
                {
                    "id": image_id,
                    "file_name": previous["source_id"],
                    "source_path": previous["source_path"],
                    "width": int(previous["width"]),
                    "height": int(previous["height"]),
                }
            )
            for detection in previous.get("detections", []):
                coco_annotations.append(
                    _coco_annotation(
                        image_id=image_id,
                        annotation_id=annotation_id,
                        detection=detection,
                    )
                )
                annotation_id += 1
            image_id += 1

    pending = [record for record in records if record[2] not in completed]
    skipped = len(records) - len(pending)
    batch_size = max(1, int(args.batch_size))
    with jsonl_path.open("a", encoding="utf-8") as jsonl:
        started = time.perf_counter()

        def write_row(row: dict[str, Any]) -> None:
            jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
            jsonl.flush()

        def report_progress() -> None:
            done = processed + errors
            if done and done % args.progress_every == 0:
                elapsed = time.perf_counter() - started
                rate = done / max(elapsed, 1e-6)
                remaining = max(0, len(pending) - done)
                eta_minutes = remaining / max(rate, 1e-6) / 60.0
                print(
                    f"进度 {done + skipped}/{len(records)}，成功={processed} "
                    f"检出框={detections_total} 错误={errors} "
                    f"速度={rate:.2f}张/s ETA={eta_minutes:.1f}min",
                    flush=True,
                )

        def write_error(root: Path, image_path: Path, source_id: str, error: Exception) -> None:
            nonlocal errors
            label_path = _safe_label_path(output_root, root, image_path)
            label_path.parent.mkdir(parents=True, exist_ok=True)
            label_path.write_text("", encoding="utf-8")
            write_row(
                {
                    "source_id": source_id,
                    "source_path": str(image_path),
                    "label": args.label,
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                    "yolo_label": str(label_path.relative_to(output_root)),
                }
            )
            errors += 1
            report_progress()

        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start : batch_start + batch_size]
            valid: list[tuple[Path, Path, str, dict[str, Any]]] = []
            for root, image_path, source_id in batch:
                try:
                    valid.append((root, image_path, source_id, preprocess_image(image_path, target_size=args.size)))
                except Exception as error:
                    write_error(root, image_path, source_id, error)
            if not valid:
                continue
            try:
                samples = torch.cat([item[3]["samples"] for item in valid], dim=0)
                masks = torch.cat([item[3]["masks"] for item in valid], dim=0)
                tensors = prompt_payload["tensors"]
                prompt_inputs = (
                    tensors["encoded_text"].repeat(len(valid), 1, 1),
                    tensors["text_token_mask"].repeat(len(valid), 1),
                    tensors["position_ids"].repeat(len(valid), 1),
                    tensors["text_self_attention_masks"].repeat(len(valid), 1, 1),
                )
                inputs = (samples.to(device), masks.to(device)) + tuple(value.to(device) for value in prompt_inputs)
                with torch.inference_mode(), precision_policy(precision, device):
                    output = model(*inputs)
                outputs = {
                    key: value.detach().cpu() if hasattr(value, "detach") else value
                    for key, value in output.items()
                }
            except Exception as batch_error:
                # Keep the run resumable. A smaller batch can be requested on
                # the next invocation if the selected device runs out of memory.
                print(f"批量推理失败，当前批次记为错误：{type(batch_error).__name__}: {batch_error}", flush=True)
                for root, image_path, source_id, _ in valid:
                    write_error(root, image_path, source_id, batch_error)
                continue
            for index, (root, image_path, source_id, image_data) in enumerate(valid):
                row: dict[str, Any] = {
                    "source_id": source_id,
                    "source_path": str(image_path),
                    "label": args.label,
                    "status": "ok",
                }
                label_path = _safe_label_path(output_root, root, image_path)
                label_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    one_output = {key: value[index : index + 1] for key, value in outputs.items()}
                    detections, diagnostics = decode_detections(
                        one_output,
                        prompt_payload["metadata"]["phrases"],
                        image_data["original_size"],
                        box_threshold=args.box_threshold,
                        nms_iou_threshold=args.nms_iou_threshold,
                        return_diagnostics=True,
                    )
                    height, width = image_data["original_size"]
                    label_path.write_text(
                        "".join(_yolo_line(detection, width, height) for detection in detections),
                        encoding="utf-8",
                    )
                    row.update({
                        "width": width,
                        "height": height,
                        "detections": detections,
                        "diagnostics": diagnostics,
                        "yolo_label": str(label_path.relative_to(output_root)),
                    })
                    coco_images.append({
                        "id": image_id,
                        "file_name": source_id,
                        "source_path": str(image_path),
                        "width": width,
                        "height": height,
                    })
                    for detection in detections:
                        coco_annotations.append(_coco_annotation(image_id=image_id, annotation_id=annotation_id, detection=detection))
                        annotation_id += 1
                    image_id += 1
                    detections_total += len(detections)
                    processed += 1
                    write_row(row)
                    report_progress()
                except Exception as error:
                    write_error(root, image_path, source_id, error)
            if device == "mps":
                torch.mps.empty_cache()

    coco = {
        "info": {
            "description": "货车装卸区开关门 / DINO-X Pro detection labels",
            "version": "1.0",
            "generator": "smart_spider.dinox_pro_labeler",
        },
        "images": coco_images,
        "annotations": coco_annotations,
        "categories": [{"id": 1, "name": args.label, "supercategory": "none"}],
    }
    _write_json(output_root / "coco_annotations.json", coco)
    summary = {
        "schema": "dinox_pro.label_summary.v1",
        "output": str(output_root),
        "selected": len(records),
        "processed": processed,
        "skipped_existing": skipped,
        "errors": errors,
        "images_with_detections": sum(
            1
            for row in _iter_jsonl(jsonl_path)
            if row.get("status") == "ok" and row.get("detections")
        ),
        "detections": len(coco_annotations),
        "device": device,
        "precision": precision,
        "checkpoint": str(checkpoint),
        "prompt": args.label,
    }
    _write_json(output_root / "summary.json", summary)
    return summary


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用本地 DINO-X Pro 权重批量生成检测标注")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--dataset", action="append", help="数据目录；可重复指定，默认自动发现货车数据目录")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--prompt-cache", required=True)
    parser.add_argument("--model-deploy", required=True, help="本地 dino_x_inference/model-deploy 根目录")
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--device", choices=("auto", "cpu", "mps", "cuda"), default="auto")
    parser.add_argument("--precision", choices=("auto", "fp32", "bf16", "fp16"), default="auto")
    parser.add_argument("--size", type=int, default=800)
    parser.add_argument("--queries", type=int, default=900)
    parser.add_argument("--batch-size", type=int, default=4, help="模型批量推理大小；MPS 内存不足时使用 1")
    parser.add_argument("--box-threshold", type=float, default=0.25)
    parser.add_argument("--nms-iou-threshold", type=float, default=0.3)
    parser.add_argument("--include-quarantine", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--progress-every", type=int, default=25)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.size <= 0 or args.size % 32:
        raise SystemExit("--size must be a positive multiple of 32")
    if args.queries <= 0 or args.batch_size <= 0 or args.limit is not None and args.limit <= 0:
        raise SystemExit("--queries, --batch-size and --limit must be positive")
    if args.progress_every <= 0:
        raise SystemExit("--progress-every must be positive")
    summary = run(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
