"""Ownership, renewal and short publication fencing across queue backends."""
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import threading
import time
import uuid

import pytest

from smart_spider.pipeline.local_queue import LocalSqliteTaskQueue
from smart_spider.pipeline.protocols import StaleClaimError
from smart_spider.pipeline.redis_queue import RedisTaskQueue
from smart_spider.task_execution import TaskExecutionContext, dataset_writer_lock
from smart_spider.worker import run_worker
from tests.test_pipeline_plugins import FakeRedis


@pytest.fixture(params=["sqlite", "redis-fake"])
def queue(request, tmp_path):
    if request.param == "sqlite":
        return LocalSqliteTaskQueue(str(tmp_path / "queue.db"))
    return RedisTaskQueue(client=FakeRedis())


def credentials(claim):
    return {"claim_token": claim.claim_token, "worker_id": claim.claimed_by}


def test_old_owner_cannot_complete_or_renew_reclaimed_task(queue):
    task = queue.enqueue("demo", {})
    first = queue.claim(worker_id="first")
    assert queue.recover_expired_claims(now=first.lease_until + 1) == 1
    second = queue.claim(worker_id="second")
    assert second.claim_token != first.claim_token
    assert second.claim_generation == first.claim_generation + 1
    for action in (queue.complete, queue.renew):
        with pytest.raises(StaleClaimError):
            action(task.task_id, **credentials(first))
    current = queue.get(task.task_id)
    assert current.status == "running"
    assert current.claimed_by == "second"
    queue.complete(task.task_id, **credentials(second))


def test_wrong_or_empty_owner_credentials_leave_task_unchanged(queue):
    task = queue.enqueue("demo", {})
    claim = queue.claim(worker_id="owner")
    for token, owner in [("", "owner"), (claim.claim_token, "other"), ("wrong", "owner")]:
        with pytest.raises(StaleClaimError):
            queue.complete(task.task_id, claim_token=token, worker_id=owner)
    assert queue.get(task.task_id).claim_token == claim.claim_token


def test_renew_extends_lease_without_new_attempt(queue):
    task = queue.enqueue("demo", {})
    claim = queue.claim(worker_id="owner", lease_seconds=1)
    renewed = queue.renew(task.task_id, lease_seconds=30, **credentials(claim))
    assert renewed.lease_until > claim.lease_until
    assert renewed.attempts == claim.attempts == 1
    assert renewed.claim_token == claim.claim_token
    assert queue.recover_expired_claims(now=claim.lease_until + 1) == 0


def test_expired_claim_cannot_complete_or_renew_even_before_recovery(queue, monkeypatch):
    task = queue.enqueue("demo", {})
    claim = queue.claim()
    monkeypatch.setattr(time, "time", lambda: claim.lease_until + 1)
    for action in (queue.complete, queue.renew):
        with pytest.raises(StaleClaimError):
            action(task.task_id, **credentials(claim))


@pytest.mark.parametrize("lease", [0, -1, float("nan"), float("inf")])
def test_invalid_lease_is_rejected(queue, lease):
    queue.enqueue("demo", {})
    with pytest.raises(ValueError):
        queue.claim(lease_seconds=lease)
    claim = queue.claim()
    with pytest.raises(ValueError):
        queue.renew(claim.task_id, lease_seconds=lease, **credentials(claim))


def test_sqlite_migrates_existing_queue_without_changing_payload(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                     "payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', "
                     "created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO tasks VALUES('legacy','demo','{\"v\":1}','pending','',1,1)")
    queue = LocalSqliteTaskQueue(path)
    record = queue.get("legacy")
    assert record.payload == {"v": 1}
    assert record.claim_generation == 0
    claim = queue.claim(worker_id="migrated")
    assert claim.claim_generation == 1
    assert claim.claim_token
    queue.complete(claim.task_id, **credentials(claim))


def test_sqlite_publish_guard_excludes_recovery_and_rejects_stale_writes(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "queue.db"))
    other = LocalSqliteTaskQueue(queue.db_path)
    queue.enqueue("demo", {})
    claim = queue.claim()
    started = threading.Event()
    finished = threading.Event()
    results = []

    def recover():
        started.set()
        results.append(other.recover_expired_claims(now=claim.lease_until + 1))
        finished.set()

    asset = tmp_path / "asset.bin"
    with queue.lease_guard(claim.task_id, **credentials(claim)):
        thread = threading.Thread(target=recover, daemon=True)
        thread.start()
        assert started.wait(1)
        assert not finished.wait(0.05)
        asset.write_bytes(b"valid-owner-publish")
    thread.join(timeout=2)
    assert finished.is_set()
    assert results == [1]
    with pytest.raises(StaleClaimError):
        with queue.lease_guard(claim.task_id, **credentials(claim)):
            asset.write_bytes(b"stale-publish")
    assert asset.read_bytes() == b"valid-owner-publish"


