# coding=utf-8
"""Near-duplicate, source-quota, and leakage-safe split primitives.

The functions here deliberately have no model dependency.  A CLIP or other
embedding producer supplies vectors, while this module persistently assigns
whole related groups to one split and enforces quotas at ingestion time.
"""
from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional, Sequence
from urllib.parse import urlsplit

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class PerceptualFingerprint:
    phash: str
    dhash: str


def _hex_from_bits(bits: np.ndarray) -> str:
    value = 0
    for enabled in bits.reshape(-1):
        value = (value << 1) | int(bool(enabled))
    return f"{value:016x}"


def perceptual_fingerprint(image: Image.Image | str | Path) -> PerceptualFingerprint:
    """Return deterministic 64-bit pHash and dHash values for an image."""
    close_image = not isinstance(image, Image.Image)
    opened = Image.open(image) if close_image else image
    try:
        rgb = opened.convert("RGB")
        gray = rgb.convert("L")
        d_pixels = np.asarray(
            gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16
        )
        dhash = _hex_from_bits(d_pixels[:, 1:] > d_pixels[:, :-1])

        p_pixels = np.asarray(
            gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64
        )
        positions = np.arange(32, dtype=np.float64)
        frequencies = positions[:, None]
        transform = np.cos(np.pi * (2.0 * positions + 1.0) * frequencies / 64.0)
        transform[0, :] /= math.sqrt(2.0)
        coefficients = (transform @ p_pixels @ transform.T) / 16.0
        low_frequency = coefficients[:8, :8]
        median = float(np.median(low_frequency.reshape(-1)[1:]))
        phash = _hex_from_bits(low_frequency > median)
        return PerceptualFingerprint(phash=phash, dhash=dhash)
    finally:
        if close_image:
            opened.close()


def hash_distance(left: str, right: str) -> int:
    """Return Hamming distance between same-width hexadecimal image hashes."""
    if len(left) != len(right) or not left:
        raise ValueError("perceptual hashes must be non-empty and have equal length")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("perceptual hashes must be hexadecimal") from exc


