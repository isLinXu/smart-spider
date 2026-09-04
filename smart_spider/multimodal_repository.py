# coding=utf-8
"""Crash-recoverable commit protocol for multimodal samples."""
from __future__ import annotations

import os
from typing import Any, Iterable

from .dataset_contracts import SampleRecord
from .dataset_state import DatasetStateStore
from .multimodal_scale import ShardedManifestWriter


class MultimodalRepository:
    """Coordinates staged assets, SQLite metadata and manifest publication.

    The SQLite prepare row is the recovery authority.  JSONL is intentionally
    rebuildable output rather than a second source of truth.
    """

    def __init__(
        self,
        output_dir: str,
        state: DatasetStateStore,
        job_id: str,
        manifest: ShardedManifestWriter,
        asset_store: Any,
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.state = state
        self.job_id = job_id
        self.manifest = manifest
        self.asset_store = asset_store

    def commit(
        self,
        sample: SampleRecord,
        *,
        candidate_ids: Iterable[str],
        staged_assets: Iterable[Any],
    ) -> bool:
        unique_candidates = list(dict.fromkeys(item for item in candidate_ids if item))
        staged = list(staged_assets)
        assets = [self.asset_store.staged_payload(item) for item in staged]
        prepared = self.state.prepare_multimodal_item(
            self.job_id,
            sample,
            candidate_ids=unique_candidates,
            assets=assets,
        )
        status = prepared["status"]
        if status == "committed":
            for item in staged:
                self.asset_store.discard(item)
            self.rebuild_manifest()
            return False

        item_id = int(prepared["item_id"])
        if status == "prepared":
            for item in staged:
                self.asset_store.publish(item)
            self.state.finalize_multimodal_item(item_id)
        else:
            raise RuntimeError(f"unexpected multimodal item state: {status}")
        try:
            self.manifest.write(sample)
        except Exception as exc:
            # State is durable; make publication convergent before surfacing a
            # filesystem error to callers.  Even if the immediate rebuild is
            # unavailable (for example, a transient full disk), the committed
            # SQLite row must remain an accepted sample and will be rebuilt on
            # the next startup.
            try:
                self.rebuild_manifest()
            except Exception as rebuild_exc:
                # Keep the failure observable without converting a durable
                # sample into a retryable/failed candidate.
                import logging
                logging.getLogger(__name__).warning(
                    "manifest rebuild deferred after commit: %s (%s)",
                    exc,
                    rebuild_exc,
                )
            return True
        return True

    def recover(self) -> None:
        """Finish durable prepare records, then reconstruct manifest output."""
        for item in self.state.list_multimodal_items(self.job_id, ("prepared",)):
            assets = item["assets"]
            try:
                for asset in assets:
                    self.asset_store.recover_staged(asset)
                self.state.finalize_multimodal_item(int(item["item_id"]))
            except FileNotFoundError:
                # A crash before the staging fsync leaves no valid asset to
                # publish.  Removing only the prepare row allows its candidate
                # lease to be retried normally.
                self.state.abort_multimodal_item(int(item["item_id"]))
            except OSError:
                # Keep the prepare record for a later startup; this is a real
                # filesystem problem, not a logical rejection.
                continue
        self.rebuild_manifest()

    def rebuild_manifest(self) -> None:
        items = self.state.list_multimodal_items(self.job_id, ("committed",))
        samples = [SampleRecord.from_dict(item["manifest"]) for item in items]
        self.manifest.replace(samples)
