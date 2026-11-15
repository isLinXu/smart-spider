import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from smart_spider.annotation_contracts import (
    AnnotationRecord,
    BoundingBox,
    ReviewDecision,
    apply_review_decisions,
)
from smart_spider.annotation_cli import main as annotation_cli_main
from smart_spider.annotation_yolo import (
    export_annotation_task,
    export_reviewed_yolo_labels,
    import_yolo_annotations,
)


ONTOLOGY = {
    "schema_version": 2,
    "task_type": "object_detection",
    "classes": [
        {"id": 0, "name": "person", "definition": "visible person"},
        {"id": 1, "name": "helmet", "definition": "industrial hard hat"},
    ],
}


def _fixture_manifest(root: Path, *, color=(20, 90, 160), sample_id=None):
    image_dir = root / "images"
    image_dir.mkdir(parents=True)
    image_path = image_dir / "frame.png"
    Image.new("RGB", (64, 48), color).save(image_path, format="PNG")
    content = image_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    sample_id = sample_id or f"sha256:{digest}"
    manifest = root / "manifest.jsonl"
    manifest.write_text(json.dumps({"id": sample_id, "file": "images/frame.png"}) + "\n", encoding="utf-8")
    return manifest, sample_id, digest


def _export(tmp_path: Path, *, second_manifest=False):
    first, sample_id, digest = _fixture_manifest(tmp_path / "source-a")
    manifests = [first]
    if second_manifest:
        second, second_id, second_digest = _fixture_manifest(tmp_path / "source-b")
        assert second_id == sample_id and second_digest == digest
        manifests.append(second)
    output = tmp_path / "annotation-task"
    report = export_annotation_task(
        manifests,
        ONTOLOGY,
        output,
        split_assignments={sample_id: "train"},
    )
    rows = [json.loads(line) for line in (output / "samples.jsonl").read_text(encoding="utf-8").splitlines()]
    return output, rows[0], sample_id, digest, report


def _write_label(package: Path, row: dict, content: str = "0 0.5 0.5 0.4 0.6\n") -> Path:
    image_path = Path(row["image_file"])
    label = package / "labels" / Path(*image_path.parts[1:]).with_suffix(".txt")
    label.parent.mkdir(parents=True, exist_ok=True)
    label.write_text(content, encoding="utf-8")
    return label


def test_annotation_task_deduplicates_content_without_creating_negative_labels(tmp_path):
    package, row, sample_id, digest, report = _export(tmp_path, second_manifest=True)

    assert report["sample_count"] == 1
    assert report["duplicate_sources_merged"] == 1
    assert row["sample_id"] == sample_id
    assert row["asset_sha256"] == digest
    assert len(row["sources"]) == 2
    assert row["training_ready"] is False
    assert (package / row["image_file"]).is_file()
    assert not (package / "labels").exists()


def test_yolo_import_is_pending_and_keeps_normalized_boxes_and_review_audit(tmp_path):
    package, row, sample_id, digest, _ = _export(tmp_path)
    _write_label(package, row)

    records = import_yolo_annotations(package, output_path=tmp_path / "annotations.jsonl")
    annotation = records[0]
    assert annotation.sample_id == sample_id
    assert annotation.asset_sha256 == digest
    assert AnnotationRecord.from_dict(annotation.to_dict()) == annotation
    assert annotation.review_status == "pending"
    assert annotation.annotation_approved is False
    assert annotation.annotations == (BoundingBox(0, 0.5, 0.5, 0.4, 0.6),)

    approved = apply_review_decisions(records, [ReviewDecision(
        sample_id=sample_id,
        asset_sha256=digest,
        expected_revision=0,
        decision="approve",
        reviewer="reviewer-17",
        decided_at="2026-11-15T10:00:00+00:00",
    )])
    assert approved[0].annotation_approved
    assert approved[0].revision == 1
    assert approved[0].audit_log[0]["annotation_sha256"] == annotation.annotation_digest()
    assert AnnotationRecord.from_dict(approved[0].to_dict()) == approved[0]

    handoff = tmp_path / "reviewed-labels"
    result = export_reviewed_yolo_labels(approved, handoff)
    label = handoff / "labels" / "all" / f"{hashlib.sha256(sample_id.encode()).hexdigest()}.txt"
    assert label.read_text(encoding="utf-8") == "0 0.5 0.5 0.4 0.6\n"
    assert result["training_ready"] is False


def test_empty_yolo_file_stays_pending_and_missing_file_is_not_assumed_negative(tmp_path):
    package, row, _, _, _ = _export(tmp_path)
    label = _write_label(package, row, "")
    records = import_yolo_annotations(package)
    assert records[0].annotations == ()
    assert records[0].review_status == "pending"
    with pytest.raises(ValueError, match="not human-approved"):
        export_reviewed_yolo_labels(records, tmp_path / "unreviewed-labels")

    sample_id = records[0].sample_id
    approved = apply_review_decisions(records, [ReviewDecision(
        sample_id=sample_id,
        asset_sha256=records[0].asset_sha256,
        expected_revision=0,
        decision="approve",
        reviewer="reviewer",
    )])
    handoff = tmp_path / "approved-background"
    export_reviewed_yolo_labels(approved, handoff)
    exported = handoff / "labels" / "all" / f"{hashlib.sha256(sample_id.encode()).hexdigest()}.txt"
    assert exported.read_text(encoding="utf-8") == ""

    label.unlink()
    with pytest.raises(FileNotFoundError, match="label file missing"):
        import_yolo_annotations(package)