def cluster_embeddings(
    embeddings: Mapping[str, Sequence[float]],
    *,
    similarity_threshold: float = 0.97,
    block_size: int = 256,
) -> dict[str, str]:
    """Cluster precomputed normalized-or-raw embeddings by cosine similarity.

    The all-pairs comparison is intended for a post-crawl curation batch.  It
    is deterministic, and callers can shard by pHash bucket for very large
    collections before using a vector index implementation.
    """
    if not -1.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between -1 and 1")
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    identifiers = sorted(str(key) for key in embeddings)
    if not identifiers:
        return {}
    vectors = []
    expected_size: Optional[int] = None
    for identifier in identifiers:
        vector = np.asarray(embeddings[identifier], dtype=np.float64).reshape(-1)
        if not vector.size or not np.isfinite(vector).all():
            raise ValueError(f"embedding {identifier!r} is empty or non-finite")
        if expected_size is None:
            expected_size = int(vector.size)
        elif vector.size != expected_size:
            raise ValueError("all embeddings must have the same dimension")
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError(f"embedding {identifier!r} has zero norm")
        vectors.append(vector / norm)
    # Float32 and fixed blocks avoid materializing an N x N matrix.  The
    # comparison remains exact for the configured threshold up to float32
    # precision, while peak memory is O(block_size * N).
    matrix = np.vstack(vectors).astype(np.float32, copy=False)
    parent = list(range(len(identifiers)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for start in range(0, len(identifiers), block_size):
        end = min(start + block_size, len(identifiers))
        similarities = matrix[start:end] @ matrix.T
        for local_row in range(end - start):
            row = start + local_row
            columns = np.flatnonzero(similarities[local_row, :row] >= similarity_threshold)
            for column in columns.tolist():
                union(row, int(column))
    members: dict[int, list[str]] = {}
    for index, identifier in enumerate(identifiers):
        members.setdefault(find(index), []).append(identifier)
    cluster_ids = {
        root: "cluster:" + hashlib.sha256(
            "\0".join(sorted(values)).encode("utf-8")
        ).hexdigest()[:16]
        for root, values in members.items()
    }
    return {identifier: cluster_ids[find(index)] for index, identifier in enumerate(identifiers)}


@dataclass(frozen=True)
class SplitItem:
    item_id: str
    origin_url: str = ""
    page_url: str = ""
    captured_at: str = ""
    cluster_id: str = ""


@dataclass(frozen=True)
class SplitPlan:
    assignments: dict[str, str]
    group_count: int


class LeakageSafeSplitter:
    """Deterministically assign connected provenance groups to one data split."""

    def __init__(
        self,
        *,
        train: float = 0.8,
        validation: float = 0.1,
        test: float = 0.1,
        seed: str = "smart-spider-v1",
    ) -> None:
        ratios = {"train": train, "validation": validation, "test": test}
        if not all(0.0 < value < 1.0 for value in ratios.values()):
            raise ValueError("split ratios must be between 0 and 1")
        if not math.isclose(sum(ratios.values()), 1.0, abs_tol=1e-9):
            raise ValueError("split ratios must sum to 1")
        self.ratios = ratios
        self.seed = str(seed)

    @staticmethod
    def _origin_domain(item: SplitItem) -> str:
        return (urlsplit(item.origin_url).hostname or "").casefold()

    @staticmethod
    def _canonical_page(item: SplitItem) -> str:
        parts = urlsplit(item.page_url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme.casefold()}://{parts.netloc.casefold()}{parts.path}"

    @staticmethod
    def _time_bucket(item: SplitItem) -> str:
        raw = item.captured_at.strip()
        if not raw:
            return ""
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return raw[:10]

    def plan(self, items: Sequence[SplitItem]) -> SplitPlan:
        identifiers = [str(item.item_id) for item in items]
        if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
            raise ValueError("split item IDs must be non-empty and unique")
        parent = list(range(len(items)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        first_by_key: dict[tuple[str, str], int] = {}
        for index, item in enumerate(items):
            keys = (
                ("origin_domain", self._origin_domain(item)),
                ("page", self._canonical_page(item)),
                ("time_bucket", self._time_bucket(item)),
                ("cluster", item.cluster_id.strip()),
            )
            for kind, value in keys:
                if not value:
                    continue
                key = (kind, value)
                previous = first_by_key.setdefault(key, index)
                union(previous, index)
        groups: dict[int, list[str]] = {}
        for index, identifier in enumerate(identifiers):
            groups.setdefault(find(index), []).append(identifier)
        assignments: dict[str, str] = {}
        train_cutoff = self.ratios["train"]
        validation_cutoff = train_cutoff + self.ratios["validation"]
        for group in groups.values():
            group_key = "\0".join(sorted(group))
            fraction = int.from_bytes(
                hashlib.sha256(f"{self.seed}\0{group_key}".encode("utf-8")).digest()[:8],
                "big",
            ) / 2**64
            split = "train" if fraction < train_cutoff else (
                "validation" if fraction < validation_cutoff else "test"
            )
            assignments.update({identifier: split for identifier in group})
        return SplitPlan(assignments=assignments, group_count=len(groups))


class QuotaLedger:
    """Thread-safe reservation ledger for max-share source or domain quotas."""

    def __init__(
        self,
        target_total: int,
        *,
        max_share: float = 1.0,
        limits: Optional[Mapping[str, int]] = None,
        initial_counts: Optional[Mapping[str, int]] = None,
    ) -> None:
        if target_total <= 0:
            raise ValueError("target_total must be positive")
        if not 0.0 < max_share <= 1.0:
            raise ValueError("max_share must be in (0, 1]")
        self.target_total = int(target_total)
        self.max_share = float(max_share)
        self.limits = {str(key): int(value) for key, value in (limits or {}).items()}
        if any(not key or value < 0 for key, value in self.limits.items()):
            raise ValueError("quota limits need non-empty keys and non-negative values")
        self._accepted = {str(key).strip(): int(value) for key, value in (initial_counts or {}).items()}
        if any(not key or value < 0 for key, value in self._accepted.items()):
            raise ValueError("initial quota counts need non-empty keys and non-negative values")
        if any(value > self.limit_for(key) for key, value in self._accepted.items()):
            raise ValueError("initial quota count exceeds its limit")
        self._pending: dict[str, int] = {}
        self._lock = threading.Lock()

    def limit_for(self, key: str) -> int:
        normalized = str(key).strip()
        if not normalized:
            return 0
        return self.limits.get(
            normalized, max(1, math.floor(self.target_total * self.max_share))
        )

    def try_reserve(self, key: str) -> bool:
        normalized = str(key).strip()
        with self._lock:
            if not normalized or self._accepted.get(normalized, 0) + self._pending.get(normalized, 0) >= self.limit_for(normalized):
                return False
            self._pending[normalized] = self._pending.get(normalized, 0) + 1
            return True

    def commit(self, key: str) -> None:
        normalized = str(key).strip()
        with self._lock:
            if self._pending.get(normalized, 0) <= 0:
                raise ValueError(f"no pending quota reservation for {normalized!r}")
            self._pending[normalized] -= 1
            self._accepted[normalized] = self._accepted.get(normalized, 0) + 1

    def release(self, key: str) -> None:
        normalized = str(key).strip()
        with self._lock:
            if self._pending.get(normalized, 0) <= 0:
                raise ValueError(f"no pending quota reservation for {normalized!r}")
            self._pending[normalized] -= 1

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            keys = sorted(set(self._accepted) | set(self._pending) | set(self.limits))
            return {
                key: {
                    "accepted": self._accepted.get(key, 0),
                    "pending": self._pending.get(key, 0),
                    "limit": self.limit_for(key),
                }
                for key in keys
            }


class SceneQuotaLedger:
    """Thread-safe exact targets for independently maintained scene datasets."""

    def __init__(
        self, targets: Mapping[str, int], *, initial_counts: Optional[Mapping[str, int]] = None
    ) -> None:
        self.targets = {str(key).strip(): int(value) for key, value in targets.items()}
        if any(not key or value <= 0 for key, value in self.targets.items()):
            raise ValueError("scene targets need non-empty names and positive counts")
        initial = {str(key).strip(): int(value) for key, value in (initial_counts or {}).items()}
        if any(value < 0 for value in initial.values()):
            raise ValueError("initial scene counts cannot be negative")
        if any(initial.get(key, 0) > target for key, target in self.targets.items()):
            raise ValueError("initial scene count exceeds its target")
        self._accepted = {key: initial.get(key, 0) for key in self.targets}
        self._pending = {key: 0 for key in self.targets}
        self._lock = threading.Lock()

    def try_reserve(self, scene: str) -> bool:
        with self._lock:
            if scene not in self.targets:
                return True
            if self._accepted[scene] + self._pending[scene] >= self.targets[scene]:
                return False
            self._pending[scene] += 1
            return True

    def commit(self, scene: str) -> None:
        with self._lock:
            if scene not in self.targets:
                return
            if self._pending[scene] <= 0:
                raise ValueError(f"no pending scene reservation for {scene!r}")
            self._pending[scene] -= 1
            self._accepted[scene] += 1

    def release(self, scene: str) -> None:
        with self._lock:
            if scene not in self.targets:
                return
            if self._pending[scene] <= 0:
                raise ValueError(f"no pending scene reservation for {scene!r}")
            self._pending[scene] -= 1

    def reached(self, scene: str) -> bool:
        with self._lock:
            return scene in self.targets and self._accepted[scene] >= self.targets[scene]

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                scene: {
                    "accepted": self._accepted[scene],
                    "pending": self._pending[scene],
                    "target": self.targets[scene],
                }
                for scene in sorted(self.targets)
            }
