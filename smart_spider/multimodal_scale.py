# coding=utf-8
"""大规模多模态数据集输出与质量统计工具。

默认仍然写入单个 ``manifest.jsonl``；当配置分片大小后，写入
``manifest-00000.jsonl``、``manifest-00001.jsonl`` 等文件。这样可以在不
强制引入重型数据框架的情况下，支持数十万条样本的增量生产和并行消费。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from glob import glob
from typing import Any

from .dataset_contracts import SampleRecord, utc_now


class ShardedManifestWriter:
    """线程安全的 JSONL manifest 写入器，支持可选记录数分片。"""

    def __init__(self, output_dir: str, shard_size: int = 0, prefix: str = "manifest"):
        if shard_size < 0:
            raise ValueError("shard_size must be non-negative")
        os.makedirs(output_dir, exist_ok=True)
        self.output_dir = output_dir
        self.shard_size = int(shard_size)
        self.prefix = prefix
        self._lock = threading.Lock()
        self._file = None
        self._records_in_shard = 0
        self._paths: list[str] = []
        self._next_index = 0

        if self.shard_size <= 0:
            self.path = os.path.join(output_dir, f"{prefix}.jsonl")
            self._paths = [self.path]
        else:
            numbered = []
            for path in glob(os.path.join(output_dir, f"{prefix}-*.jsonl")):
                try:
                    index = int(os.path.basename(path).rsplit("-", 1)[1].split(".")[0])
                except (ValueError, IndexError):
                    continue
                numbered.append((index, path))
            numbered.sort(key=lambda item: item[0])
            existing = [path for _, path in numbered]
            self._paths = existing
            if existing:
                last = existing[-1]
                try:
                    self._next_index = int(os.path.basename(last).split("-")[-1].split(".")[0])
                except (ValueError, IndexError):
                    self._next_index = len(existing) - 1
                self.path = last
                self._records_in_shard = self._count_records(last)
                if self._records_in_shard >= self.shard_size:
                    self._next_index += 1
                    self.path = self._shard_path(self._next_index)
                    self._records_in_shard = 0
            else:
                self.path = self._shard_path(0)
            if self.path not in self._paths:
                self._paths.append(self.path)

    @staticmethod
    def _count_records(path: str) -> int:
        try:
            with open(path, encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def _shard_path(self, index: int) -> str:
        return os.path.join(self.output_dir, f"{self.prefix}-{index:05d}.jsonl")

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(self._paths)

    def _ensure_open(self):
        if self._file is None or self._file.closed:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._file = open(self.path, "a", encoding="utf-8", buffering=1)

    def _rotate_if_needed(self):
        if self.shard_size <= 0 or self._records_in_shard < self.shard_size:
            return
        if self._file is not None and not self._file.closed:
            self._file.flush()
            self._file.close()
        self._next_index += 1
        self.path = self._shard_path(self._next_index)
        self._records_in_shard = 0
        if self.path not in self._paths:
            self._paths.append(self.path)

    def write(self, sample: SampleRecord):
        with self._lock:
            self._rotate_if_needed()
            self._ensure_open()
            self._file.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
            self._file.flush()
            os.fsync(self._file.fileno())
            self._records_in_shard += 1

    def replace(self, samples: list[SampleRecord]):
        """Atomically rebuild all manifest shards from committed state records."""
        encoded = [json.dumps(sample.to_dict(), ensure_ascii=False) + "\n" for sample in samples]
        if self.shard_size > 0:
            chunks = [
                encoded[index:index + self.shard_size]
                for index in range(0, len(encoded), self.shard_size)
            ] or [[]]
            paths = [self._shard_path(index) for index in range(len(chunks))]
        else:
            chunks = [encoded]
            paths = [os.path.join(self.output_dir, f"{self.prefix}.jsonl")]

        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            for path, lines in zip(paths, chunks):
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="utf-8", dir=self.output_dir,
                        prefix=f".{self.prefix}-", suffix=".tmp", delete=False,
                    ) as handle:
                        temporary = handle.name
                        handle.writelines(lines)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    if temporary and os.path.exists(temporary):
                        os.unlink(temporary)
            expected = set(paths)
            pattern = (
                os.path.join(self.output_dir, f"{self.prefix}-*.jsonl")
                if self.shard_size > 0
                else os.path.join(self.output_dir, f"{self.prefix}.jsonl")
            )
            for obsolete in glob(pattern):
                if obsolete not in expected:
                    os.unlink(obsolete)
            self._file = None
            self._paths = paths
            self.path = paths[-1]
            self._next_index = len(paths) - 1 if self.shard_size > 0 else 0
            self._records_in_shard = len(chunks[-1])

    def close(self):
        with self._lock:
            if self._file is not None and not self._file.closed:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()


@dataclass
class QualityReport:
    """样本级质量与生产统计，可直接序列化为审计报告。"""

    generated_at: str = field(default_factory=utc_now)
    accepted: int = 0
    rejected: int = 0
    materialized_images: int = 0
    modality_counts: Counter = field(default_factory=Counter)
    task_type_counts: Counter = field(default_factory=Counter)
    label_counts: Counter = field(default_factory=Counter)
    route_counts: Counter = field(default_factory=Counter)
    route_reasons: Counter = field(default_factory=Counter)
    block_kinds: Counter = field(default_factory=Counter)
    scene_decisions: Counter = field(default_factory=Counter)
    rejection_reasons: Counter = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)

    def observe_route(self, route: str, reason: str = ""):
        if route:
            self.route_counts[str(route)] += 1
        if reason:
            self.route_reasons[str(reason)] += 1

    def observe_block(self, kind: str):
        if kind:
            self.block_kinds[str(kind)] += 1

    def observe_materialized_image(self, count: int = 1):
        self.materialized_images += max(0, int(count))

    def observe_scene_decision(self, action: str):
        """Record detector-backed scene gate outcomes without retaining images."""
        if action:
            self.scene_decisions[str(action)] += 1

    def observe_sample(self, sample: SampleRecord):
        self.accepted += 1
        self.task_type_counts[sample.task_type or "unknown"] += 1
        for asset in sample.modalities:
            modality = getattr(asset.modality, "value", str(asset.modality))
            self.modality_counts[modality] += 1
        for label in sample.labels:
            if label.name:
                self.label_counts[label.name] += 1

    def observe_rejection(self, reason: str):
        self.rejected += 1
        self.rejection_reasons[reason or "unknown"] += 1

    def observe_error(self, error: str):
        if error and error not in self.errors:
            self.errors.append(error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "materialized_images": self.materialized_images,
            "modality_counts": dict(sorted(self.modality_counts.items())),
            "task_type_counts": dict(sorted(self.task_type_counts.items())),
            "label_counts": dict(sorted(self.label_counts.items())),
            "route_counts": dict(sorted(self.route_counts.items())),
            "route_reasons": dict(sorted(self.route_reasons.items())),
            "block_kinds": dict(sorted(self.block_kinds.items())),
            "scene_decisions": dict(sorted(self.scene_decisions.items())),
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "errors": list(self.errors),
        }

    def write(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
