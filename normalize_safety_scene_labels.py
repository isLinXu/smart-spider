#!/usr/bin/env python3
"""Normalize fixed scene labels in dataset JSONL indexes and SQLite payloads."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from crawl_safety_scenes import SCENES


def fixed_label(label: str, query: str, scene: str) -> list[dict[str, Any]]:
    return [{
        "name": label,
        "score": 1.0,
        "source": "fixed",
        "evidence": {
            "query": query,
            "dataset_scene": scene,
        },
    }]


def canonical_relative_path(raw_path: str, root: Path, scene_dir: Path, scene: str) -> str:
    """Map absolute or project-root-relative paths to a scene-relative path."""
    if not raw_path:
        return raw_path
    path = Path(raw_path)
    if path.is_absolute():
        try:
            return str(path.resolve().relative_to(scene_dir.resolve()))
        except ValueError:
            return raw_path
    parts = path.parts
    if scene in parts:
        scene_index = parts.index(scene)
        candidate = scene_dir.joinpath(*parts[scene_index + 1 :])
    else:
        candidate = scene_dir / path
    if candidate.exists():
        return str(candidate.resolve().relative_to(scene_dir.resolve()))
    return raw_path


def normalize_record(
    record: dict[str, Any],
    label: str,
    scene: str,
    root: Path,
    scene_dir: Path,
) -> dict[str, Any]:
    provenance = record.get("provenance") or {}
    query = str(provenance.get("query") or record.get("keyword") or "")
    if "file_path" in record:
        relative = canonical_relative_path(str(record.get("file_path") or ""), root, scene_dir, scene)
        record["file_path"] = str((scene_dir / relative).resolve()) if relative else relative
    if "file" in record:
        record["file"] = canonical_relative_path(str(record.get("file") or ""), root, scene_dir, scene)
    modalities = record.get("modalities") or []
    for modality in modalities:
        if isinstance(modality, dict) and modality.get("uri"):
            modality["uri"] = canonical_relative_path(str(modality["uri"]), root, scene_dir, scene)
    record["labels"] = fixed_label(label, query, scene)
    return record


def rewrite_jsonl(path: Path, label: str, scene: str, root: Path, scene_dir: Path) -> int:
    count = 0
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination, path.open(
            "r", encoding="utf-8"
        ) as source:
            for line_number, line in enumerate(source, 1):
                try:
                    record = json.loads(line)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid JSON in {path}:{line_number}") from exc
                destination.write(
                    json.dumps(
                        normalize_record(record, label, scene, root, scene_dir),
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                count += 1
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return count


def normalize_state_db(path: Path, label: str, scene: str, root: Path, scene_dir: Path) -> int:
    if not path.exists():
        return 0
    updated = 0
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT sample_id, payload_json FROM samples").fetchall()
        for sample_id, payload_json in rows:
            record = json.loads(payload_json)
            normalized = normalize_record(record, label, scene, root, scene_dir)
            connection.execute(
                "UPDATE samples SET payload_json = ? WHERE sample_id = ?",
                (json.dumps(normalized, ensure_ascii=False), sample_id),
            )
            updated += 1
        connection.commit()
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", default="dataset_safety_scenes_20000_20260902")
    args = parser.parse_args()
    root = Path(args.dataset_root).resolve()

    summary = {}
    for scene, config in SCENES.items():
        label = str(config["label"])
        scene_dir = root / scene
        metadata_count = rewrite_jsonl(scene_dir / "metadata.jsonl", label, scene, root, scene_dir)
        manifest_count = rewrite_jsonl(scene_dir / "manifest.jsonl", label, scene, root, scene_dir)
        state_count = normalize_state_db(
            scene_dir / ".dataset_state.sqlite3", label, scene, root, scene_dir
        )
        summary[scene] = {
            "label": label,
            "metadata_records": metadata_count,
            "manifest_records": manifest_count,
            "state_records": state_count,
        }
        print(f"{scene}: metadata={metadata_count} manifest={manifest_count} state={state_count}")

    report_path = root / "label_normalization_report.json"
    report_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
