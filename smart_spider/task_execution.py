"""Lease heartbeat and guarded local publication for queue workers."""
from __future__ import annotations

from contextlib import contextmanager
import math
import os
import threading
import time

from .pipeline.protocols import StaleClaimError


class TaskExecutionContext:
    def __init__(self, queue, task, *, lease_seconds: float):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and positive")
        self.queue = queue
        self.task = task
        self.lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None

    @property
    def claim_kwargs(self):
        return {"claim_token": self.task.claim_token, "worker_id": self.task.claimed_by}

    def check_active(self):
        if self._lost.is_set():
            raise StaleClaimError(f"task lease lost: {self.task.task_id}")
        current = self.queue.get(self.task.task_id)
        if (current is None or current.status != "running"
                or current.claim_token != self.task.claim_token
                or current.claimed_by != self.task.claimed_by
                or current.lease_until <= time.time()):
            self._lost.set()
            raise StaleClaimError(f"task lease lost: {self.task.task_id}")

    @contextmanager
    def commit_guard(self):
        guard = getattr(self.queue, "lease_guard", None)
        if not callable(guard):
            raise RuntimeError("this queue backend cannot fence local dataset publication")
        if self._lost.is_set():
            raise StaleClaimError(f"task lease lost: {self.task.task_id}")
        with guard(self.task.task_id, **self.claim_kwargs):
            yield

    def _heartbeat(self):
        while not self._stop.wait(self.lease_seconds / 3):
            try:
                self.queue.renew(
                    self.task.task_id, lease_seconds=self.lease_seconds,
                    **self.claim_kwargs,
                )
            except Exception:
                # An uncertain renewal must not allow new publication.
                self._lost.set()
                return

    def __enter__(self):
        self.check_active()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, min(5.0, self.lease_seconds)))


@contextmanager
def dataset_writer_lock(output_dir: str):
    """One worker writes a local dataset at a time, including recovery.

    The lock file is intentionally retained: unlinking a live lock file could
    allow another process to lock a different inode under the same path.
    """
    import fcntl

    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, ".worker.lock"), "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another worker is writing this dataset") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