@pytest.mark.parametrize("state_db", [None, "state.db"])
def test_worker_heartbeat_keeps_long_task_owned(tmp_path, monkeypatch, state_db):
    queue = LocalSqliteTaskQueue(str(tmp_path / "queue.db"))
    task = queue.enqueue("dataset_crawl", {"config": {
        "keywords": ["fixture"], "total_count": 1, "use_clip": False,
        "output_dir": str(tmp_path / "out"),
        "state_db": None if state_db is None else str(tmp_path / state_db),
    }})
    commits = []

    class Crawler:
        @classmethod
        def from_config(cls, config, *, commit_guard, check_active):
            instance = cls()
            instance.guard = commit_guard
            instance.check = check_active
            return instance

        def crawl(self):
            threading.Event().wait(0.35)  # More than two initial lease periods.
            assert queue.recover_expired_claims() == 0
            self.check()
            with self.guard():
                commits.append("published")

    monkeypatch.setattr("smart_spider.worker.DatasetCrawler", Crawler)
    assert run_worker(queue, once=True, lease_seconds=0.15) == 1
    assert queue.get(task.task_id).status == "succeeded"
    assert queue.get(task.task_id).attempts == 1
    assert commits == ["published"]


def test_lost_heartbeat_blocks_new_publication(tmp_path, monkeypatch):
    queue = LocalSqliteTaskQueue(str(tmp_path / "queue.db"))
    queue.enqueue("demo", {})
    claim = queue.claim(lease_seconds=30)
    renewed = threading.Event()

    def fail_renew(*args, **kwargs):
        renewed.set()
        raise ConnectionError("synthetic renewal failure")

    monkeypatch.setattr(queue, "renew", fail_renew)
    context = TaskExecutionContext(queue, claim, lease_seconds=0.03)
    with context:
        assert renewed.wait(1)
        assert context._lost.wait(1)
        with pytest.raises(StaleClaimError):
            context.check_active()
        with pytest.raises(StaleClaimError):
            with context.commit_guard():
                pytest.fail("publication must not start after failed renewal")


def test_dataset_writer_lock_rejects_concurrent_writer(tmp_path):
    with dataset_writer_lock(str(tmp_path)):
        with pytest.raises(RuntimeError, match="another worker"):
            with dataset_writer_lock(str(tmp_path)):
                pytest.fail("second writer entered")
    with dataset_writer_lock(str(tmp_path)):
        pass  # Closing the owner releases the retained lock-file inode.


def test_repository_publication_is_fenced_after_queue_reclaims_owner(tmp_path):
    from smart_spider.dataset_contracts import SampleRecord
    from smart_spider.dataset_repository import DatasetRepository
    from smart_spider.dataset_state import DatasetStateStore

    queue = LocalSqliteTaskQueue(str(tmp_path / "queue.db"))
    queue.enqueue("dataset_crawl", {})
    old = queue.claim(worker_id="old")
    old_context = TaskExecutionContext(queue, old, lease_seconds=300)
    state = DatasetStateStore(str(tmp_path / "state.db"))
    state.create_job("fixture")
    root = tmp_path / "dataset"
    repo = DatasetRepository(root, state, "fixture", commit_guard=old_context.commit_guard)

    def publish(repository, content):
        return repository.commit_image(
            content, source_content=content, url="https://example.com/fixture.bin",
            extension=".bin", candidate_id=None, max_count=3,
            build_records=lambda index, absolute, relative, digest: (
                SampleRecord(sample_id=digest, file=relative),
                {"file_path": absolute, "sha256": digest},
            ),
        )

    accepted = publish(repo, b"first small synthetic fixture")
    assert accepted.committed
    initial_manifest = repo.manifest_path.read_bytes()
    queue.recover_expired_claims(now=old.lease_until + 1)
    new = queue.claim(worker_id="new")
    with pytest.raises(StaleClaimError):
        publish(repo, b"stale synthetic fixture")
    assert state.dataset_item_count("fixture") == 1
    assert repo.manifest_path.read_bytes() == initial_manifest
    assert (root / accepted.relative_path).read_bytes() == b"first small synthetic fixture"
    assert list(repo.staging_dir.iterdir()) == []
    with pytest.raises(StaleClaimError):
        repo.close()
    new_context = TaskExecutionContext(queue, new, lease_seconds=300)
    current = DatasetRepository(root, state, "fixture", commit_guard=new_context.commit_guard)
    assert publish(current, b"current small synthetic fixture").committed
    assert current.active_count == 2
    current.close()
    state.close()


