# coding=utf-8
"""DatasetRepository crash-injection and idempotency E2E (S5)."""
from __future__ import annotations

import hashlib
import json

from PIL import Image

from smart_spider.dataset_contracts import LabelDecision, LabelPolicy, QualityMetrics, SampleRecord
from smart_spider.dataset_repository import DatasetRepository
from smart_spider.dataset_state import DatasetStateStore


def _image_bytes(color=(90, 40, 20)):
    image = Image.new("RGB", (24, 24), color)
    import io

    buf = io.BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def _build_records(index, absolute, relative, content_hash):
    sample = SampleRecord(
        sample_id=f"sha256:{content_hash}",
        file=relative,
        labels=LabelPolicy(mode="fixed", fixed_labels=["gate"]).resolve(
            [LabelDecision("gate", 1.0, "fixed")]
        ).labels,
        quality=QualityMetrics(
            modality="image",
            width=24,
            height=24,
            file_size=len(_image_bytes()),
            format="jpeg",
            validated=True,
        ),
        provenance={"url": f"https://example.com/{index}.jpg", "source": "e2e"},
        pipeline={"status": "accepted"},
        task_type="image_classification",
    )
    metadata = {
        "index": index,
        "file_path": absolute,
        "sha256": content_hash,
        "batch": relative.split("/", 1)[0],
    }
    return sample, metadata


def test_pending_reservation_aborted_on_restart(tmp_path):
    """Crash before prepare must abort pending on next repository open.

    Important: ``DatasetRepository.close()`` intentionally recovers incomplete
    commits. A hard crash is simulated by closing only the SQLite handle.
    """
    state_path = tmp_path / "state.sqlite3"
    state = DatasetStateStore(str(state_path))
    state.create_job("job-crash")
    repo = DatasetRepository(tmp_path, state, "job-crash", batch_size=10)
    content = _image_bytes()
    digest = hashlib.sha256(content).hexdigest()
    staged = repo.staging_dir / "crash.tmp"
    staged.write_bytes(content)
    reservation = state.reserve_dataset_item(
        "job-crash",
        content_hash=digest,
        source_hash=digest,
        staging_path=staged.relative_to(tmp_path).as_posix(),
        batch_size=10,
        filename_token="crash",
        extension=".jpg",
        max_count=5,
    )
    assert reservation.get("status") == "reserved"
    # Hard-crash simulation: drop DB connection without repository.close().
    state._conn.close()

    reopened_state = DatasetStateStore(str(state_path))
    reopened_state.create_job("job-crash")
    reopened = DatasetRepository(tmp_path, reopened_state, "job-crash", batch_size=10)
    try:
        assert reopened.recovery_report["pending_aborted"] >= 1
        assert reopened.active_count == 0
        metadata = tmp_path / "metadata.jsonl"
        assert (not metadata.exists()) or (not metadata.read_text().strip())
    finally:
        reopened.close()
        reopened_state.close()


def test_commit_idempotent_across_repository_instances(tmp_path):
    state = DatasetStateStore(str(tmp_path / "state.sqlite3"))
    state.create_job("job-idemp")
    content = _image_bytes((11, 22, 33))
    repo1 = DatasetRepository(tmp_path, state, "job-idemp", batch_size=5)
    first = repo1.commit_image(
        content,
        source_content=content,
        url="https://example.com/same.jpg",
        extension=".jpg",
        candidate_id=None,
        max_count=5,
        build_records=_build_records,
    )
    assert first.committed is True
    repo1.close()

    repo2 = DatasetRepository(tmp_path, state, "job-idemp", batch_size=5)
    try:
        second = repo2.commit_image(
            content,
            source_content=content,
            url="https://example.com/other-url-same-bytes.jpg",
            extension=".jpg",
            candidate_id=None,
            max_count=5,
            build_records=_build_records,
        )
        assert second.status == "duplicate"
        assert repo2.active_count == 1
        manifest_lines = (tmp_path / "manifest.jsonl").read_text().splitlines()
        assert len(manifest_lines) == 1
        SampleRecord.from_dict(json.loads(manifest_lines[0])).validate()
    finally:
        repo2.close()
        state.close()
