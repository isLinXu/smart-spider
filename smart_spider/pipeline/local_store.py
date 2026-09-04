# coding=utf-8
"""本地文件系统对象存储（ObjectStore 默认实现）。"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading


class LocalFilesystemObjectStore:
    """将对象写入 ``root/``，key 默认可为内容哈希或调用方指定路径。"""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._lock = threading.Lock()
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(os.path.join(self.root, ".staging"), exist_ok=True)

    def _resolve(self, key: str) -> str:
        key = key.lstrip("/")
        if not key or ".." in key.split("/"):
            raise ValueError(f"unsafe object key: {key!r}")
        path = os.path.abspath(os.path.join(self.root, key))
        if not path.startswith(self.root + os.sep) and path != self.root:
            raise ValueError(f"object key escapes root: {key!r}")
        return path

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> str:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        data = bytes(data)
        if not key:
            digest = hashlib.sha256(data).hexdigest()
            key = f"sha256/{digest[:2]}/{digest}"
        path = self._resolve(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staging_dir = os.path.join(self.root, ".staging")
        with self._lock:
            fd, temp_path = tempfile.mkstemp(prefix="obj_", dir=staging_dir)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, path)
            except Exception:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                raise
        return key

    def get_bytes(self, key: str) -> bytes:
        path = self._resolve(key)
        with open(path, "rb") as handle:
            return handle.read()

    def exists(self, key: str) -> bool:
        return os.path.isfile(self._resolve(key))

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
