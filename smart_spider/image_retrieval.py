# coding=utf-8
"""本地图片以图搜图能力。

该模块把项目已有的 CLIP 图像编码能力封装成一个可持久化的图片索引，
用于在已采集数据集或任意本地图片目录中按视觉相似度检索图片。

索引使用归一化 CLIP 向量和 NumPy 点积计算余弦相似度，不依赖额外的
向量数据库；未来接入远程反向图片搜索服务时，可以在本模块之外实现
同样的 ``search`` 接口，而不改变现有数据契约。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence, Union

import numpy as np
from PIL import Image, UnidentifiedImageError


ImageInput = Union[str, os.PathLike[str], Image.Image]
PathInput = Union[str, os.PathLike[str]]

IMAGE_SUFFIXES = frozenset({
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
})


class ImageEncoder(Protocol):
    """图片编码器最小协议，便于测试和替换模型。"""

    def encode_images(self, images: Sequence[Image.Image]) -> Any:
        ...

    def encode_image(self, image: Image.Image) -> Any:
        ...


@dataclass(frozen=True)
class ImageSearchResult:
    """单条以图搜图结果。"""

    path: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, rank: int = 0) -> dict[str, Any]:
        result = {
            "path": self.path,
            "score": round(float(self.score), 6),
            "metadata": dict(self.metadata),
        }
        if rank > 0:
            result["rank"] = rank
        return result


@dataclass(frozen=True)
class IndexBuildReport:
    """建立索引的统计信息。"""

    indexed: int
    skipped: int
    skipped_files: tuple[str, ...] = ()
    embedding_dim: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "skipped_files": list(self.skipped_files),
            "embedding_dim": self.embedding_dim,
        }


def iter_image_files(directory: Union[str, os.PathLike[str]], recursive: bool = True) -> list[str]:
    """按稳定顺序枚举目录中的图片文件。"""
    root = Path(directory)
    if not root.exists():
        raise FileNotFoundError(f"image directory not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"image directory expected: {root}")

    iterator = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        str(path.resolve())
        for path in iterator
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


class ImageSimilarityIndex:
    """基于 CLIP 向量的本地图片相似度索引。"""

    FORMAT_VERSION = 1

    def __init__(
        self,
        encoder: Optional[ImageEncoder] = None,
        *,
        model_name: str = "ViT-B/32",
        device: str = "cpu",
        batch_size: int = 32,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.encoder = encoder
        self.model_name = model_name
        self.device = device
        self.batch_size = batch_size
        self.paths: list[str] = []
        self.metadata: list[dict[str, Any]] = []
        self.embeddings = np.empty((0, 0), dtype=np.float32)

    def _ensure_encoder(self) -> ImageEncoder:
        if self.encoder is None:
            from .perception.clip_inference import CLIPInference

            self.encoder = CLIPInference(model_name=self.model_name, device=self.device)
        return self.encoder

    @staticmethod
    def _open_image(image: ImageInput) -> tuple[Image.Image, str]:
        if isinstance(image, Image.Image):
            return image.convert("RGB"), ""
        path = str(Path(image).expanduser().resolve())
        try:
            with Image.open(path) as opened:
                return opened.convert("RGB"), path
        except (FileNotFoundError, UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"cannot open image '{path}': {exc}") from exc

    @staticmethod
    def _normalize_embeddings(raw: Any) -> np.ndarray:
        if hasattr(raw, "detach"):
            raw = raw.detach().cpu().numpy()
        array = np.asarray(raw, dtype=np.float32)
        if array.ndim == 1:
            array = array.reshape(1, -1)
        if array.ndim != 2 or array.shape[1] == 0:
            raise ValueError("image encoder must return a 2D non-empty embedding matrix")
        if not np.isfinite(array).all():
            raise ValueError("image encoder returned non-finite embeddings")
        norms = np.linalg.norm(array, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise ValueError("image encoder returned a zero-length embedding")
        return (array / norms).astype(np.float32, copy=False)

    def _encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        if not images:
            return np.empty((0, 0), dtype=np.float32)
        encoder = self._ensure_encoder()
        batch_method = getattr(encoder, "encode_images", None)
        if callable(batch_method):
            return self._normalize_embeddings(batch_method(images))
        single_method = getattr(encoder, "encode_image", None)
        if not callable(single_method):
            raise TypeError("encoder must provide encode_images or encode_image")
        return self._normalize_embeddings([single_method(image) for image in images])

    @staticmethod
    def _metadata_for_image(path: str, image: Image.Image) -> dict[str, Any]:
        stat = Path(path).stat()
        return {
            "width": int(image.width),
            "height": int(image.height),
            "format": str(image.format or Path(path).suffix.lstrip(".")).lower(),
            "file_size": int(stat.st_size),
        }

    def build(self, image_paths: Iterable[PathInput], clear: bool = True) -> IndexBuildReport:
        """从图片路径建立或追加索引。"""
        if clear:
            self.paths = []
            self.metadata = []
            self.embeddings = np.empty((0, 0), dtype=np.float32)

        pending_images: list[Image.Image] = []
        pending_paths: list[str] = []
        pending_metadata: list[dict[str, Any]] = []
        embeddings: list[np.ndarray] = []
        paths: list[str] = []
        metadata: list[dict[str, Any]] = []
        skipped_files: list[str] = []

        def flush() -> None:
            if not pending_images:
                return
            encoded = self._encode_images(pending_images)
            if len(encoded) != len(pending_images):
                raise ValueError("encoder returned a row count different from input images")
            embeddings.append(encoded)
            paths.extend(pending_paths)
            metadata.extend(pending_metadata)
            pending_images.clear()
            pending_paths.clear()
            pending_metadata.clear()

        for image_input in image_paths:
            path = str(Path(image_input).expanduser().resolve())
            try:
                image, resolved_path = self._open_image(path)
                path = resolved_path or path
                pending_images.append(image)
                pending_paths.append(path)
                pending_metadata.append(self._metadata_for_image(path, image))
            except (ValueError, OSError) as exc:
                skipped_files.append(f"{path}: {exc}")
                continue
            if len(pending_images) >= self.batch_size:
                flush()
        flush()

        if embeddings:
            new_embeddings = np.concatenate(embeddings, axis=0)
            if self.embeddings.size and self.embeddings.shape[1] != new_embeddings.shape[1]:
                raise ValueError("embedding dimension differs from existing index")
            self.embeddings = (
                new_embeddings
                if not self.embeddings.size
                else np.concatenate([self.embeddings, new_embeddings], axis=0)
            )
            self.paths.extend(paths)
            self.metadata.extend(metadata)

        return IndexBuildReport(
            indexed=len(paths),
            skipped=len(skipped_files),
            skipped_files=tuple(skipped_files),
            embedding_dim=(
                int(self.embeddings.shape[1])
                if self.embeddings.ndim == 2 and self.embeddings.size
                else 0
            ),
        )

    def build_from_directory(self, directory: PathInput, recursive: bool = True) -> IndexBuildReport:
        """索引目录中的全部支持格式图片。"""
        return self.build(iter_image_files(directory, recursive=recursive))

    def search(
        self,
        query_image: ImageInput,
        *,
        top_k: int = 10,
        threshold: Optional[float] = None,
        exclude_query: bool = True,
    ) -> list[ImageSearchResult]:
        """返回与查询图片最相似的图片，结果按分数降序排列。"""
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if threshold is not None and not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between -1 and 1")
        if not self.paths:
            return []

        image, query_path = self._open_image(query_image)
        query_embedding = self._encode_images([image])[0]
        scores = self.embeddings @ query_embedding
        order = np.argsort(-scores, kind="stable")
        normalized_query = os.path.normcase(os.path.abspath(query_path)) if query_path else ""
        results: list[ImageSearchResult] = []
        for index in order:
            score = float(scores[index])
            path = self.paths[int(index)]
            if exclude_query and normalized_query:
                candidate_path = os.path.normcase(os.path.abspath(path))
                if candidate_path == normalized_query:
                    continue
            if threshold is not None and score < threshold:
                continue
            results.append(ImageSearchResult(
                path=path,
                score=score,
                metadata=self.metadata[int(index)] if int(index) < len(self.metadata) else {},
            ))
            if len(results) >= top_k:
                break
        return results

    def save(self, index_path: PathInput) -> str:
        """原子保存索引，返回实际路径。"""
        target = Path(index_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format_version": self.FORMAT_VERSION,
            "model_name": self.model_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "paths": self.paths,
            "metadata": self.metadata,
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(target.parent),
                prefix=f".{target.name}.",
                suffix=".npz.tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                np.savez_compressed(
                    handle,
                    embeddings=self.embeddings.astype(np.float32, copy=False),
                    metadata_json=np.asarray(json.dumps(payload, ensure_ascii=False)),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except Exception:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            raise
        return str(target)

    @classmethod
    def load(
        cls,
        index_path: PathInput,
        encoder: Optional[ImageEncoder] = None,
        *,
        device: str = "cpu",
        batch_size: int = 32,
    ) -> "ImageSimilarityIndex":
        """加载索引；查询时仍需要一个可编码查询图片的 encoder。"""
        path = Path(index_path).expanduser().resolve()
        try:
            with np.load(path, allow_pickle=False) as archive:
                embeddings = np.asarray(archive["embeddings"], dtype=np.float32)
                raw_metadata = archive["metadata_json"].item()
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid image index '{path}': {exc}") from exc

        try:
            payload = json.loads(str(raw_metadata))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid image index metadata '{path}': {exc}") from exc
        if payload.get("format_version") != cls.FORMAT_VERSION:
            raise ValueError(
                f"unsupported image index format: {payload.get('format_version')}"
            )
        paths = [str(item) for item in payload.get("paths", [])]
        metadata = [dict(item or {}) for item in payload.get("metadata", [])]
        if embeddings.ndim != 2 or (embeddings.shape[0] and embeddings.shape[1] == 0):
            raise ValueError("image index embeddings must be a 2D matrix")
        if embeddings.size:
            embeddings = cls._normalize_embeddings(embeddings)
        if len(paths) != len(embeddings):
            raise ValueError("image index paths and embeddings have different lengths")

        index = cls(
            encoder=encoder,
            model_name=str(payload.get("model_name") or "ViT-B/32"),
            device=device,
            batch_size=batch_size,
        )
        index.paths = paths
        index.metadata = metadata
        index.embeddings = embeddings
        return index
