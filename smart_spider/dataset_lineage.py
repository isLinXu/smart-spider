# coding=utf-8
"""Dataset lineage, config fingerprints, and manifest checksums (S10).

A published dataset directory should answer: which job, which config, which
models, and whether ``manifest.jsonl`` still matches the published checksum.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from .dataset_contracts import CONTRACT_FORMAT_VERSION, URL_NORMALIZE_VERSION, utc_now

LINEAGE_FORMAT_VERSION = 1
LINEAGE_FILENAME = ".dataset_lineage.json"
MANIFEST_CHECKSUM_FILENAME = "manifest.sha256"


@dataclass(frozen=True)
class DatasetLineage:
    """Immutable identity for one published dataset directory."""

    dataset_id: str
    version: str
    created_at: str = field(default_factory=utc_now)
    job_id: str = ""
    config_fingerprint: str = ""
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    format_version: int = LINEAGE_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("dataset_id is required")
        if not self.version:
            raise ValueError("version is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DatasetLineage":
        return cls(
            dataset_id=str(raw.get("dataset_id") or ""),
            version=str(raw.get("version") or ""),
            created_at=str(raw.get("created_at") or utc_now()),
            job_id=str(raw.get("job_id") or ""),
            config_fingerprint=str(raw.get("config_fingerprint") or ""),
            config_snapshot=dict(raw.get("config_snapshot") or {}),
            provenance=dict(raw.get("provenance") or {}),
            format_version=int(raw.get("format_version") or LINEAGE_FORMAT_VERSION),
        )


def config_fingerprint(config: Mapping[str, Any] | None) -> str:
    """Stable short fingerprint over a JSON-serializable config snapshot."""
    payload = json.dumps(
        _canonicalize(config or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_dataset_id(*, job_id: str, output_dir: str | os.PathLike[str]) -> str:
    """Derive a stable dataset id from job + absolute output path."""
    root = str(Path(output_dir).expanduser().resolve())
    material = f"{job_id or 'job'}\0{root}"
    return "ds_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_lineage(
    *,
    job_id: str,
    output_dir: str | os.PathLike[str],
    config_snapshot: Optional[Mapping[str, Any]] = None,
    version: Optional[str] = None,
    clip_model: Optional[str] = None,
    extra_provenance: Optional[Mapping[str, Any]] = None,
) -> DatasetLineage:
    snapshot = dict(config_snapshot or {})
    fingerprint = config_fingerprint(snapshot)
    dataset_id = build_dataset_id(job_id=job_id, output_dir=output_dir)
    lineage_version = version or f"v1-{fingerprint}"
    provenance = {
        "url_normalize_version": URL_NORMALIZE_VERSION,
        "contract_format_version": CONTRACT_FORMAT_VERSION,
        "clip_model": clip_model or snapshot.get("clip_model") or snapshot.get("model"),
        "output_dir": str(Path(output_dir).expanduser().resolve()),
    }
    if extra_provenance:
        provenance.update(dict(extra_provenance))
    return DatasetLineage(
        dataset_id=dataset_id,
        version=lineage_version,
        job_id=str(job_id or ""),
        config_fingerprint=fingerprint,
        config_snapshot=snapshot,
        provenance=provenance,
    )


def lineage_path(output_dir: str | os.PathLike[str]) -> Path:
    return Path(output_dir).expanduser().resolve() / LINEAGE_FILENAME


def write_lineage(output_dir: str | os.PathLike[str], lineage: DatasetLineage) -> Path:
    path = lineage_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(lineage.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_lineage(output_dir: str | os.PathLike[str]) -> Optional[DatasetLineage]:
    path = lineage_path(output_dir)
    if not path.is_file():
        return None
    return DatasetLineage.from_dict(json.loads(path.read_text(encoding="utf-8")))


def iter_manifest_files(output_dir: str | os.PathLike[str]) -> list[Path]:
    root = Path(output_dir).expanduser().resolve()
    files = sorted(root.glob("manifest-*.jsonl"))
    single = root / "manifest.jsonl"
    if single.is_file():
        files = [single, *[path for path in files if path != single]]
    return files


def hash_files(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def write_manifest_checksum(
    output_dir: str | os.PathLike[str],
    *,
    paths: Optional[Sequence[Path]] = None,
) -> dict[str, Any]:
    """Write ``manifest.sha256`` covering active manifest JSONL shards."""
    root = Path(output_dir).expanduser().resolve()
    manifest_paths = list(paths) if paths is not None else iter_manifest_files(root)
    checksum = hash_files(manifest_paths) if manifest_paths else hashlib.sha256().hexdigest()
    record = {
        "algorithm": "sha256",
        "created_at": utc_now(),
        "files": [path.name for path in manifest_paths],
        "sha256": checksum,
        "format_version": LINEAGE_FORMAT_VERSION,
    }
    target = root / MANIFEST_CHECKSUM_FILENAME
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return record


def verify_manifest_checksum(output_dir: str | os.PathLike[str]) -> dict[str, Any]:
    root = Path(output_dir).expanduser().resolve()
    checksum_path = root / MANIFEST_CHECKSUM_FILENAME
    if not checksum_path.is_file():
        return {"ok": False, "error": "manifest.sha256 missing"}
    record = json.loads(checksum_path.read_text(encoding="utf-8"))
    expected = str(record.get("sha256") or "")
    names = list(record.get("files") or [])
    paths = [root / name for name in names]
    missing = [str(path.name) for path in paths if not path.is_file()]
    if missing:
        return {"ok": False, "error": "missing manifest files", "missing": missing}
    actual = hash_files(paths) if paths else hashlib.sha256().hexdigest()
    return {
        "ok": actual == expected,
        "expected": expected,
        "actual": actual,
        "files": names,
    }


def publish_dataset_artifacts(
    output_dir: str | os.PathLike[str],
    lineage: DatasetLineage,
) -> dict[str, Any]:
    """Write lineage sidecar + manifest checksum for a finished dataset."""
    lineage_file = write_lineage(output_dir, lineage)
    checksum = write_manifest_checksum(output_dir)
    return {
        "lineage_path": str(lineage_file),
        "checksum": checksum,
        "lineage": lineage.to_dict(),
    }


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
