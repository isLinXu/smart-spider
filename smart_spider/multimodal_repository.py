# coding=utf-8
"""Crash-recoverable commit protocol for multimodal samples."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Iterable

from .dataset_contracts import SampleRecord, normalize_url
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
        review_queue_path: str | None = None,
    ):
        self.output_dir = os.path.abspath(output_dir)
        self.state = state
        self.job_id = job_id
        self.manifest = manifest
        self.asset_store = asset_store
        self.review_queue_path = review_queue_path or os.path.join(output_dir, "scene_review_queue.jsonl")
        self.accepted_count = 0
        self._review_sample_ids: set[str] = set()
        self._review_candidate_ids: set[str] = set()

    def has_review(self, sample_id: str) -> bool:
        return sample_id in self._review_sample_ids

    def has_review_for_url(self, url: str) -> bool:
        if not url:
            return False
        resource_id = hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()
        candidate_id = hashlib.sha256(f"{self.job_id}\0{resource_id}".encode("utf-8")).hexdigest()
        return candidate_id in self._review_candidate_ids

    def retain_review(
        self, sample: SampleRecord, *, candidate_ids: Iterable[str],
        staged_assets: Iterable[Any], decision: dict[str, Any],
    ) -> bool:
        """Commit review assets separately from the accepted manifest."""
        staged = list(staged_assets)
        candidate_ids = list(dict.fromkeys(value for value in candidate_ids if value))
        payloads = [self.asset_store.staged_payload(item) for item in staged]
        for asset in payloads:
            self._review_asset_path(asset, "relative_path")
            self._review_asset_path(asset, "staging_path")
        prepared = self.state.prepare_multimodal_review(
            self.job_id, sample, candidate_ids=candidate_ids, assets=payloads,
            decision=decision,
        )
        self._review_sample_ids.add(sample.sample_id)
        self._review_candidate_ids.update(candidate_ids)
        if prepared["status"] == "needs_review":
            for item in staged:
                self.asset_store.discard(item)
            self.rebuild_review_queue()
            return False
        for item in staged:
            self.asset_store.publish(item)
        for asset in payloads:
            self._verify_review_asset(asset)
        self.state.finalize_multimodal_review(int(prepared["review_id"]))
        try:
            # Append only this materialized view row. Full reconstruction is
            # reserved for startup, so reviewing N images does not re-hash all
            # prior images after every item.
            record = self._review_record({
                "review_id": prepared["review_id"], "sample_id": sample.sample_id,
                "created_at": prepared["created_at"], "decision": decision,
                "manifest": sample.to_dict(), "assets": payloads,
            })
            os.makedirs(os.path.dirname(os.path.abspath(self.review_queue_path)), exist_ok=True)
            with open(self.review_queue_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            # The durable SQLite row and assets remain the recovery authority.
            import logging
            logging.getLogger(__name__).exception("review queue publication deferred")
        return True

    def _review_record(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "review_id": item["review_id"], "sample_ref": item["sample_id"],
            "created_at": item["created_at"], "status": "needs_review",
            "decision": item["decision"], "sample": item["manifest"],
            "asset_root": self.output_dir,
            "assets": [{key: value for key, value in asset.items() if key != "staging_path"}
                       for asset in item["assets"]],
        }

    def _review_asset_path(self, asset: dict[str, Any], key: str) -> str:
        relative = str(asset[key])
        path = os.path.realpath(os.path.join(self.output_dir, relative))
        root = os.path.realpath(self.output_dir)
        if os.path.isabs(relative) or os.path.commonpath((root, path)) != root:
            raise ValueError("review asset path escapes the output directory")
        return path

    def _verify_review_asset(self, asset: dict[str, Any]) -> None:
        path = self._review_asset_path(asset, "relative_path")
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != asset["digest"]:
            raise ValueError("review asset hash mismatch")

    def rebuild_review_queue(self) -> None:
        reviews = self.state.list_multimodal_reviews(self.job_id, ("prepared", "needs_review"))
        self._review_sample_ids = {item["sample_id"] for item in reviews}
        self._review_candidate_ids = {key for item in reviews for key in item["candidate_ids"]}
        rows = []
        for item in reviews:
            if item["state"] != "needs_review":
                continue
            for asset in item["assets"]:
                self._verify_review_asset(asset)
            rows.append(self._review_record(item))
        if not rows and not os.path.exists(self.review_queue_path):
            return
        parent = os.path.dirname(os.path.abspath(self.review_queue_path))
        os.makedirs(parent, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=parent, delete=False) as handle:
                temporary = handle.name
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.review_queue_path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

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
            self.accepted_count += 1
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
        for item in self.state.list_multimodal_reviews(self.job_id, ("prepared",)):
            try:
                for asset in item["assets"]:
                    # Validate both stored paths before recovery can move bytes.
                    for key in ("relative_path", "staging_path"):
                        self._review_asset_path(asset, key)
                    self.asset_store.recover_staged(asset)
                    self._verify_review_asset(asset)
                self.state.finalize_multimodal_review(int(item["review_id"]))
            except FileNotFoundError:
                self.state.abort_multimodal_review(int(item["review_id"]))
            except OSError:
                continue
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
        self.rebuild_review_queue()

    def rebuild_manifest(self) -> None:
        items = self.state.list_multimodal_items(self.job_id, ("committed",))
        samples = [SampleRecord.from_dict(item["manifest"]) for item in items]
        self.manifest.replace(samples)
        self.accepted_count = len(items)
