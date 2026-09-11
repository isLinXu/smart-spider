#!/usr/bin/env python3
"""Repair active safety-scene batch layout after dedupe/replenishment.

Exact dedupe removes records from the middle of a dataset.  A subsequent
resume crawl appends at the old active count, which can leave duplicate
indices and an over-full final batch.  This utility deterministically
reindexes active records, moves images into 100-image batches, and updates all
three indexes (metadata, manifest, SQLite sample/event payloads).

The original indexes are copied to ``_layout_repairs/<run_id>`` before an
apply.  Image moves are staged and rolled back on an exception so an
interrupted repair does not intentionally discard data.
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected object in {path}:{line_no}")
            rows.append(value)
    return rows


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(path)


def _resolve(scene: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = scene / candidate
    return candidate.resolve()


def _scene_relative(scene: Path, value: str) -> str:
    return _resolve(scene, value).relative_to(scene.resolve()).as_posix()


def _replace_paths(value: Any, path_map: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_paths(item, path_map) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_paths(item, path_map) for item in value]
    if isinstance(value, str):
        return path_map.get(value, value)
    return value


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_conn = sqlite3.connect(source)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()


def _update_sqlite(path: Path, path_map: dict[str, str]) -> int:
    changed = 0
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("samples", "events"):
            rows = connection.execute(f"SELECT rowid, payload_json FROM {table}").fetchall()
            for rowid, payload_json in rows:
                payload = json.loads(payload_json)
                updated = _replace_paths(payload, path_map)
                if updated != payload:
                    connection.execute(
                        f"UPDATE {table} SET payload_json=? WHERE rowid=?",
                        (json.dumps(updated, ensure_ascii=False), rowid),
                    )
                    changed += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return changed


def inspect_scene(scene: Path, batch_size: int) -> dict[str, int]:
    metadata = _jsonl(scene / "metadata.jsonl")
    active_files = sorted(scene.glob("batch_*/*.jpg"))
    counts: dict[str, int] = {}
    for row in metadata:
        counts[str(row.get("batch", ""))] = counts.get(str(row.get("batch", "")), 0) + 1
    return {
        "metadata_records": len(metadata),
        "active_files": len(active_files),
        "duplicate_indices": len(metadata) - len({row.get("index") for row in metadata}),
        "batch_violations": sum(count > batch_size for count in counts.values()),
    }


def repair_scene(scene: Path, run_id: str, batch_size: int = 100, apply: bool = False) -> dict[str, Any]:
    scene = scene.resolve()
    metadata_path = scene / "metadata.jsonl"
    manifest_path = scene / "manifest.jsonl"
    state_path = scene / ".dataset_state.sqlite3"
    for required in (metadata_path, manifest_path, state_path):
        if not required.exists():
            raise FileNotFoundError(required)

    metadata = _jsonl(metadata_path)
    manifest = _jsonl(manifest_path)
    active_files = sorted(scene.glob("batch_*/*.jpg"))
    if len(metadata) != len(active_files) or len(manifest) != len(active_files):
        raise ValueError(
            f"record/file mismatch: metadata={len(metadata)} manifest={len(manifest)} "
            f"files={len(active_files)}"
        )

    metadata_by_rel: dict[str, dict[str, Any]] = {}
    for row in metadata:
        rel = _scene_relative(scene, str(row["file_path"]))
        if rel in metadata_by_rel:
            raise ValueError(f"duplicate metadata path: {rel}")
        if not (scene / rel).is_file():
            raise FileNotFoundError(scene / rel)
        metadata_by_rel[rel] = row

    manifest_by_rel: dict[str, dict[str, Any]] = {}
    for row in manifest:
        rel = _scene_relative(scene, str(row["file"]))
        if rel in manifest_by_rel:
            raise ValueError(f"duplicate manifest path: {rel}")
        manifest_by_rel[rel] = row

    active_rel = [path.relative_to(scene).as_posix() for path in active_files]
    if set(active_rel) != set(metadata_by_rel) or set(active_rel) != set(manifest_by_rel):
        missing_meta = sorted(set(active_rel) - set(metadata_by_rel))[:3]
        missing_files = sorted(set(metadata_by_rel) - set(active_rel))[:3]
        raise ValueError(f"index/file path mismatch: missing_metadata={missing_meta} missing_files={missing_files}")

    # Preserve metadata order (which is crawl order), while assigning a dense
    # index and stable 100-image batch placement.
    entries: list[dict[str, str]] = []
    used_targets: set[str] = set()
    for ordinal, row in enumerate(metadata):
        old_rel = _scene_relative(scene, str(row["file_path"]))
        source = Path(old_rel)
        target_batch = f"batch_{(ordinal // batch_size) * batch_size:04d}"
        target_name = source.name
        target_rel = f"{target_batch}/{target_name}"
        if target_rel in used_targets:
            stem, suffix = source.stem, source.suffix
            target_rel = f"{target_batch}/{ordinal:05d}_{stem}{suffix}"
        used_targets.add(target_rel)
        entries.append({"old_rel": old_rel, "new_rel": target_rel})

    report: dict[str, Any] = {
        "run_id": run_id,
        "dataset": str(scene),
        "records": len(entries),
        "batch_size": batch_size,
        "applied": apply,
        "old_to_new_paths": len(entries),
    }
    if not apply:
        report["before"] = inspect_scene(scene, batch_size)
        report["after"] = {
            "metadata_records": len(entries),
            "active_files": len(entries),
            "duplicate_indices": 0,
            "batch_violations": 0,
        }
        return report

    backup_dir = scene / "_layout_repairs" / run_id
    if backup_dir.exists():
        raise FileExistsError(f"repair backup already exists: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(metadata_path, backup_dir / "metadata.jsonl")
    shutil.copy2(manifest_path, backup_dir / "manifest.jsonl")
    progress_path = scene / ".dataset_progress.json"
    if progress_path.exists():
        shutil.copy2(progress_path, backup_dir / progress_path.name)
    _backup_sqlite(state_path, backup_dir / state_path.name)
    with (backup_dir / "path_map.jsonl").open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    staging = scene / f".layout_repair_tmp_{run_id}"
    if staging.exists():
        raise FileExistsError(f"temporary staging directory already exists: {staging}")
    staging.mkdir()
    moved_to_staging: list[tuple[Path, Path]] = []
    moved_to_target: list[tuple[Path, Path]] = []
    path_map: dict[str, str] = {}
    try:
        for ordinal, entry in enumerate(entries):
            source = scene / entry["old_rel"]
            staged = staging / f"{ordinal:05d}.jpg"
            shutil.move(str(source), str(staged))
            moved_to_staging.append((source, staged))

        for ordinal, entry in enumerate(entries):
            staged = staging / f"{ordinal:05d}.jpg"
            target = scene / entry["new_rel"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staged), str(target))
            moved_to_target.append((staged, target))
            path_map[entry["old_rel"]] = entry["new_rel"]
            path_map[str((scene / entry["old_rel"]).resolve())] = str(target.resolve())

        new_metadata: list[dict[str, Any]] = []
        new_manifest: list[dict[str, Any]] = []
        for ordinal, entry in enumerate(entries):
            old_rel = entry["old_rel"]
            new_rel = entry["new_rel"]
            metadata_row = copy.deepcopy(metadata_by_rel[old_rel])
            metadata_row["index"] = ordinal
            metadata_row["batch"] = Path(new_rel).parent.name
            metadata_row["file_path"] = str((scene / new_rel).resolve())
            new_metadata.append(metadata_row)

            manifest_row = copy.deepcopy(manifest_by_rel[old_rel])
            manifest_row["file"] = new_rel
            for asset in manifest_row.get("modalities", []):
                if isinstance(asset, dict) and asset.get("uri") in {old_rel, entry["old_rel"]}:
                    asset["uri"] = new_rel
            new_manifest.append(manifest_row)

        # Update SQLite payloads before publishing the new JSONL indexes.  A
        # transaction keeps samples/events internally consistent on failure.
        changed_sqlite = _update_sqlite(state_path, path_map)
        _write_jsonl(metadata_path, new_metadata)
        _write_jsonl(manifest_path, new_manifest)
        staging.rmdir()
        report["sqlite_payloads_updated"] = changed_sqlite
        report["before"] = inspect_scene(backup_dir, batch_size) if False else None
        report["after"] = inspect_scene(scene, batch_size)
        report.pop("before", None)
        (backup_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return report
    except Exception:
        # Restore the file layout first, then the indexes/database from backup.
        for staged, target in reversed(moved_to_target):
            if target.exists():
                shutil.move(str(target), str(staged))
        for source, staged in reversed(moved_to_staging):
            if staged.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(staged), str(source))
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copy2(backup_dir / "metadata.jsonl", metadata_path)
        shutil.copy2(backup_dir / "manifest.jsonl", manifest_path)
        shutil.copy2(backup_dir / state_path.name, state_path)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--run-id", default="layout_repair_20260903")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = repair_scene(args.dataset, args.run_id, args.batch_size, args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
