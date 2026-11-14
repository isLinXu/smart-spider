"""Cross-component image curation recovery, using tiny generated JPEG fixtures."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from smart_spider.dataset_contracts import SampleRecord
from smart_spider.dataset_crawler import DatasetCrawler
from smart_spider.dataset_filter import DatasetImageFilter
from smart_spider.dataset_repository import DatasetRepository
from smart_spider.dataset_state import DatasetStateStore


class SimulatedCrash(BaseException):
    """Bypass in-process exception cleanup like an abruptly terminated process."""


def image_bytes(value):
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), (value, 42, 80)).save(stream, format="JPEG")
    return stream.getvalue()


def records(index, absolute, relative, digest):
    return (
        SampleRecord(sample_id=f"sha256:{digest}", file=relative,
                     provenance={"url": f"https://images.example/{index}.jpg"},
                     pipeline={"status": "accepted"}),
        {"index": index, "file_path": absolute, "sha256": digest,
         "url": f"https://images.example/{index}.jpg", "source": "fixture", "keyword": "test"},
    )


def commit(repo, value, max_count=10):
    content = image_bytes(value)
    return repo.commit_image(content, source_content=content,
                             url=f"https://images.example/{value}.jpg", extension=".jpg",
                             candidate_id=None, max_count=max_count, build_records=records)


def seed(root, count=3, database=None):
    state = DatasetStateStore(str(database or root / ".dataset_state.sqlite3"))
    state.create_job("fixture")
    repo = DatasetRepository(root, state, "fixture")
    commits = [commit(repo, 30 + index * 40) for index in range(count)]
    repo.close()
    state.close()
    return commits


def manifest(root):
    return [json.loads(line) for line in (root / "manifest.jsonl").read_text().splitlines()]


def assert_inventory(repo, expected):
    items = repo.state.list_dataset_items(repo.job_id)
    assert repo.active_count == len(expected)
    assert {item["relative_path"] for item in items} == set(expected)
    assert {item["file"] for item in manifest(repo.root)} == set(expected)
    metadata = [json.loads(line) for line in repo.metadata_path.read_text().splitlines()]
    assert {repo._relative_path(row["file_path"]) for row in metadata} == set(expected)
    for item in items:
        assert hashlib.sha256((repo.root / item["relative_path"]).read_bytes()).hexdigest() == item["content_hash"]


def test_quarantine_restart_replenish_restore_preserves_new_samples(tmp_path):
    original = seed(tmp_path)
    rejected = original[0].relative_path
    DatasetImageFilter.run_review_rejections(tmp_path, [rejected], apply=True, run_id="review")
    repo = DatasetRepository.open_existing(tmp_path)
    assert_inventory(repo, [item.relative_path for item in original[1:]])
    assert commit(repo, 30).status == "duplicate"  # A rejected image stays excluded.
    added = commit(repo, 190, max_count=3)
    assert added.committed
    repo.close()
    assert DatasetImageFilter.verify(tmp_path, verify_hashes=True)["ok"]

    DatasetImageFilter.restore(tmp_path, "review")
    assert DatasetImageFilter.restore(tmp_path, "review")["restored"] == 1
    repo = DatasetRepository.open_existing(tmp_path)
    assert_inventory(repo, [item.relative_path for item in original] + [added.relative_path])
    assert [item["state"] for item in repo.state.list_dataset_operations("fixture")] == ["published", "published"]
    repo.close()


@pytest.mark.parametrize("kind", ["quarantine", "restore"])
@pytest.mark.parametrize("boundary", ["first_move", "database_commit", "views_written"])
def test_journal_recovers_every_curation_boundary(tmp_path, monkeypatch, kind, boundary):
    original = seed(tmp_path)
    rejected = [item.relative_path for item in original[:2]]
    if kind == "restore":
        DatasetImageFilter.run_review_rejections(tmp_path, rejected, apply=True, run_id="review")

    with monkeypatch.context() as patch:
        if boundary == "first_move":
            move = DatasetRepository._move_operation_asset

            def crash_after_move(self, plan):
                move(self, plan)
                raise SimulatedCrash()

            patch.setattr(DatasetRepository, "_move_operation_asset", crash_after_move)
        elif boundary == "database_commit":
            patch.setattr(DatasetRepository, "_publish_operation",
                          lambda *_: (_ for _ in ()).throw(SimulatedCrash()))
        else:
            patch.setattr(DatasetStateStore, "publish_dataset_operation",
                          lambda *_: (_ for _ in ()).throw(SimulatedCrash()))
        with pytest.raises(SimulatedCrash):
            if kind == "quarantine":
                DatasetImageFilter.run_review_rejections(tmp_path, rejected, apply=True, run_id="review")
            else:
                DatasetImageFilter.restore(tmp_path, "review")

    reopened = DatasetRepository.open_existing(tmp_path)
    expected = original[2:] if kind == "quarantine" else original
    assert_inventory(reopened, [item.relative_path for item in expected])
    assert all(operation["state"] == "published"
               for operation in reopened.state.list_dataset_operations("fixture"))
    assert DatasetImageFilter._read_active_runs(tmp_path) == reopened._active_runs()
    reopened.close()
    assert DatasetImageFilter.verify(tmp_path, verify_hashes=True)["ok"]


def test_restore_is_lifo_and_checks_hash_before_moving(tmp_path):
    original = seed(tmp_path)
    for index in range(2):
        DatasetImageFilter.run_review_rejections(
            tmp_path, [original[index].relative_path], apply=True, run_id=f"review{index}"
        )
    with pytest.raises(RuntimeError, match="latest"):
        DatasetImageFilter.restore(tmp_path, "review0")
    quarantined = tmp_path / "_quarantine/review1/files" / original[1].relative_path
    healthy = quarantined.read_bytes()
    quarantined.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        DatasetImageFilter.restore(tmp_path, "review1")
    assert not (tmp_path / original[1].relative_path).exists()
    quarantined.write_bytes(healthy)
    # Opening replays the already logged restore, then allows the previous run.
    repo = DatasetRepository.open_existing(tmp_path)
    assert repo.active_count == 2
    repo.close()
    DatasetImageFilter.restore(tmp_path, "review0")
    assert len(manifest(tmp_path)) == 3


@pytest.mark.parametrize("damage", ["missing", "corrupted"])
def test_missing_or_corrupted_committed_asset_blocks_resume(tmp_path, damage):
    original = seed(tmp_path, 1)
    path = tmp_path / original[0].relative_path
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="asset integrity"):
        DatasetCrawler(keywords=["test"], total_count=1, output_dir=str(tmp_path),
                       use_clip=False, resume=True, job_id="fixture")


@pytest.mark.parametrize("damage", ["missing", "truncated", "same_path_wrong_labels"])
def test_recovery_precedes_quota_restore_and_rebuilds_full_records(tmp_path, damage):
    original = seed(tmp_path, 1)
    metadata = tmp_path / "metadata.jsonl"
    if damage == "missing":
        metadata.unlink()
    elif damage == "truncated":
        metadata.write_text("{partial")
    else:
        row = manifest(tmp_path)[0]
        row["labels"] = [{"name": "tampered"}]
        (tmp_path / "manifest.jsonl").write_text(json.dumps(row) + "\n")
    crawler = DatasetCrawler(keywords=["test"], total_count=3, output_dir=str(tmp_path),
                             use_clip=False, resume=True, job_id="fixture")
    try:
        assert crawler._dir_manager.saved_count == 1
        assert crawler._source_quotas.snapshot()["fixture"]["accepted"] == 1
        assert crawler._domain_quotas.snapshot()["images.example"]["accepted"] == 1
        assert_inventory(crawler._repository, [original[0].relative_path])
        assert manifest(tmp_path)[0]["labels"] == []
    finally:
        crawler.close()


def test_custom_database_binding_cannot_be_redirected_to_another_root(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    seed(first, 1, database=tmp_path / "external.sqlite3")
    seed(second, 1)
    foreign = json.loads((first / DatasetRepository.BINDING_FILE).read_text())
    foreign["root"] = str(second.resolve())
    (second / DatasetRepository.BINDING_FILE).write_text(json.dumps(foreign))
    with pytest.raises(ValueError, match="another output root"):
        DatasetRepository.open_existing(second)
    assert len(manifest(first)) == len(manifest(second)) == 1


def test_persisted_move_cannot_escape_dataset_root(tmp_path):
    root = tmp_path / "dataset"
    seed(root, 1)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"do not move")
    repo = DatasetRepository.open_existing(root)
    item = repo.state.list_dataset_items("fixture")[0]
    repo.state.prepare_dataset_operation("fixture", "unsafe", "quarantine", {
        "moves": [{"item_id": item["item_id"], "relative_path": item["relative_path"],
                   "content_hash": item["content_hash"], "source": "../outside.jpg",
                   "destination": "_quarantine/unsafe/files/a.jpg"}],
    })
    repo.state.close()  # Simulate process exit before recovery.
    with pytest.raises(ValueError, match="escapes root"):
        DatasetRepository.open_existing(root)
    assert outside.read_bytes() == b"do not move"
    assert (root / item["relative_path"]).is_file()


def test_existing_prejournal_quarantine_is_adopted_without_resurrection(tmp_path):
    original = seed(tmp_path, 3)
    # Run the legacy filesystem path, reproducing a dataset filtered by an older release.
    marker = tmp_path / DatasetRepository.BINDING_FILE
    binding = marker.read_bytes()
    marker.unlink()
    database = tmp_path / ".dataset_state.sqlite3"
    offline = tmp_path / "legacy-hidden.sqlite3"
    database.rename(offline)
    for index in range(2):
        DatasetImageFilter.run_review_rejections(
            tmp_path, [original[index].relative_path], apply=True, run_id=f"old{index}"
        )
    offline.rename(database)
    marker.write_bytes(binding)
    repo = DatasetRepository.open_existing(tmp_path)
    assert_inventory(repo, [original[2].relative_path])
    assert len(repo.state.list_dataset_operations("fixture")) == 2
    repo.close()
    DatasetImageFilter.restore(tmp_path, "old1")
    DatasetImageFilter.restore(tmp_path, "old0")
    assert len(manifest(tmp_path)) == 3


def test_commit_guard_blocks_recovery_before_publishing(tmp_path):
    original = seed(tmp_path, 1)
    state = DatasetStateStore(str(tmp_path / ".dataset_state.sqlite3"))

    def expired():
        raise RuntimeError("stale lease")

    (tmp_path / "manifest.jsonl").unlink()
    with pytest.raises(RuntimeError, match="stale lease"):
        DatasetRepository(tmp_path, state, "fixture", commit_guard=expired)
    assert not (tmp_path / "manifest.jsonl").exists()
    assert (tmp_path / original[0].relative_path).is_file()
    state.close()


def test_lease_loss_on_close_still_releases_local_resources(tmp_path):
    from contextlib import contextmanager
    from unittest.mock import Mock

    expired = False

    @contextmanager
    def guard():
        if expired:
            raise RuntimeError("stale lease")
        yield

    crawler = DatasetCrawler(keywords=["test"], total_count=1, output_dir=str(tmp_path),
                             use_clip=False, commit_guard=guard)
    resources = [crawler._metadata_writer, crawler._manifest_writer,
                 crawler._dedup, crawler._state_store]
    for resource in resources:
        resource.close = Mock(wraps=resource.close)
    expired = True
    with pytest.raises(RuntimeError, match="stale lease"):
        crawler._generate_report()
    assert not (tmp_path / "_dataset_report.json").exists()
    assert not (tmp_path / "lineage.json").exists()
    with pytest.raises(RuntimeError, match="stale lease"):
        crawler.close()
    for resource in resources:
        resource.close.assert_called_once_with()
    assert crawler._state_store is None
    crawler.close()


def test_empty_quarantine_remains_valid_and_can_be_restored(tmp_path):
    original = seed(tmp_path, 1)
    repo = DatasetRepository.open_existing(tmp_path)
    repo.quarantine("empty", [])
    assert_inventory(repo, [original[0].relative_path])
    assert repo.restore_quarantine("empty")["restored"] == 0
    repo.close()


def test_legacy_without_database_still_restores_exact_views(tmp_path):
    batch = tmp_path / "batch_0000"
    batch.mkdir()
    path = batch / "0000.jpg"
    path.write_bytes(image_bytes(30))
    metadata = {"index": 0, "file_path": "batch_0000/0000.jpg"}
    before = json.dumps(metadata) + "\n"
    (tmp_path / "metadata.jsonl").write_text(before)
    (tmp_path / "manifest.jsonl").write_text(json.dumps({"id": "legacy", "file": metadata["file_path"]}) + "\n")
    DatasetImageFilter.run_review_rejections(tmp_path, [metadata["file_path"]], apply=True, run_id="legacy")
    assert not path.exists()
    DatasetImageFilter.restore(tmp_path, "legacy")
    assert path.is_file()
    assert (tmp_path / "metadata.jsonl").read_text() == before
    assert not (tmp_path / ".dataset_state.sqlite3").exists()


def test_first_database_import_preserves_an_already_filtered_legacy_dataset(tmp_path):
    batch = tmp_path / "batch_0000"
    batch.mkdir()
    rows = []
    for index in range(2):
        relative = f"batch_0000/{index:04d}.jpg"
        (tmp_path / relative).write_bytes(image_bytes(30 + index * 40))
        rows.append({"index": index, "file_path": relative})
    (tmp_path / "metadata.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    DatasetImageFilter.run_review_rejections(
        tmp_path, [rows[0]["file_path"]], apply=True, run_id="legacy"
    )
    state = DatasetStateStore(str(tmp_path / ".dataset_state.sqlite3"))
    state.create_job("imported")
    repo = DatasetRepository(tmp_path, state, "imported")
    assert_inventory(repo, [rows[1]["file_path"]])
    assert len(state.list_dataset_items("imported", ("quarantined",))) == 1
    repo.restore_quarantine("legacy")
    assert_inventory(repo, [row["file_path"] for row in rows])
    repo.close()
    state.close()


def test_empty_authoritative_dataset_repairs_truncated_views_without_importing_orphans(tmp_path):
    seed(tmp_path, 0)
    (tmp_path / "metadata.jsonl").write_text("{partial")
    (tmp_path / "orphan.jpg").write_bytes(image_bytes(30))
    repo = DatasetRepository.open_existing(tmp_path)
    assert_inventory(repo, [])
    repo.close()
    assert (tmp_path / "orphan.jpg").exists()
