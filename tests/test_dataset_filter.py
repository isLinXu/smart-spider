# coding=utf-8
"""Dataset advertisement and semantic filter tests."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from smart_spider.dataset_filter import (
    CLIPPromptScorer,
    DatasetImageFilter,
    DecisionPolicy,
    FilterThresholds,
    PromptSet,
    SemanticScores,
    VisualSignalAnalyzer,
    VisualSignals,
)
from smart_spider.dataset_filter_cli import _build_parser, main
from smart_spider.dataset_review import (
    BLIPReviewScores,
    BLIPReviewThresholds,
    export_review_candidates,
    run_quarantine_review,
)


class ColorScorer:
    def score_images(self, images):
        scores = []
        for image in images:
            red, green, blue = np.asarray(image.resize((1, 1)))[0, 0]
            if red > green and red > blue:
                scores.append(SemanticScores(0.21, 0.28, 0.18))
            elif blue > red and blue > green:
                scores.append(SemanticScores(0.17, 0.18, 0.25))
            else:
                scores.append(SemanticScores(0.31, 0.18, 0.17))
        return scores


class ColorSignals:
    def analyze(self, image, scores=None):
        red, green, blue = np.asarray(image.resize((1, 1)))[0, 0]
        if red > green and red > blue:
            return VisualSignals(
                text_area_ratio=0.25,
                text_box_count=8,
                promotion_hits=("sale",),
                contact_hits=1,
            )
        return VisualSignals()


class AlwaysMismatchScorer:
    def score_images(self, images):
        return [SemanticScores(0.23, 0.15, 0.30) for _ in images]


def _write_dataset(root: Path):
    batch = root / "batch_0000"
    batch.mkdir(parents=True)
    rows = []
    manifests = []
    colors = ((220, 20, 20), (20, 220, 20), (20, 20, 220))
    for index, color in enumerate(colors):
        relative = f"batch_0000/{index:04d}.jpg"
        Image.new("RGB", (32, 24), color).save(root / relative)
        rows.append({"index": index, "file_path": relative, "url": f"https://x/{index}"})
        manifests.append({
            "id": f"{index:06d}",
            "modalities": [{"type": "image", "path": relative}],
        })
    (root / "metadata.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    (root / "manifest.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in manifests), encoding="utf-8"
    )


def _filter(root: Path):
    return DatasetImageFilter(
        root,
        ColorScorer(),
        ColorSignals(),
        policy=DecisionPolicy(FilterThresholds()),
        batch_size=2,
    )


def test_decision_policy_distinguishes_advertisement_mismatch_and_keep():
    policy = DecisionPolicy()
    advertisement = policy.decide(
        record_position=0,
        path="ad.jpg",
        scores=SemanticScores(0.21, 0.28, 0.18),
        signals=VisualSignals(text_area_ratio=0.3, promotion_hits=("sale",)),
    )
    mismatch = policy.decide(
        record_position=1,
        path="wrong.jpg",
        scores=SemanticScores(0.17, 0.18, 0.25),
        signals=VisualSignals(),
    )
    keep = policy.decide(
        record_position=2,
        path="keep.jpg",
        scores=SemanticScores(0.31, 0.18, 0.17),
        signals=VisualSignals(),
    )

    assert advertisement.category == "advertisement"
    assert advertisement.action == "quarantine"
    assert mismatch.category == "semantic_mismatch"
    assert keep.action == "keep"


def test_dry_run_is_non_mutating_and_samples_evenly(tmp_path):
    _write_dataset(tmp_path)
    report = _filter(tmp_path).run(limit=2, run_id="dry")

    assert report["scanned"] == 2
    assert report["dataset_records"] == 3
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3
    assert not (tmp_path / DatasetImageFilter.ACTIVE_RUN_FILE).exists()
    decisions = [
        json.loads(line)
        for line in (tmp_path / "_filter_runs/dry/decisions.jsonl").read_text().splitlines()
    ]
    assert [item["record_position"] for item in decisions] == [0, 2]


def test_apply_quarantines_and_restore_recovers_indexes(tmp_path):
    _write_dataset(tmp_path)
    original_metadata = (tmp_path / "metadata.jsonl").read_text()
    original_manifest = (tmp_path / "manifest.jsonl").read_text()

    report = _filter(tmp_path).run(apply=True, run_id="applied")

    assert report["quarantined"] == 2
    assert report["remaining"] == 1
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 1
    assert len((tmp_path / "metadata.jsonl").read_text().splitlines()) == 1
    assert len((tmp_path / "manifest.jsonl").read_text().splitlines()) == 1
    assert len(list((tmp_path / "_quarantine/applied/files").rglob("*.jpg"))) == 2

    verified = DatasetImageFilter.verify(tmp_path, run_id="applied")
    assert verified["ok"] is True
    assert verified["active_images"] == 1
    assert verified["quarantined_images"] == 2
    assert verified["checked_active_images"] == 1
    assert verified["checked_quarantined_images"] == 2

    restored = DatasetImageFilter.restore(tmp_path, "applied")

    assert restored["restored"] == 2
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3
    assert (tmp_path / "metadata.jsonl").read_text() == original_metadata
    assert (tmp_path / "manifest.jsonl").read_text() == original_manifest
    assert not (tmp_path / DatasetImageFilter.ACTIVE_RUN_FILE).exists()
    assert DatasetImageFilter.verify(tmp_path)["ok"] is True


def test_cli_parser_exposes_safe_filter_controls():
    args = _build_parser().parse_args([
        "--dataset", "dataset",
        "--limit", "100",
        "--ocr",
        "--min-relevance", "0.22",
        "--profile", "strict",
        "--verify-hashes",
    ])
    assert args.dataset == "dataset"
    assert args.limit == 100
    assert args.ocr is True
    assert args.apply is False
    assert args.min_relevance == 0.22
    assert args.profile == "strict"
    assert args.verify_hashes is True


def test_filter_profiles_have_explicit_precision_recall_tradeoffs():
    conservative = FilterThresholds.from_profile("conservative")
    balanced = FilterThresholds.from_profile("balanced")
    strict = FilterThresholds.from_profile("strict")
    precision = FilterThresholds.from_profile("precision")

    assert conservative.min_relevance < balanced.min_relevance == strict.min_relevance
    assert conservative.mismatch_margin > balanced.mismatch_margin > strict.mismatch_margin
    assert conservative.text_area_ratio > balanced.text_area_ratio > strict.text_area_ratio
    assert balanced.minimum_scene_evidence is None
    assert precision.minimum_scene_evidence is not None
    assert precision.scene_evidence_margin == 0.005


def test_precision_profile_requires_core_scene_evidence():
    policy = DecisionPolicy(FilterThresholds.from_profile("precision"))
    generic_truck = policy.decide(
        record_position=0,
        path="generic-truck.jpg",
        scores=SemanticScores(
            relevance=0.29,
            advertisement=0.18,
            mismatch=0.25,
            scene_evidence=0.21,
        ),
        signals=VisualSignals(),
    )
    active_loading = policy.decide(
        record_position=1,
        path="active-loading.jpg",
        scores=SemanticScores(
            relevance=0.29,
            advertisement=0.18,
            mismatch=0.24,
            scene_evidence=0.28,
        ),
        signals=VisualSignals(),
    )

    assert generic_truck.action == "quarantine"
    assert "low_scene_evidence" in generic_truck.reasons
    assert active_loading.action == "keep"

    weak_loading_cue = policy.decide(
        record_position=2,
        path="weak-loading-cue.jpg",
        scores=SemanticScores(
            relevance=0.29,
            advertisement=0.18,
            mismatch=0.271,
            scene_evidence=0.265,
        ),
        signals=VisualSignals(),
    )
    assert weak_loading_cue.action == "quarantine"
    assert "scene_negative_prompt_wins" in weak_loading_cue.reasons


def test_non_ascii_query_uses_calibrated_clip_prompts_only():
    chinese = PromptSet.truck_loading("货车装卸区 开关门")
    english = PromptSet.truck_loading("truck loading dock door operation")

    assert "货车装卸区 开关门" not in chinese.positive
    assert english.positive[0] == "truck loading dock door operation"


def test_supported_advertisement_signal_catches_textual_product_card():
    policy = DecisionPolicy()
    decision = policy.decide(
        record_position=0,
        path="product-card.jpg",
        scores=SemanticScores(0.24, 0.22, 0.20),
        signals=VisualSignals(text_area_ratio=0.18),
    )
    assert decision.action == "quarantine"
    assert decision.category == "advertisement"
    assert "text_supported_advertisement" in decision.reasons


def test_apply_rejects_invalid_records_without_mutation(tmp_path):
    _write_dataset(tmp_path)
    metadata_before = (tmp_path / "metadata.jsonl").read_text()
    (tmp_path / "batch_0000/0001.jpg").unlink()

    try:
        _filter(tmp_path).run(apply=True, run_id="invalid")
    except RuntimeError as exc:
        assert "invalid or missing" in str(exc)
    else:
        raise AssertionError("expected invalid apply to fail")
    assert (tmp_path / "metadata.jsonl").read_text() == metadata_before
    assert not (tmp_path / DatasetImageFilter.ACTIVE_RUN_FILE).exists()


def test_applied_runs_stack_and_restore_in_reverse_order(tmp_path):
    _write_dataset(tmp_path)
    _filter(tmp_path).run(apply=True, run_id="first")
    DatasetImageFilter(
        tmp_path,
        AlwaysMismatchScorer(),
        ColorSignals(),
        batch_size=2,
    ).run(apply=True, run_id="second")

    marker = json.loads((tmp_path / DatasetImageFilter.ACTIVE_RUN_FILE).read_text())
    assert [item["run_id"] for item in marker["runs"]] == ["first", "second"]
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 0
    verified = DatasetImageFilter.verify(tmp_path)
    assert verified["ok"] is True
    assert verified["quarantined_images"] == 3
    assert [item["run_id"] for item in verified["active_runs"]] == ["first", "second"]

    try:
        DatasetImageFilter.restore(tmp_path, "first")
    except RuntimeError as exc:
        assert "latest run" in str(exc)
    else:
        raise AssertionError("expected out-of-order restore to fail")
    DatasetImageFilter.restore(tmp_path, "second")
    marker = json.loads((tmp_path / DatasetImageFilter.ACTIVE_RUN_FILE).read_text())
    assert marker["run_id"] == "first"
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 1
    DatasetImageFilter.restore(tmp_path, "first")
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3


def test_verify_reports_missing_quarantine_file(tmp_path):
    _write_dataset(tmp_path)
    _filter(tmp_path).run(apply=True, run_id="broken")
    quarantined = next((tmp_path / "_quarantine/broken/files").rglob("*.jpg"))
    quarantined.unlink()

    report = DatasetImageFilter.verify(tmp_path)
    assert report["ok"] is False
    assert any("quarantined image missing" in error for error in report["errors"])


def test_verify_normalizes_prefixed_paths_and_uses_manifest_hashes(tmp_path):
    _write_dataset(tmp_path)
    metadata_path = tmp_path / "metadata.jsonl"
    metadata = [json.loads(line) for line in metadata_path.read_text().splitlines()]
    for record in metadata:
        record["file_path"] = f"{tmp_path.name}/{record['file_path']}"
    metadata_path.write_text(
        "".join(json.dumps(record) + "\n" for record in metadata),
        encoding="utf-8",
    )

    manifest_path = tmp_path / "manifest.jsonl"
    manifest = [json.loads(line) for line in manifest_path.read_text().splitlines()]
    for record in manifest:
        relative = record["modalities"][0]["path"]
        record["id"] = f"sha256:{hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest()}"
    manifest_path.write_text(
        "".join(json.dumps(record) + "\n" for record in manifest),
        encoding="utf-8",
    )

    _filter(tmp_path).run(apply=True, run_id="hashes")
    report = DatasetImageFilter.verify(tmp_path, verify_hashes=True)

    assert report["ok"] is True
    assert report["hashed_active_images"] == 1
    assert report["hashed_quarantined_images"] == 2


def test_exact_dedupe_quarantines_later_copy_and_restores(tmp_path):
    _write_dataset(tmp_path)
    duplicate = tmp_path / "batch_0000/0002.jpg"
    duplicate.write_bytes((tmp_path / "batch_0000/0001.jpg").read_bytes())

    report = DatasetImageFilter.run_exact_dedupe(
        tmp_path,
        apply=True,
        run_id="dedupe",
    )

    assert report["quarantined"] == 1
    assert report["remaining"] == 2
    assert report["duplicate_groups"] == 1
    assert DatasetImageFilter.verify(tmp_path)["ok"] is True
    DatasetImageFilter.restore(tmp_path, "dedupe")
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3


def test_near_dedupe_uses_hash_aspect_and_color_gates(tmp_path):
    _write_dataset(tmp_path)
    near_copy = tmp_path / "batch_0000/0002.jpg"
    Image.new("RGB", (40, 30), (220, 20, 20)).save(near_copy, quality=70)

    dry_run = DatasetImageFilter.run_near_dedupe(tmp_path, run_id="near-dry")

    assert dry_run["actions"]["quarantine"] == 1
    assert dry_run["duplicate_groups"] == 1
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3

    applied = DatasetImageFilter.run_near_dedupe(
        tmp_path,
        apply=True,
        run_id="near-applied",
    )
    assert applied["quarantined"] == 1
    assert applied["remaining"] == 2
    assert DatasetImageFilter.verify(tmp_path)["ok"] is True
    DatasetImageFilter.restore(tmp_path, "near-applied")
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 3


def test_review_rejection_list_is_validated_quarantined_and_restored(tmp_path):
    _write_dataset(tmp_path)

    report = DatasetImageFilter.run_review_rejections(
        tmp_path,
        ["batch_0000/0001.jpg"],
        apply=True,
        run_id="review",
    )

    assert report["quarantined"] == 1
    assert report["remaining"] == 2
    assert not (tmp_path / "batch_0000/0001.jpg").exists()
    assert DatasetImageFilter.verify(tmp_path)["ok"] is True
    DatasetImageFilter.restore(tmp_path, "review")
    assert (tmp_path / "batch_0000/0001.jpg").exists()


def test_review_rejection_list_rejects_unknown_active_paths(tmp_path):
    _write_dataset(tmp_path)

    try:
        DatasetImageFilter.run_review_rejections(
            tmp_path,
            ["batch_0000/missing.jpg"],
            run_id="review",
        )
    except ValueError as exc:
        assert "not active dataset records" in str(exc)
    else:
        raise AssertionError("unknown review path must be rejected")


def test_blip_review_thresholds_require_content_and_margin():
    thresholds = BLIPReviewThresholds(
        min_positive=0.5,
        min_content_evidence=0.2,
        min_margin=0.1,
    )

    assert thresholds.accepts(BLIPReviewScores(
        positive=0.9,
        content_evidence=0.8,
        negative=0.2,
        margin=0.7,
        positive_prompt="loading",
        negative_prompt="repair",
    ))
    assert not thresholds.accepts(BLIPReviewScores(
        positive=0.9,
        content_evidence=0.05,
        negative=0.2,
        margin=0.7,
        positive_prompt="opening a door",
        negative_prompt="repair",
    ))


def test_quarantine_review_is_non_mutating_and_reports_metrics(tmp_path):
    _write_dataset(tmp_path)
    _filter(tmp_path).run(apply=True, run_id="source")
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "path,label\n"
        "batch_0000/0000.jpg,irrelevant\n"
        "batch_0000/0002.jpg,relevant\n",
        encoding="utf-8",
    )

    class Reviewer:
        batch_size = 8
        model_name = "fake-blip"
        device = "cpu"
        cache_hits = 0
        cache_misses = 2

        def score_images(self, images):
            results = []
            for image in images:
                red, _, blue = np.asarray(image.resize((1, 1)))[0, 0]
                relevant = blue > red
                results.append(BLIPReviewScores(
                    positive=0.99 if relevant else 0.2,
                    content_evidence=0.8 if relevant else 0.1,
                    negative=0.1 if relevant else 0.9,
                    margin=0.8 if relevant else -0.7,
                    positive_prompt="loading",
                    negative_prompt="advertisement",
                ))
            return results

    report = run_quarantine_review(
        tmp_path,
        "source",
        Reviewer(),
        labels_path=labels,
        run_id="evaluation",
    )

    assert report["mutated_dataset"] is False
    assert report["candidates"] == 1
    assert report["metrics"]["precision"] == 1.0
    assert report["metrics"]["recall"] == 1.0
    assert len(list(tmp_path.glob("batch_*/*.jpg"))) == 1
    assert DatasetImageFilter.verify(tmp_path)["ok"] is True


def test_export_review_candidates_builds_independent_dataset(tmp_path):
    source = tmp_path / "source"
    _write_dataset(source)
    _filter(source).run(apply=True, run_id="source-run")
    output = tmp_path / "expanded"

    report = export_review_candidates(
        source,
        "source-run",
        ["batch_0000/0002.jpg"],
        output,
    )

    assert report["active_images"] == 1
    assert report["recovered_images"] == 1
    assert report["total_images"] == 2
    assert len(list(output.glob("batch_*/*.jpg"))) == 2
    assert len(list(source.glob("batch_*/*.jpg"))) == 1
    exported_metadata = [
        json.loads(line) for line in (output / "metadata.jsonl").read_text().splitlines()
    ]
    assert all(not Path(row["file_path"]).is_absolute() for row in exported_metadata)
    assert DatasetImageFilter.verify(output)["ok"] is True


def test_clip_feature_cache_reuses_image_embeddings(tmp_path):
    torch = pytest.importorskip("torch")

    class Encoder:
        def __init__(self):
            self.calls = 0

        def encode_images(self, images):
            self.calls += 1
            return torch.tensor([[1.0, 0.0, 0.0] for _ in images], dtype=torch.float16)

    prompts = PromptSet(("positive",), ("advertisement",), ("mismatch",))
    scorer = CLIPPromptScorer.__new__(CLIPPromptScorer)
    scorer.device = "cpu"
    scorer.model_name = "fake"
    scorer.prompts = prompts
    scorer.encoder = Encoder()
    scorer.cache_dir = tmp_path
    scorer.cache_hits = 0
    scorer.cache_misses = 0
    scorer._group_sizes = (1, 1, 1, 0)
    scorer._text_features = torch.eye(3, dtype=torch.float16)
    images = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "green")]

    first = scorer.score_images(images)
    second = scorer.score_images(images)

    assert scorer.encoder.calls == 1
    assert scorer.cache_hits == 2
    assert scorer.cache_misses == 2
    assert first == second
    assert first[0].relevance_prompt == "positive"
    assert first[0].scene_evidence == first[0].relevance
    assert first[0].scene_evidence_prompt == "positive"


def test_verify_cli_does_not_initialize_clip(tmp_path, monkeypatch):
    _write_dataset(tmp_path)

    def fail(*args, **kwargs):
        raise AssertionError("CLIP must not initialize for verification")

    monkeypatch.setattr("smart_spider.dataset_filter_cli.CLIPPromptScorer", fail)
    assert main(["--dataset", str(tmp_path), "--verify"]) == 0


def test_qr_signal_requires_successful_decode():
    analyzer = VisualSignalAnalyzer(enable_ocr=False)

    class Detector:
        decoded = ""

        def detectAndDecode(self, image):
            return self.decoded, object(), object()

    detector = Detector()
    analyzer._qr_detector = detector
    image = Image.new("RGB", (32, 32), "white")

    assert analyzer._detect_qr(image) is False
    detector.decoded = "https://example.com"
    assert analyzer._detect_qr(image) is True
