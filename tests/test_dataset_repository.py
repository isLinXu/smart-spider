# coding=utf-8
"""事务化数据集仓库测试。"""
import hashlib
import json

from PIL import Image

from smart_spider.dataset_contracts import LabelDecision, LabelPolicy, QualityMetrics, SampleRecord
from smart_spider.dataset_repository import DatasetRepository
from smart_spider.dataset_state import DatasetStateStore


def _build_records(index, absolute, relative, content_hash):
    sample = SampleRecord(
        sample_id=f"sha256:{content_hash}",
        file=relative,
        labels=(LabelPolicy(mode="fixed", fixed_labels=["测试"]).resolve([
            LabelDecision("测试", 1.0, "fixed")
        ]).labels),
        quality=QualityMetrics(
            modality="image", width=32, height=32, file_size=100,
            format="jpeg", validated=True,
        ),
        provenance={"url": f"https://example.com/{index}.jpg", "source": "test"},
        pipeline={"status": "accepted"},
        task_type="image_classification",
    )
    metadata = {
        "index": index,
        "file_path": absolute,
        "sha256": content_hash,
        "batch": relative.split("/", 1)[0],
        "labels": [item.to_dict() for item in sample.labels],
    }
    return sample, metadata


def _image_bytes(color=(120, 30, 80)):
    image = Image.new("RGB", (32, 32), color)
    import io
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def test_repository_commits_final_hash_once_and_publishes_views(tmp_path):
    state = DatasetStateStore(str(tmp_path / "state.sqlite3"))
    state.create_job("job-1")
    repo = DatasetRepository(tmp_path, state, "job-1", batch_size=2)
    content = _image_bytes()
    try:
        first = repo.commit_image(
            content,
            source_content=content,
            url="https://example.com/a.jpg",
            extension=".jpg",
            candidate_id=None,
            max_count=2,
            build_records=_build_records,
        )
        duplicate = repo.commit_image(
            content,
            source_content=content,
            url="https://example.com/b.jpg",
            extension=".jpg",
            candidate_id=None,
            max_count=2,
            build_records=_build_records,
        )
        assert first.committed is True
        assert duplicate.status == "duplicate"
        assert repo.active_count == 1
        metadata = [json.loads(line) for line in (tmp_path / "metadata.jsonl").read_text().splitlines()]
        manifest = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text().splitlines()]
        assert len(metadata) == len(manifest) == 1
        expected_name = f"batch_0000/0000_{hashlib.sha256('https://example.com/a.jpg'.encode()).hexdigest()[:8]}.jpg"
        assert metadata[0]["file_path"] == str(tmp_path / expected_name)
        assert state.dataset_item_count("job-1") == 1
        assert state.dataset_item_count("job-1", ("pending", "prepared", "committed")) == 1
    finally:
        repo.close()
        state.close()


def test_repository_recovers_prepared_item_after_restart(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    state = DatasetStateStore(str(state_path))
    state.create_job("job-1")
    repo = DatasetRepository(tmp_path, state, "job-1")
    content = _image_bytes((20, 180, 40))
    digest = hashlib.sha256(content).hexdigest()
    staged = repo.staging_dir / "prepared.tmp"
    staged.write_bytes(content)
    reservation = state.reserve_dataset_item(
        "job-1",
        content_hash=digest,
        source_hash=digest,
        staging_path=staged.relative_to(tmp_path).as_posix(),
        batch_size=100,
        filename_token="recovery",
        extension=".jpg",
        max_count=1,
    )
    sample, metadata = _build_records(
        reservation["index"],
        str(tmp_path / reservation["relative_path"]),
        reservation["relative_path"],
        digest,
    )
    state.prepare_dataset_item(reservation["item_id"], metadata, sample.to_dict())
    state.close()

    reopened_state = DatasetStateStore(str(state_path))
    reopened_state.create_job("job-1")
    reopened = DatasetRepository(tmp_path, reopened_state, "job-1")
    try:
        assert reopened.recovery_report["prepared_recovered"] == 1
        assert reopened.active_count == 1
        assert (tmp_path / reservation["relative_path"]).is_file()
        assert len((tmp_path / "metadata.jsonl").read_text().splitlines()) == 1
    finally:
        reopened.close()
        reopened_state.close()
