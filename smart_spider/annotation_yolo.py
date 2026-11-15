# coding=utf-8
"""Safe offline exchange between image manifests and YOLO annotation files.

Annotation-task exports contain verified images but deliberately contain no
empty label files. An empty YOLO label is a reviewed background only after an
explicit human decision; treating every unannotated image as a negative would
silently poison a training set.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import warnings
from typing import Any

from PIL import Image

from .annotation_contracts import AnnotationRecord, BoundingBox, ontology_fingerprint


_IMAGE_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_SPLITS = {"train": "train", "val": "validation", "validation": "validation", "test": "test"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_LABEL_BYTES = 8 * 1024 * 1024
_MAX_BOXES_PER_IMAGE = 100_000


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path.name}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"expected an object at {path.name}:{line_number}")
            yield line_number, row


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [row for _, row in _iter_jsonl(path)]


def _file_digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _safe_relative_path(raw: Any, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    posix = PurePosixPath(raw.replace("\\", "/"))
    native = Path(raw)
    if posix.is_absolute() or native.is_absolute() or ".." in posix.parts:
        raise ValueError(f"{field} must stay inside its dataset root")
    return Path(*posix.parts)


def _image_facts(path: Path, *, max_bytes: int, max_pixels: int) -> tuple[bytes, str, int, int, str]:
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError(f"image exceeds byte limit: {path.name}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                image.verify()
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
                image_format = str(image.format or "").upper()
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError(f"invalid or unsafe image: {path.name}") from exc
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ValueError(f"image dimensions exceed limit: {path.name}")
    if image_format not in _IMAGE_EXTENSIONS:
        raise ValueError(f"unsupported image format {image_format!r}: {path.name}")
    return content, _file_digest(content), width, height, image_format


def _normalize_ontology(raw: Mapping[str, Any]) -> dict[str, Any]:
    source_classes = raw.get("classes")
    if not isinstance(source_classes, list) or not source_classes:
        raise ValueError("ontology must define a non-empty classes list")
    classes = []
    for index, item in enumerate(source_classes):
        if isinstance(item, str):
            class_id, name, definition = index, item.strip(), ""
        elif isinstance(item, Mapping):
            class_id = item.get("id", index)
            name = str(item.get("name") or "").strip()
            definition = str(item.get("definition") or "").strip()
        else:
            raise ValueError(f"invalid ontology class at index {index}")
        if isinstance(class_id, bool) or not isinstance(class_id, int) or class_id != index:
            raise ValueError("ontology class IDs must be contiguous and ordered from zero")
        if not name:
            raise ValueError(f"ontology class {index} has no name")
        classes.append({"id": index, "name": name, "definition": definition})
    names = [item["name"] for item in classes]
    if len(set(names)) != len(names):
        raise ValueError("ontology class names must be unique")
    material = {
        "ontology_id": str(raw.get("ontology_id") or raw.get("task_type") or "object_detection"),
        "ontology_version": str(raw.get("ontology_version") or raw.get("schema_version") or "1"),
        "classes": classes,
    }
    if not material["ontology_id"].strip() or not material["ontology_version"].strip():
        raise ValueError("ontology ID and version are required")
    class_names = [item["name"] for item in classes]
    class_definitions = [item["definition"] for item in classes]
    return {
        **material,
        "fingerprint": ontology_fingerprint(
            material["ontology_id"],
            material["ontology_version"],
            class_names,
            class_definitions,
        ),
    }


def _load_ontology(source: str | os.PathLike[str] | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(source, Mapping):
        raw = source
    else:
        try:
            raw = json.loads(Path(source).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read ontology: {source}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("ontology must be a JSON object")
    return _normalize_ontology(raw)


def _read_verified_manifest_asset(manifest: Path, row: Mapping[str, Any], *, max_bytes: int, max_pixels: int):
    sample_id = str(row.get("id") or row.get("sample_id") or "").strip()
    if not sample_id:
        raise ValueError(f"sample ID missing in {manifest.name}")
    relative = _safe_relative_path(row.get("file") or row.get("file_path"), field="manifest file")
    root = manifest.parent.resolve()
    asset = (root / relative).resolve()
    try:
        asset.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"manifest asset escapes its root: {relative}") from exc
    content, actual, width, height, image_format = _image_facts(
        asset, max_bytes=max_bytes, max_pixels=max_pixels
    )
    expected = row.get("sha256")
    if not expected and sample_id.startswith("sha256:"):
        expected = sample_id.removeprefix("sha256:")
    if isinstance(expected, str) and expected.startswith("sha256:"):
        expected = expected.removeprefix("sha256:")
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise ValueError(f"manifest has no valid SHA-256 for sample {sample_id}")
    if actual != expected:
        raise ValueError(f"manifest asset hash mismatch for sample {sample_id}")
    return sample_id, asset, actual, width, height, image_format, content


def _sample_stem(sample_id: str) -> str:
    return sha256(sample_id.encode("utf-8")).hexdigest()


def _write_new_directory(target: Path, writer) -> None:
    target = target.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        writer(temporary)
        os.replace(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def export_annotation_task(
    manifest_paths: Sequence[str | os.PathLike[str]],
    ontology: str | os.PathLike[str] | Mapping[str, Any],
    output_dir: str | os.PathLike[str],
    *,
    split_assignments: Mapping[str, str] | None = None,
    max_asset_bytes: int = 64 * 1024 * 1024,
    max_pixels: int = 100_000_000,
) -> dict[str, Any]:
    """Copy verified local images into an annotation-only package.

    ``manifest_paths`` may contain several per-class manifests. Identical
    sample IDs are deduplicated by their verified bytes and keep all source
    manifest references. No empty YOLO labels are created.
    """
    if not manifest_paths:
        raise ValueError("at least one manifest path is required")
    if max_asset_bytes <= 0 or max_pixels <= 0:
        raise ValueError("image limits must be positive")
    ontology_record = _load_ontology(ontology)
    manifests = [Path(value).expanduser().resolve(strict=True) for value in manifest_paths]
    if any(not path.is_file() for path in manifests):
        raise FileNotFoundError("one or more manifest paths are not files")
    requested_splits = dict(split_assignments or {})
    invalid_splits = {
        sample_id: value for sample_id, value in requested_splits.items()
        if value not in _SPLITS
    }
    if invalid_splits:
        raise ValueError(f"unsupported split assignments: {invalid_splits}")

    grouped: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        for line_number, row in _iter_jsonl(manifest):
            sample_id, asset, digest, width, height, image_format, _ = _read_verified_manifest_asset(
                manifest, row, max_bytes=max_asset_bytes, max_pixels=max_pixels
            )
            split = _SPLITS.get(requested_splits.get(sample_id), "")
            manifest_ref = "/".join(manifest.parts[-2:])
            sources = {"manifest": manifest_ref, "line": line_number}
            current = grouped.get(sample_id)
            if current is None:
                grouped[sample_id] = {
                    "sample_id": sample_id,
                    "source_path": asset,
                    "asset_sha256": digest,
                    "image_width": width,
                    "image_height": height,
                    "image_format": image_format,
                    "split": split,
                    "sources": [sources],
                }
            else:
                if current["asset_sha256"] != digest:
                    raise ValueError(f"duplicate sample ID has different bytes: {sample_id}")
                if current["split"] != split:
                    raise ValueError(f"duplicate sample ID has conflicting split assignments: {sample_id}")
                if sources not in current["sources"]:
                    current["sources"].append(sources)
    unknown_split_ids = sorted(set(requested_splits) - set(grouped))
    if unknown_split_ids:
        raise ValueError(f"split assignments reference unknown samples: {unknown_split_ids[:5]}")
    if not grouped:
        raise ValueError("manifests contain no image samples")

    rows = []
    for sample_id in sorted(grouped):
        item = grouped[sample_id]
        row = {key: value for key, value in item.items() if key != "source_path"}
        row["image_file"] = f"images/all/{_sample_stem(sample_id)}{_IMAGE_EXTENSIONS[item['image_format']]}"
        row["sources"].sort(key=lambda value: (value["manifest"], value["line"]))
        row["status"] = "pending_annotation"
        row["training_ready"] = False
        rows.append(row)

    output = Path(output_dir).expanduser().resolve()

    def build(temporary: Path) -> None:
        (temporary / "images" / "all").mkdir(parents=True)
        for row in rows:
            source_entry = grouped[row["sample_id"]]
            content, digest, width, height, image_format = _image_facts(
                source_entry["source_path"], max_bytes=max_asset_bytes, max_pixels=max_pixels
            )
            if (
                digest != row["asset_sha256"]
                or width != row["image_width"]
                or height != row["image_height"]
                or image_format != source_entry["image_format"]
            ):
                raise ValueError(f"source asset changed during export: {row['sample_id']}")
            (temporary / row["image_file"]).write_bytes(content)
        (temporary / "ontology.json").write_bytes(_json_bytes(ontology_record))
        _write_jsonl(temporary / "samples.jsonl", rows)
        package = {
            "format_version": 1,
            "purpose": "human_annotation",
            "training_ready": False,
            "annotation_labels_created": False,
            "ontology_fingerprint": ontology_record["fingerprint"],
            "sample_count": len(rows),
            "sidecars": {
                "ontology.json": _file_digest((temporary / "ontology.json").read_bytes()),
                "samples.jsonl": _file_digest((temporary / "samples.jsonl").read_bytes()),
            },
        }
        (temporary / "package.json").write_bytes(_json_bytes(package))

    _write_new_directory(output, build)
    return {
        "output_dir": str(output),
        "sample_count": len(rows),
        "duplicate_sources_merged": sum(len(row["sources"]) - 1 for row in rows),
        "ontology_fingerprint": ontology_record["fingerprint"],
        "training_ready": False,
    }


def _load_annotation_package(package_dir: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    try:
        package = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        ontology_raw = json.loads((package_dir / "ontology.json").read_text(encoding="utf-8"))
        samples = _read_jsonl(package_dir / "samples.jsonl")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or incomplete annotation package") from exc
    if not isinstance(package, dict) or package.get("format_version") != 1:
        raise ValueError("unsupported annotation package format")
    if package.get("purpose") != "human_annotation" or package.get("training_ready") is not False:
        raise ValueError("input is not a non-training annotation package")
    sidecars = package.get("sidecars") or {}
    if not isinstance(sidecars, Mapping):
        raise ValueError("invalid annotation package sidecar checksums")
    for name in ("ontology.json", "samples.jsonl"):
        if sidecars.get(name) != _file_digest((package_dir / name).read_bytes()):
            raise ValueError(f"annotation package sidecar changed: {name}")
    if not isinstance(ontology_raw, Mapping):
        raise ValueError("annotation package ontology must be an object")
    ontology = _normalize_ontology(ontology_raw)
    if ontology["fingerprint"] != package.get("ontology_fingerprint"):
        raise ValueError("annotation package ontology fingerprint mismatch")
    if package.get("sample_count") != len(samples) or not samples:
        raise ValueError("annotation package sample count mismatch or package is empty")
    return package, ontology, samples


def _parse_yolo_file(path: Path, class_count: int) -> tuple[BoundingBox, ...]:
    if path.stat().st_size > _MAX_LABEL_BYTES:
        raise ValueError(f"YOLO label file exceeds size limit: {path.name}")
    boxes = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 5:
                raise ValueError(f"expected five YOLO fields at {path.name}:{line_number}")
            try:
                class_id = int(fields[0])
                coordinates = [float(value) for value in fields[1:]]
            except ValueError as exc:
                raise ValueError(f"invalid YOLO value at {path.name}:{line_number}") from exc
            if len(boxes) >= _MAX_BOXES_PER_IMAGE:
                raise ValueError(f"YOLO label file exceeds box-count limit: {path.name}")
            box = BoundingBox(class_id, *coordinates)
            try:
                box.validate(class_count)
            except ValueError as exc:
                raise ValueError(f"invalid box at {path.name}:{line_number}: {exc}") from exc
            boxes.append(box)
    return tuple(boxes)


def import_yolo_annotations(
    package_dir: str | os.PathLike[str],
    *,
    labels_dir: str | os.PathLike[str] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    max_asset_bytes: int = 64 * 1024 * 1024,
    max_pixels: int = 100_000_000,
) -> list[AnnotationRecord]:
    """Import YOLO ``class cx cy width height`` files as pending review records.

    Label directory layout mirrors the task package's ``images`` directory,
    replacing ``images`` with ``labels`` and each image suffix with ``.txt``.
    Empty label files are imported as empty annotations but remain pending.
    """
    root = Path(package_dir).expanduser().resolve(strict=True)
    _, ontology, samples = _load_annotation_package(root)
    label_root = Path(labels_dir).expanduser().resolve() if labels_dir else root / "labels"
    if not label_root.is_dir():
        raise FileNotFoundError(f"YOLO labels directory not found: {label_root}")
    class_names = tuple(item["name"] for item in ontology["classes"])
    records: list[AnnotationRecord] = []
    expected_label_paths: set[Path] = set()
    sample_ids: set[str] = set()
    for sample in samples:
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id or sample_id in sample_ids:
            raise ValueError("annotation package has missing or duplicate sample IDs")
        if sample.get("status") != "pending_annotation" or sample.get("training_ready") is not False:
            raise ValueError(f"sample is not an annotation-only candidate: {sample_id}")
        sample_ids.add(sample_id)
        image_relative = _safe_relative_path(sample.get("image_file"), field="package image_file")
        if not image_relative.parts or image_relative.parts[0] != "images":
            raise ValueError("package image_file must be under images/")
        image_path = (root / image_relative).resolve()
        try:
            image_path.relative_to(root)
        except ValueError as exc:
            raise ValueError("package image escapes package root") from exc
        content, digest, width, height, _ = _image_facts(
            image_path, max_bytes=max_asset_bytes, max_pixels=max_pixels
        )
        if digest != sample.get("asset_sha256"):
            raise ValueError(f"package image hash mismatch for {sample_id}")
        if width != sample.get("image_width") or height != sample.get("image_height"):
            raise ValueError(f"package image dimensions changed for {sample_id}")
        del content
        label_relative = Path(*image_relative.parts[1:]).with_suffix(".txt")
        label_path = (label_root / label_relative).resolve()
        try:
            label_path.relative_to(label_root)
        except ValueError as exc:
            raise ValueError("YOLO label path escapes labels root") from exc
        expected_label_paths.add(label_path)
        if not label_path.is_file():
            raise FileNotFoundError(f"YOLO label file missing for {sample_id}: {label_path}")
        boxes = _parse_yolo_file(label_path, len(class_names))
        records.append(AnnotationRecord(
            sample_id=sample_id,
            asset_sha256=digest,
            image_width=width,
            image_height=height,
            ontology_id=ontology["ontology_id"],
            ontology_version=ontology["ontology_version"],
            class_names=class_names,
            ontology_fingerprint=ontology["fingerprint"],
            class_definitions=tuple(item["definition"] for item in ontology["classes"]),
            annotations=boxes,
        ))

    for label_path in label_root.rglob("*.txt"):
        if label_path.name == "classes.txt":
            continue
        if label_path.resolve() not in expected_label_paths:
            raise ValueError(f"YOLO labels contain an unknown sample: {label_path}")
    if output_path is not None:
        write_annotation_records(records, output_path)
    return records


def write_annotation_records(
    records: Sequence[AnnotationRecord],
    output_path: str | os.PathLike[str],
) -> Path:
    """Create an annotation JSONL once; never silently overwrite a review ledger."""
    if not records:
        raise ValueError("cannot write an empty annotation ledger")
    seen = set()
    rows = []
    for record in records:
        record.validate()
        if record.sample_id in seen:
            raise ValueError(f"duplicate annotation sample ID: {record.sample_id}")
        seen.add(record.sample_id)
        rows.append(record.to_dict())
    path = Path(output_path).expanduser().resolve()
    if path.exists():
        raise FileExistsError(f"annotation ledger already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        _write_jsonl(temporary, rows)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_annotation_records(path: str | os.PathLike[str]) -> list[AnnotationRecord]:
    """Load and validate a complete annotation JSONL ledger."""
    rows = _read_jsonl(Path(path).expanduser().resolve(strict=True))
    records = [AnnotationRecord.from_dict(row) for row in rows]
    sample_ids = [record.sample_id for record in records]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("annotation ledger contains duplicate sample IDs")
    return records


def export_reviewed_yolo_labels(
    records: Sequence[AnnotationRecord],
    output_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Export YOLO labels only after explicit approval of every annotation row.

    This creates a label handoff, not a training release: source authorization,
    leakage-safe split assignment, and release validation remain separate gates.
    """
    if not records:
        raise ValueError("no annotation records to export")
    first = records[0]
    seen = set()
    for record in records:
        record.validate()
        if record.sample_id in seen:
            raise ValueError(f"duplicate annotation sample ID: {record.sample_id}")
        seen.add(record.sample_id)
        if record.review_status != "approved":
            raise ValueError(f"sample is not human-approved: {record.sample_id}")
        if (
            record.ontology_id != first.ontology_id
            or record.ontology_version != first.ontology_version
            or record.class_names != first.class_names
        ):
            raise ValueError("annotation records use different ontology versions")

    output = Path(output_dir).expanduser().resolve()
    rows = [record.to_dict() for record in records]
    ontology = {
        "ontology_id": first.ontology_id,
        "ontology_version": first.ontology_version,
        "classes": [
            {"id": index, "name": name, "definition": first.class_definitions[index]}
            for index, name in enumerate(first.class_names)
        ],
    }
    ontology = _normalize_ontology(ontology)
    if ontology["fingerprint"] != first.ontology_fingerprint:
        raise ValueError("annotation ontology fingerprint is inconsistent")

    def build(temporary: Path) -> None:
        (temporary / "labels" / "all").mkdir(parents=True)
        for record in records:
            label = temporary / "labels" / "all" / f"{_sample_stem(record.sample_id)}.txt"
            label.write_text(
                "".join(
                    f"{box.class_id} {box.x_center:.10g} {box.y_center:.10g} "
                    f"{box.width:.10g} {box.height:.10g}\n"
                    for box in record.annotations
                ),
                encoding="utf-8",
            )
        (temporary / "ontology.json").write_bytes(_json_bytes(ontology))
        _write_jsonl(temporary / "annotations.jsonl", rows)
        package = {
            "format_version": 1,
            "purpose": "reviewed_yolo_label_handoff",
            "training_ready": False,
            "training_readiness_reason": "source authorization, split registry, and release checks are separate gates",
            "sample_count": len(records),
            "ontology_fingerprint": ontology["fingerprint"],
        }
        (temporary / "package.json").write_bytes(_json_bytes(package))

    _write_new_directory(output, build)
    return {
        "output_dir": str(output),
        "sample_count": len(records),
        "training_ready": False,
        "ontology_fingerprint": ontology["fingerprint"],
    }
