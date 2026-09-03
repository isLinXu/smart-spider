# coding=utf-8
"""SQLite 数据集状态库测试。"""
import os
import tempfile
import time

from smart_spider.dataset_contracts import CandidateResource, SampleRecord
from smart_spider.dataset_state import DatasetStateStore


def test_candidate_is_idempotent_and_can_be_claimed_and_completed():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "state.sqlite3")
        with DatasetStateStore(db_path) as store:
            store.create_job("job-1", {"labels": ["cat"]})
            resource = CandidateResource("https://example.com/a.jpg", "bing", query="cat")
            first = store.add_candidate("job-1", resource)
            second = store.add_candidate("job-1", resource)
            assert first == second
            assert store.count_candidates("job-1") == 1

            claimed = store.claim_candidates("job-1", limit=1, lease_seconds=60)
            assert len(claimed) == 1
            assert claimed[0]["state"] == "leased"
            assert claimed[0]["attempts"] == 1

            store.complete_candidate(first)
            assert store.get_candidate(first)["state"] == "fetched"


def test_failed_candidate_retries_then_becomes_terminal():
    with tempfile.TemporaryDirectory() as tmp:
        with DatasetStateStore(os.path.join(tmp, "state.sqlite3")) as store:
            store.create_job("job-1")
            resource = CandidateResource("https://example.com/retry.jpg", "bing")
            candidate_id = store.add_candidate("job-1", resource)
            store.claim_candidates("job-1", lease_seconds=60)
            assert store.fail_candidate(candidate_id, "timeout", max_retries=2, retry_delay=0) == "retry_wait"
            store.claim_candidates("job-1", lease_seconds=60)
            assert store.fail_candidate(candidate_id, "timeout", max_retries=2, retry_delay=0) == "failed"
            assert store.get_candidate(candidate_id)["state"] == "failed"


def test_expired_lease_is_recoverable():
    with tempfile.TemporaryDirectory() as tmp:
        with DatasetStateStore(os.path.join(tmp, "state.sqlite3")) as store:
            store.create_job("job-1")
            candidate_id = store.add_candidate(
                "job-1", CandidateResource("https://example.com/lease.jpg", "bing")
            )
            store.claim_candidates("job-1", lease_seconds=0)
            time.sleep(0.01)
            assert store.recover_expired_leases("job-1") == 1
            assert store.get_candidate(candidate_id)["state"] == "retry_wait"


def test_sample_submission_is_idempotent_by_content_hash():
    with tempfile.TemporaryDirectory() as tmp:
        with DatasetStateStore(os.path.join(tmp, "state.sqlite3")) as store:
            store.create_job("job-1")
            sample = SampleRecord(sample_id="sample-1", file="images/1.jpg")
            assert store.add_sample("job-1", sample, content_hash="same") is True
            assert store.add_sample("job-1", SampleRecord(sample_id="sample-2", file="images/2.jpg"), content_hash="same") is False
            assert len(store.list_events("job-1")) == 1


def test_event_payload_is_compact_and_events_can_be_pruned():
    with tempfile.TemporaryDirectory() as tmp:
        with DatasetStateStore(os.path.join(tmp, "state.sqlite3")) as store:
            store.create_job("job-1")
            resource = CandidateResource("https://example.com/a.jpg", "bing", query="cat")
            store.add_candidate("job-1", resource)
            event = store.list_events("job-1")[0]
            assert set(event["payload"]) <= {"url", "source", "query", "modality"}
            assert store.prune_events("job-1", keep_latest=0) == 1
            assert store.list_events("job-1") == []


def test_dataset_item_reservation_is_unique_and_monotonic():
    with tempfile.TemporaryDirectory() as tmp:
        with DatasetStateStore(os.path.join(tmp, "state.sqlite3")) as store:
            store.create_job("job-1")
            first = store.reserve_dataset_item(
                "job-1",
                content_hash="hash-a",
                source_hash="source-a",
                staging_path=".dataset_staging/a.tmp",
                batch_size=100,
                filename_token="token-a",
                extension=".jpg",
                max_count=2,
            )
            duplicate = store.reserve_dataset_item(
                "job-1",
                content_hash="hash-a",
                source_hash="source-a",
                staging_path=".dataset_staging/b.tmp",
                batch_size=100,
                filename_token="token-b",
                extension=".jpg",
                max_count=2,
            )
            second = store.reserve_dataset_item(
                "job-1",
                content_hash="hash-b",
                source_hash="source-b",
                staging_path=".dataset_staging/c.tmp",
                batch_size=100,
                filename_token="token-c",
                extension=".jpg",
                max_count=2,
            )
            assert first["status"] == "reserved"
            assert duplicate["status"] == "duplicate"
            assert second["index"] == 1
            assert store.dataset_next_index("job-1") == 2
