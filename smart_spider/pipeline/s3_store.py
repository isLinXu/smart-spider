# coding=utf-8
"""可选 S3/兼容对象存储插件骨架。

安装::

    pip install -e ".[s3]"

环境变量::

    SMART_SPIDER_S3_BUCKET
    SMART_SPIDER_S3_PREFIX   可选 key 前缀
    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION
    SMART_SPIDER_S3_ENDPOINT 可选（MinIO 等）
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional


def _boto3():
    try:
        import boto3
    except ImportError as exc:
        raise ImportError(
            "S3ObjectStore requires boto3; install with: pip install -e '.[s3]'"
        ) from exc
    return boto3


class S3ObjectStore:
    """S3 兼容 ObjectStore 最小实现。"""

    def __init__(
        self,
        bucket: Optional[str] = None,
        *,
        prefix: str = "",
        endpoint_url: Optional[str] = None,
    ):
        boto3 = _boto3()
        self.bucket = bucket or os.environ.get("SMART_SPIDER_S3_BUCKET")
        if not self.bucket:
            raise ValueError("S3 bucket is required (SMART_SPIDER_S3_BUCKET)")
        self.prefix = (prefix or os.environ.get("SMART_SPIDER_S3_PREFIX") or "").strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or os.environ.get("SMART_SPIDER_S3_ENDPOINT") or None,
        )

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        if self.prefix:
            return f"{self.prefix}/{key}"
        return key

    def put_bytes(self, key: str, data: bytes, *, content_type: str = "") -> str:
        data = bytes(data)
        if not key:
            digest = hashlib.sha256(data).hexdigest()
            key = f"sha256/{digest[:2]}/{digest}"
        extra = {}
        if content_type:
            extra["ContentType"] = content_type
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._key(key),
            Body=data,
            **extra,
        )
        return key

    def get_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=self._key(key))
