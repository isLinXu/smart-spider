# coding=utf-8
"""数据集分桶目录、进度与 JSONL 写入辅助（从 dataset_crawler 拆出）。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from typing import Optional

from loguru import logger

from .dataset_contracts import SampleRecord

_BATCH_SIZE = 100


class DatasetDirManager:
    """管理数据集分桶目录，每 100 张图片一个子目录。

    目录命名规则：batch_0000/, batch_0100/, batch_0200/, ...
    文件命名规则：{全局序号:04d}_{url_hash[:8]}{ext}

    线程安全：所有公共方法通过内部锁保护。
    """

    def __init__(self, output_dir: str, batch_size: int = _BATCH_SIZE):
        self.output_dir = output_dir
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._saved_count = 0
        self._next_index = 0
        os.makedirs(output_dir, exist_ok=True)

    def get_save_path(self, url: str, ext: str) -> str:
        """获取图片保存路径，自动分配到正确的分桶目录。

        Returns:
            完整文件路径，如 /output/batch_0100/0123_a1b2c3d4.jpg
        """
        with self._lock:
            idx = self._next_index
            self._saved_count += 1
            self._next_index += 1

        batch_dir = os.path.join(
            self.output_dir,
            f"batch_{(idx // self.batch_size) * self.batch_size:04d}",
        )
        os.makedirs(batch_dir, exist_ok=True)

        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        filename = f"{idx:04d}_{url_hash}{ext}"
        return os.path.join(batch_dir, filename)

    def save_content(
        self,
        url: str,
        ext: str,
        content: bytes,
        max_count: Optional[int] = None,
    ) -> Optional[tuple[int, str]]:
        """原子保存内容并返回 ``(index, path)``。"""
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("content must be bytes-like")

        with self._lock:
            if max_count is not None and self._saved_count >= max_count:
                return None

            idx = self._next_index
            batch_dir = os.path.join(
                self.output_dir,
                f"batch_{(idx // self.batch_size) * self.batch_size:04d}",
            )
            os.makedirs(batch_dir, exist_ok=True)
            url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
            final_path = os.path.join(batch_dir, f"{idx:04d}_{url_hash}{ext}")
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=batch_dir,
                    prefix=f".{idx:04d}_",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_path = temp_file.name
                    temp_file.write(bytes(content))
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, final_path)
            except Exception:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                raise

            self._saved_count += 1
            self._next_index = idx + 1
            return idx, final_path

    def increment(self) -> int:
        """递增计数器并返回当前值（线程安全）。"""
        with self._lock:
            idx = self._next_index
            self._saved_count += 1
            self._next_index += 1
            return idx

    def set_existing_state(self, saved_count: int, next_index: Optional[int] = None) -> None:
        """Restore active count without reusing indices removed by filtering."""
        if saved_count < 0:
            raise ValueError("saved_count must be non-negative")
        if next_index is None:
            next_index = saved_count
        if next_index < saved_count:
            raise ValueError("next_index must be >= saved_count")
        with self._lock:
            self._saved_count = saved_count
            self._next_index = next_index

    def record_repository_commit(self, index: int) -> None:
        """Mirror a commit allocated by DatasetRepository."""
        with self._lock:
            self._saved_count += 1
            self._next_index = max(self._next_index, index + 1)

    @property
    def saved_count(self) -> int:
        with self._lock:
            return self._saved_count

    def current_batch_dir(self) -> str:
        """返回当前分桶目录路径。"""
        with self._lock:
            idx = self._next_index
        batch_name = f"batch_{(idx // self.batch_size) * self.batch_size:04d}"
        return os.path.join(self.output_dir, batch_name)

    def list_batches(self) -> list[str]:
        """列出所有已创建的分桶目录。"""
        batches = []
        for name in sorted(os.listdir(self.output_dir)):
            if name.startswith("batch_") and os.path.isdir(
                os.path.join(self.output_dir, name)
            ):
                batches.append(name)
        return batches

    def count_existing_images(self) -> int:
        """统计已保存的图片数量（扫描磁盘）。"""
        count = 0
        for name in os.listdir(self.output_dir):
            batch_dir = os.path.join(self.output_dir, name)
            if not os.path.isdir(batch_dir) or not name.startswith("batch_"):
                continue
            for fname in os.listdir(batch_dir):
                if fname.lower().endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
                ):
                    count += 1
        return count

    def infer_next_index(self) -> int:
        """Return one past the largest numeric filename prefix on disk."""
        maximum = -1
        for name in os.listdir(self.output_dir):
            batch_dir = os.path.join(self.output_dir, name)
            if not os.path.isdir(batch_dir) or not name.startswith("batch_"):
                continue
            for filename in os.listdir(batch_dir):
                prefix = filename.split("_", 1)[0]
                if prefix.isdigit():
                    maximum = max(maximum, int(prefix))
        return maximum + 1


class ProgressManager:
    """管理数据集爬取的断点续传状态。"""

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, ".dataset_progress.json")
        self._lock = threading.Lock()
        self._data: dict = {}

    def load(self) -> dict:
        """加载进度文件。"""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                logger.info(
                    f"ProgressManager: loaded from {self._path}, "
                    f"saved_count={self._data.get('saved_count', 0)}"
                )
            except Exception as e:
                logger.warning(f"ProgressManager: failed to load {self._path}: {e}")
                self._data = {}
        return self._data

    def save(
        self,
        saved_count: int,
        total_target: int,
        keywords_done: list[str],
        keywords_remaining: list[str],
    ):
        """保存进度。"""
        from datetime import datetime

        with self._lock:
            self._data = {
                "saved_count": saved_count,
                "total_target": total_target,
                "keywords_done": keywords_done,
                "keywords_remaining": keywords_remaining,
                "last_update": datetime.now().isoformat(),
            }
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"ProgressManager: failed to save: {e}")

    @property
    def saved_count(self) -> int:
        return self._data.get("saved_count", 0)

    @property
    def keywords_done(self) -> list[str]:
        return self._data.get("keywords_done", [])


class MetadataWriter:
    """线程安全的元数据追加写入器。"""

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, "metadata.jsonl")
        self._lock = threading.Lock()
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, record: dict):
        """写入一条元数据记录。"""
        with self._lock:
            try:
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"MetadataWriter: write error: {e}")

    def close(self):
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

    def __del__(self):
        self.close()


class ManifestWriter:
    """通用多模态 manifest 写入器。"""

    def __init__(self, output_dir: str):
        self._path = os.path.join(output_dir, "manifest.jsonl")
        self._lock = threading.Lock()
        self._file = open(self._path, "a", encoding="utf-8", buffering=1)

    def write(self, sample: SampleRecord):
        with self._lock:
            try:
                self._file.write(json.dumps(sample.to_dict(), ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning(f"ManifestWriter: write error: {e}")

    def close(self):
        with self._lock:
            try:
                self._file.close()
            except Exception:
                pass

    def __del__(self):
        self.close()
