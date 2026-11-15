"""Offline triage preserves source assets and never manufactures training labels."""
import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from smart_spider.industrial_review import (
    CLASS_NAMES, LocalCLIPScorer, fingerprint, load_candidates, load_profile,
    screen_candidates, triage, write_results,
)


@pytest.fixture
def profile():
    return load_profile(Path(__file__).resolve().parents[1] / "examples/industrial_visual_profile.json")


def add_image(root, name="forklift", color="orange"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "test.png"
    Image.new("RGB", (40, 32), color).save(path)
    identifier = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    (folder / "manifest.jsonl").write_text(json.dumps({
        "id": identifier, "file": "test.png", "labels": [],
        "provenance": {"query": "misleading page text must not reach the model"},
    }) + "\n")
    return path


def scores(profile, *, positive=.35, negative=.20, photo=.31, style=.20):
    result = {name: {"score": positive if name.endswith("positive") else negative,
                     "prompt": prompts[0]} for name, prompts in profile["groups"].items()}
    result["photo"]["score"] = photo
    result["style_negative"]["score"] = style
    result["scene"]["score"] = .30
    return result


def test_high_similarity_is_only_a_review_priority(profile):
    decision = triage(scores(profile), "forklift", profile["thresholds"])
    assert decision["status"] == "priority_review"
    assert decision["advisory"] and not decision["training_ready"]
    assert "labels" not in decision and "bbox" not in decision


@pytest.mark.parametrize("changes,reason", [
    ({"photo": .19, "style": .36}, "illustration_advertisement_or_product_risk"),
    ({"positive": .20, "negative": .32}, "confusable_object_or_background"),
])
def test_style_and_confusable_objects_lower_priority(profile, changes, reason):
    decision = triage(scores(profile, **changes), "forklift", profile["thresholds"])
    assert decision["status"] == "lower_priority"
    assert reason in decision["reasons"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 4.0])
def test_invalid_scores_cannot_pass(profile, value):
    with pytest.raises(ValueError):
        triage(scores(profile, positive=value), "head", profile["thresholds"])


def test_hashes_are_checked_and_duplicates_merge(tmp_path):
    add_image(tmp_path, "person")
    add_image(tmp_path, "head")
    candidates, invalid = load_candidates([tmp_path, tmp_path])
    assert not invalid and len(candidates) == 1
    assert set(candidates[0]["retrieval_classes"]) == {"person", "head"}
    assert len(candidates[0]["sources"]) == 2
    (tmp_path / "person/test.png").write_bytes(b"tampered")
    candidates, invalid = load_candidates([tmp_path])
    assert len(invalid) == 1 and len(candidates) == 1
    assert "hash" in invalid[0]["reason"]
    assert candidates[0]["retrieval_classes"] == ["head"]


def test_path_escape_never_reads_outside_class(tmp_path):
    add_image(tmp_path)
    outside = tmp_path / "outside.png"
    Image.new("RGB", (10, 10)).save(outside)
    record = {"id": "sha256:" + hashlib.sha256(outside.read_bytes()).hexdigest(), "file": "../outside.png"}
    (tmp_path / "forklift/manifest.jsonl").write_text(json.dumps(record))
    candidates, invalid = load_candidates([tmp_path])
    assert not candidates and len(invalid) == 1


class StubScorer:
    metadata = {"model_id": "test", "revision": "sha256:test", "source": "fixture", "advisory": True}
    def __init__(self, profile):
        self.profile = profile
        self.images_seen = 0
    def score_images(self, images):
        assert all(isinstance(image, Image.Image) for image in images)
        self.images_seen += len(images)
        return [scores(self.profile) for image in images]


def test_inference_is_local_once_per_unique_image_and_leaves_manifests_untouched(tmp_path, profile):
    root = tmp_path / "dataset"
    image = add_image(root, "person")
    add_image(root, "head")
    original = image.read_bytes()
    manifests = {p: p.read_bytes() for p in root.glob("*/manifest.jsonl")}
    candidates, invalid = load_candidates([root])
    scorer = StubScorer(profile)
    records, errors = screen_candidates(candidates, scorer, profile)
    assert not invalid and not errors and scorer.images_seen == 1
    assert all(not decision["training_ready"] for decision in records[0]["class_evidence"].values())
    assert records[0]["model"] == scorer.metadata
    summary = write_results(tmp_path / "review", records, [], profile, scorer.metadata)
    assert summary["unique_images_scored"] == 1 and not summary["training_ready"]
    assert summary["triage_by_retrieval_class"]["head"]["priority_review"] == 1
    assert image.read_bytes() == original
    assert all(p.read_bytes() == content for p, content in manifests.items())
    assert (tmp_path / "review/review.html").exists()


def test_changed_or_corrupt_images_cannot_reach_encoder(tmp_path, profile):
    path = add_image(tmp_path)
    candidates, _ = load_candidates([tmp_path])
    path.write_bytes(b"changed")
    scorer = StubScorer(profile)
    records, errors = screen_candidates(candidates, scorer, profile)
    assert not records and len(errors) == 1 and scorer.images_seen == 0
    candidates[0]["sample_id"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    records, errors = screen_candidates(candidates, scorer, profile)
    assert not records and len(errors) == 1 and scorer.images_seen == 0


def test_missing_or_wrong_weights_fail_before_loading_model(tmp_path, profile):
    with pytest.raises(FileNotFoundError):
        LocalCLIPScorer(tmp_path / "missing.pt", profile)
    weights = tmp_path / "wrong.pt"
    weights.write_bytes(b"not trusted model bytes")
    with pytest.raises(ValueError, match="pinned"):
        LocalCLIPScorer(weights, profile)


def test_profile_changes_have_different_fingerprints(profile):
    before = fingerprint(profile)
    profile["groups"]["phone.positive"].append("a visibly held smartphone")
    assert fingerprint(profile) != before


def test_inference_failure_does_not_silently_accept(tmp_path, profile):
    add_image(tmp_path)
    candidates, _ = load_candidates([tmp_path])
    class BrokenScorer(StubScorer):
        def score_images(self, images):
            raise RuntimeError("inference failed")
    with pytest.raises(RuntimeError, match="inference failed"):
        screen_candidates(candidates, BrokenScorer(profile), profile)