@pytest.mark.parametrize("line", [
    "0 0.5 0.5 0 0.2\n",
    "0 0.95 0.5 0.2 0.2\n",
    "2 0.5 0.5 0.2 0.2\n",
    "0 nan 0.5 0.2 0.2\n",
    "0 0.5 0.5 0.2\n",
])
def test_yolo_import_rejects_invalid_box_rows(tmp_path, line):
    package, row, _, _, _ = _export(tmp_path)
    _write_label(package, row, line)
    with pytest.raises(ValueError):
        import_yolo_annotations(package)


def test_import_rechecks_image_hash_and_package_sidecars(tmp_path):
    package, row, _, _, _ = _export(tmp_path)
    _write_label(package, row)
    image = package / row["image_file"]
    image.write_bytes(image.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="invalid or unsafe image|hash mismatch"):
        import_yolo_annotations(package)


def test_import_rejects_changed_sidecar_and_unknown_label_files(tmp_path):
    package, row, _, _, _ = _export(tmp_path)
    _write_label(package, row)
    samples = package / "samples.jsonl"
    samples.write_text(samples.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sidecar changed"):
        import_yolo_annotations(package)

    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    fresh_package, fresh_row, _, _, _ = _export(fresh_root)
    _write_label(fresh_package, fresh_row)
    extra = fresh_package / "labels" / "all" / "unknown.txt"
    extra.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown sample"):
        import_yolo_annotations(fresh_package)


def test_review_decisions_fence_stale_hash_and_revision_and_rejection_needs_reason(tmp_path):
    package, row, sample_id, digest, _ = _export(tmp_path)
    _write_label(package, row)
    records = import_yolo_annotations(package)
    with pytest.raises(ValueError, match="requires a reason"):
        apply_review_decisions(records, [ReviewDecision(sample_id, digest, 0, "reject", "reviewer")])
    with pytest.raises(ValueError, match="image hash changed"):
        apply_review_decisions(records, [ReviewDecision(sample_id, "0" * 64, 0, "approve", "reviewer")])

    approved = apply_review_decisions(records, [ReviewDecision(sample_id, digest, 0, "approve", "reviewer")])
    with pytest.raises(ValueError, match="stale review revision"):
        apply_review_decisions(approved, [ReviewDecision(sample_id, digest, 0, "approve", "reviewer")])
    with pytest.raises(ValueError, match="already approved"):
        apply_review_decisions(approved, [ReviewDecision(sample_id, digest, 1, "approve", "reviewer")])


def test_approved_record_rejects_boxes_changed_after_review(tmp_path):
    package, row, sample_id, digest, _ = _export(tmp_path)
    _write_label(package, row)
    record = import_yolo_annotations(package)[0]
    approved = apply_review_decisions([record], [ReviewDecision(sample_id, digest, 0, "approve", "reviewer")])[0]
    with pytest.raises(ValueError, match="annotation content changed"):
        AnnotationRecord(
            sample_id=approved.sample_id,
            asset_sha256=approved.asset_sha256,
            image_width=approved.image_width,
            image_height=approved.image_height,
            ontology_id=approved.ontology_id,
            ontology_version=approved.ontology_version,
            class_names=approved.class_names,
            ontology_fingerprint=approved.ontology_fingerprint,
            class_definitions=approved.class_definitions,
            annotations=(BoundingBox(0, 0.5, 0.5, 0.2, 0.2),),
            review_status="approved",
            revision=approved.revision,
            audit_log=approved.audit_log,
        )


def test_export_refuses_existing_output_and_unknown_split_ids(tmp_path):
    manifest, _, _ = _fixture_manifest(tmp_path / "source")
    with pytest.raises(ValueError, match="unknown samples"):
        export_annotation_task([manifest], ONTOLOGY, tmp_path / "bad", split_assignments={"missing": "train"})
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        export_annotation_task([manifest], ONTOLOGY, output)


def test_annotation_cli_help_lists_exchange_commands(capsys):
    with pytest.raises(SystemExit) as exit_info:
        annotation_cli_main(["--help"])
    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "export-task" in help_text
    assert "import-yolo" in help_text
    assert "export-reviewed" in help_text


def test_annotation_cli_complete_offline_handoff(tmp_path, capsys):
    manifest, sample_id, digest = _fixture_manifest(tmp_path / "source")
    ontology_path = tmp_path / "ontology.json"
    ontology_path.write_text(json.dumps(ONTOLOGY), encoding="utf-8")
    package = tmp_path / "cli-task"
    assert annotation_cli_main([
        "export-task", "--manifest", str(manifest), "--ontology", str(ontology_path),
        "--output", str(package),
    ]) == 0
    sample = json.loads((package / "samples.jsonl").read_text(encoding="utf-8"))
    _write_label(package, sample)

    ledger = tmp_path / "annotations.jsonl"
    assert annotation_cli_main([
        "import-yolo", "--package", str(package), "--output", str(ledger),
    ]) == 0
    decisions = tmp_path / "decisions.jsonl"
    decisions.write_text(json.dumps({
        "sample_id": sample_id,
        "asset_sha256": digest,
        "expected_revision": 0,
        "decision": "approve",
        "reviewer": "reviewer-42",
    }) + "\n", encoding="utf-8")
    reviewed = tmp_path / "reviewed.jsonl"
    assert annotation_cli_main([
        "review", "--annotations", str(ledger), "--decisions", str(decisions),
        "--output", str(reviewed),
    ]) == 0
    handoff = tmp_path / "cli-yolo-labels"
    assert annotation_cli_main([
        "export-reviewed", "--annotations", str(reviewed), "--output", str(handoff),
    ]) == 0
    assert json.loads((handoff / "package.json").read_text(encoding="utf-8"))["training_ready"] is False
    capsys.readouterr()
