# coding=utf-8
"""Dataset governance regression tests."""
from PIL import Image

from smart_spider.dataset_governance import (
    LeakageSafeSplitter,
    QuotaLedger,
    SceneQuotaLedger,
    SplitItem,
    cluster_embeddings,
    hash_distance,
    perceptual_fingerprint,
)


def test_perceptual_fingerprint_has_stable_phash_and_dhash(tmp_path):
    path = tmp_path / "sample.png"
    Image.new("RGB", (48, 32), (30, 90, 160)).save(path)
    first = perceptual_fingerprint(path)
    second = perceptual_fingerprint(path)

    assert len(first.phash) == len(first.dhash) == 16
    assert first == second
    assert hash_distance(first.phash, second.phash) == 0


def test_embedding_clusters_are_deterministic():
    clusters = cluster_embeddings({
        "a": (1.0, 0.0, 0.0),
        "b": (0.99, 0.01, 0.0),
        "c": (0.0, 1.0, 0.0),
    }, similarity_threshold=0.98)
    assert clusters["a"] == clusters["b"]
    assert clusters["a"] != clusters["c"]


def test_splitter_never_separates_connected_provenance_groups():
    plan = LeakageSafeSplitter(seed="test").plan([
        SplitItem("a1", origin_url="https://origin-a.test/a", page_url="https://page.test/1", captured_at="2026-09-03T12:00:00Z"),
        SplitItem("a2", origin_url="https://origin-a.test/b", cluster_id="cluster-x"),
        SplitItem("a3", origin_url="https://other.test/c", cluster_id="cluster-x"),
        SplitItem("b1", origin_url="https://origin-b.test/a", page_url="https://page-b.test/1"),
    ])
    assert plan.assignments["a1"] == plan.assignments["a2"] == plan.assignments["a3"]
    assert set(plan.assignments) == {"a1", "a2", "a3", "b1"}
    assert plan.group_count == 2


def test_quota_ledgers_reserve_and_release_without_overflow():
    sources = QuotaLedger(10, max_share=0.2)
    assert sources.try_reserve("example.com")
    sources.commit("example.com")
    assert sources.try_reserve("example.com")
    sources.release("example.com")
    assert sources.try_reserve("example.com")
    sources.commit("example.com")
    assert not sources.try_reserve("example.com")

    scenes = SceneQuotaLedger({"smoking": 1})
    assert scenes.try_reserve("smoking")
    scenes.commit("smoking")
    assert scenes.reached("smoking")
    assert not scenes.try_reserve("smoking")
