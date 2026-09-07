# coding=utf-8
"""CLIP prompt scorers for offline dataset filtering."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from .dataset_filter_types import PromptScorer, PromptSet, SemanticScores


class CLIPPromptScorer:
    """Batch CLIP scorer using max prompt similarity per semantic group."""

    def __init__(
        self,
        prompts: PromptSet,
        *,
        model_name: str = "ViT-B/32",
        device: str = "auto",
        cache_dir: Optional[str | os.PathLike[str]] = None,
    ) -> None:
        import torch

        from .perception.clip_inference import CLIPInference

        if device == "auto":
            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.device = device
        self.model_name = model_name
        self.prompts = prompts
        self.encoder = CLIPInference(model_name=model_name, device=device)
        self.cache_dir = None
        if cache_dir:
            model_key = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:16]
            self.cache_dir = Path(cache_dir).expanduser().resolve() / model_key
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_hits = 0
        self.cache_misses = 0
        self._group_sizes = (
            len(prompts.positive),
            len(prompts.advertisement),
            len(prompts.mismatch),
            len(prompts.scene_evidence),
        )
        all_prompts = (
            prompts.positive
            + prompts.advertisement
            + prompts.mismatch
            + prompts.scene_evidence
        )
        self._text_features = torch.cat(
            [self.encoder.encode_text(prompt) for prompt in all_prompts], dim=0
        )

    @staticmethod
    def _image_cache_key(image: Image.Image) -> str:
        digest = hashlib.sha256()
        rgb = image.convert("RGB")
        digest.update(str(rgb.size).encode("ascii"))
        digest.update(rgb.tobytes())
        return digest.hexdigest()

    def _load_cached_feature(self, image: Image.Image):
        if self.cache_dir is None:
            return None
        path = self.cache_dir / f"{self._image_cache_key(image)}.npy"
        try:
            array = np.load(path, allow_pickle=False)
            if (
                array.ndim != 2
                or array.shape[0] != 1
                or array.shape[1] != self._text_features.shape[1]
            ):
                raise ValueError("invalid cached feature shape")
            self.cache_hits += 1
            import torch

            return torch.from_numpy(np.asarray(array, dtype=np.float32)).to(
                device=self.device,
                dtype=self._text_features.dtype,
            )
        except (FileNotFoundError, OSError, ValueError):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            self.cache_misses += 1
            return None

    def _store_cached_feature(self, image: Image.Image, feature) -> None:
        if self.cache_dir is None:
            return
        path = self.cache_dir / f"{self._image_cache_key(image)}.npy"
        if path.exists():
            return
        temporary: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.cache_dir, prefix=f".{path.stem}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                np.save(handle, feature.detach().cpu().numpy().astype(np.float32), allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def score_images(self, images: Sequence[Image.Image]) -> list[SemanticScores]:
        if not images:
            return []
        import torch

        features: list[Optional[torch.Tensor]] = [
            self._load_cached_feature(image) for image in images
        ]
        missing_positions = [index for index, feature in enumerate(features) if feature is None]
        if missing_positions:
            missing_images = [images[index] for index in missing_positions]
            encoded = self.encoder.encode_images(list(missing_images))
            for encoded_index, image_index in enumerate(missing_positions):
                feature = encoded[encoded_index:encoded_index + 1]
                features[image_index] = feature
                self._store_cached_feature(images[image_index], feature)
        image_features = torch.cat([feature for feature in features if feature is not None], dim=0)
        raw = (image_features @ self._text_features.T).detach().cpu().numpy()
        positive_size, ad_size, mismatch_size, scene_size = self._group_sizes
        positive_end = positive_size
        ad_end = positive_end + ad_size
        mismatch_end = ad_end + mismatch_size
        scene_end = mismatch_end + scene_size
        results: list[SemanticScores] = []
        for row in raw:
            positive = row[:positive_end]
            advertisement = row[positive_end:ad_end]
            mismatch = row[ad_end:mismatch_end]
            positive_index = int(np.argmax(positive))
            advertisement_index = int(np.argmax(advertisement))
            mismatch_index = int(np.argmax(mismatch))
            if scene_size:
                scene = row[mismatch_end:scene_end]
                scene_index = int(np.argmax(scene))
                scene_score = float(scene[scene_index])
                scene_prompt = self.prompts.scene_evidence[scene_index]
            else:
                scene_score = float(positive[positive_index])
                scene_prompt = self.prompts.positive[positive_index]
            results.append(SemanticScores(
                relevance=float(positive[positive_index]),
                advertisement=float(advertisement[advertisement_index]),
                mismatch=float(mismatch[mismatch_index]),
                scene_evidence=scene_score,
                relevance_prompt=self.prompts.positive[positive_index],
                advertisement_prompt=self.prompts.advertisement[advertisement_index],
                mismatch_prompt=self.prompts.mismatch[mismatch_index],
                scene_evidence_prompt=scene_prompt,
            ))
        return results