@pytest.mark.parametrize("backend", ["redis", "legacy", "same_database", "linked_database"])
def test_worker_refuses_unfenced_dataset_before_creating_crawler(tmp_path, monkeypatch, backend):
    queue = (RedisTaskQueue(client=FakeRedis()) if backend == "redis"
             else LocalSqliteTaskQueue(str(tmp_path / "queue.db")))
    state_db = str(tmp_path / "state.db")
    if backend == "legacy":
        state_db = ""
    elif backend == "same_database":
        state_db = queue.db_path
    elif backend == "linked_database":
        os.link(queue.db_path, state_db)
    task = queue.enqueue("dataset_crawl", {"config": {
        "keywords": ["fixture"], "total_count": 1, "use_clip": False,
        "output_dir": str(tmp_path / "out"),
        "state_db": state_db,
    }})

    def construct(*args, **kwargs):
        pytest.fail("unfenced task must not instantiate a crawler")

    monkeypatch.setattr("smart_spider.worker.DatasetCrawler.from_config", construct)
    run_worker(queue, once=True)
    assert queue.get(task.task_id).status == "dead"
    assert not (tmp_path / "out").exists()


def test_redis_crash_before_exec_leaves_task_claimable():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client)
    task = queue.enqueue("demo", {})

    def crash(_pipe):
        raise RuntimeError("synthetic process failure before EXEC")

    client.before_execute = crash
    with pytest.raises(RuntimeError, match="synthetic process failure"):
        queue.claim()
    assert queue.get(task.task_id).status == "pending"
    claim = queue.claim()
    assert claim.task_id == task.task_id
    assert claim.attempts == 1
    assert client.zsets[queue._key("running")][task.task_id] == claim.lease_until


def test_redis_watch_conflict_retries_without_losing_or_double_claiming():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client)
    task = queue.enqueue("demo", {})
    client.before_execute = lambda _pipe: client.hset(
        queue._key("task", task.task_id), mapping={"updated_at": "123"},
    )
    claim = queue.claim()
    assert claim.task_id == task.task_id
    assert claim.attempts == 1
    assert queue.claim() is None
    assert client.zsets[queue._key("running")][task.task_id] == claim.lease_until


def test_redis_recovery_conflict_preserves_concurrent_renewal():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client)
    task = queue.enqueue("demo", {})
    claim = queue.claim(lease_seconds=1)
    client.before_execute = lambda _pipe: queue.renew(
        task.task_id, lease_seconds=60, **credentials(claim),
    )
    assert queue.recover_expired_claims(now=claim.lease_until + 1) == 0
    assert queue.get(task.task_id).status == "running"
    assert queue.claim() is None


def test_redis_completion_conflict_rejects_old_owner():
    client = FakeRedis()
    queue = RedisTaskQueue(client=client)
    task = queue.enqueue("demo", {})
    claim = queue.claim(worker_id="old")
    replacement = []

    def replace(_pipe):
        queue.recover_expired_claims(now=claim.lease_until + 1)
        replacement.append(queue.claim(worker_id="new"))

    client.before_execute = replace
    with pytest.raises(StaleClaimError):
        queue.complete(task.task_id, **credentials(claim))
    assert queue.get(task.task_id).claim_token == replacement[0].claim_token


def test_real_redis_claim_renew_and_stale_completion():
    url = os.environ.get("SMART_SPIDER_TEST_REDIS_URL")
    if not url:
        pytest.skip("set SMART_SPIDER_TEST_REDIS_URL for real Redis integration")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(url, decode_responses=True)
    client.ping()
    prefix = "smart_spider_test_" + uuid.uuid4().hex
    queue = RedisTaskQueue(client=client, prefix=prefix)
    try:
        task = queue.enqueue("demo", {})
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda worker: queue.claim(worker_id=worker), ["A", "B"]))
        claim = next(item for item in claims if item is not None)
        assert sum(item is not None for item in claims) == 1
        queue.renew(task.task_id, **credentials(claim))
        queue.recover_expired_claims(now=time.time() + 1000)
        current = queue.claim(worker_id="replacement")
        with pytest.raises(StaleClaimError):
            queue.complete(task.task_id, **credentials(claim))
        queue.complete(task.task_id, **credentials(current))
    finally:
        keys = list(client.scan_iter(match=prefix + ":*"))
        if keys:
            client.delete(*keys)
