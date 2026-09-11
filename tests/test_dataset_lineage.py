# coding=utf-8
"""Dataset lineage and manifest checksum tests (S10)."""
from pathlib import Path

from smart_spider.dataset_config import DatasetCrawlConfig
from smart_spider.dataset_lineage import (
    build_lineage,
    config_fingerprint,
    load_lineage,
    publish_dataset_artifacts,
    verify_manifest_checksum,
    write_manifest_checksum,
)
from smart_spider.dataset_state import DatasetStateStore


def test_config_fingerprint_is_stable():
    left = DatasetCrawlConfig(keywords=["cat"], total_count=10, output_dir="./a")
    right = DatasetCrawlConfig(keywords=["cat"], total_count=10, output_dir="./a")
    assert left.fingerprint() == right.fingerprint() == config_fingerprint(left.to_dict())
    changed = DatasetCrawlConfig(keywords=["dog"], total_count=10, output_dir="./a")
    assert changed.fingerprint() != left.fingerprint()


def test_publish_lineage_and_verify_checksum(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"id":"sha256:abc","file":"batch_0000/0000.jpg"}\n', encoding="utf-8")
    lineage = build_lineage(
        job_id="job-1",
        output_dir=tmp_path,
        config_snapshot={"keywords": ["cat"], "clip_model": "ViT-B/32"},
        clip_model="ViT-B/32",
    )
    published = publish_dataset_artifacts(tmp_path, lineage)
    assert published["lineage"]["dataset_id"].startswith("ds_")
    assert published["checksum"]["sha256"]
    assert (tmp_path / ".dataset_lineage.json").is_file()
    assert (tmp_path / "manifest.sha256").is_file()
    assert verify_manifest_checksum(tmp_path)["ok"] is True
    loaded = load_lineage(tmp_path)
    assert loaded is not None
    assert loaded.config_fingerprint == lineage.config_fingerprint
    assert loaded.provenance["url_normalize_version"]


def test_checksum_detects_manifest_tamper(tmp_path):
    (tmp_path / "manifest.jsonl").write_text('{"id":"1"}\n', encoding="utf-8")
    write_manifest_checksum(tmp_path)
    assert verify_manifest_checksum(tmp_path)["ok"] is True
    (tmp_path / "manifest.jsonl").write_text('{"id":"2"}\n', encoding="utf-8")
    assert verify_manifest_checksum(tmp_path)["ok"] is False


def test_lease_recovery_counted_in_stats(tmp_path):
    db = tmp_path / "state.sqlite3"
    with DatasetStateStore(str(db)) as store:
        store.create_job("job-1")
        from smart_spider.dataset_contracts import CandidateResource
        import time

        candidate_id = store.add_candidate(
            "job-1",
            CandidateResource("https://example.com/a.jpg", "bing"),
        )
        store.claim_candidates("job-1", lease_seconds=0)
        time.sleep(0.01)
        recovered = store.recover_expired_leases("job-1")
        assert recovered == 1
        stats = store.collect_stats()
        assert stats["lease_recoveries_total"] == 1
        assert store.get_candidate(candidate_id)["state"] == "retry_wait"
