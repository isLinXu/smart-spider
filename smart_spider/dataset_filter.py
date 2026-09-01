# coding=utf-8
"""Offline semantic and advertisement filtering for image datasets.

Dry runs only write decisions and a summary. Applied runs move rejected images
to quarantine, back up the indexes, and can be restored without data loss.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

import numpy as np
from PIL import Image, UnidentifiedImageError
from tqdm import tqdm

from .image_retrieval import IMAGE_SUFFIXES


PROMOTION_TERMS = (
    "促销", "优惠", "特价", "厂家直销", "招商", "加盟", "批发", "报价",
    "咨询", "联系", "扫码", "微信", "电话", "广告", "限时", "sale",
    "discount", "promotion", "wholesale", "contact", "call now", "official",
)
CONTACT_PATTERNS = (
    re.compile(r"(?:https?://|www\.)", re.IGNORECASE),
    re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)(?:400|800)[- ]?\d{3}[- ]?\d{4}(?!\d)"),
)


@dataclass(frozen=True)
class PromptSet:
    positive: tuple[str, ...]
    advertisement: tuple[str, ...]
    mismatch: tuple[str, ...]
    scene_evidence: tuple[str, ...] = ()

    @classmethod
    def truck_loading(cls, query: str = "货车装卸区 开关门") -> "PromptSet":
        query = query.strip()
        custom_prompts = (query,) if query and query.isascii() else ()
        return cls(
            positive=custom_prompts + (
                "a cargo truck backed into a warehouse loading dock",
                "workers opening or closing the rear doors of a cargo truck at a loading area",
                "workers loading or unloading goods through the open rear doors of a truck",
                "a box truck with rear cargo doors open at a warehouse",
                "a loading bay occupied by a truck or trailer",
                "a delivery truck parked at a loading dock",
                "inside the cargo area of a truck with its rear doors open",
                "a freight trailer being loaded at a warehouse",
            ),
            advertisement=(
                "a commercial advertisement poster with sales text and phone numbers",
                "a company promotional banner or product brochure",
                "an e-commerce product listing with price and marketing text",
                "an industrial product sales poster with specifications",
                "a promotional image with QR code, phone number, or watermark",
                "a collage of product photos with labels",
                "a product cutout on a white background with marketing text",
                "a company logo graphic or website screenshot",
            ),
            mismatch=(
                "an empty warehouse loading dock with no truck",
                "a loading dock leveler, ramp, lift table, or platform shown as a product",
                "a forklift or construction vehicle with no cargo truck",
                "a close-up of a car door handle, hinge, latch, remote control, switch, or spare part",
                "a passenger car, SUV, pickup truck, bus, motorcycle, or vehicle interior",
                "an ambulance, fire truck, emergency vehicle, or rescue scene",
                "a cargo truck driving or parked on a road with no warehouse loading area",
                "a closed cargo truck photographed by itself in a parking lot or vehicle yard",
                "a front, side, or rear exterior view of a truck with its cargo doors closed",
                "a truck accident, breakdown, repair, roadside inspection, or towing scene",
                "a passenger van, car trunk, tailgate, or side door open",
                "a shipping port, container yard, crane, train, or ship with no loading dock",
                "an aerial photograph of a warehouse, port, or industrial park",
                "a warehouse interior with pallets or conveyor belts but no truck",
                "people carrying or sorting packages indoors with no cargo truck visible",
                "a warehouse rolling shutter or loading bay door with no truck present",
                "a close-up of a truck engine, wheel, chassis, suspension, or mechanical part",
                "workers repairing, washing, painting, inspecting, or changing the tire of a truck",
                "a person opening a truck cab door, hood, engine bay, or maintenance panel",
                "a dump truck, garbage truck, crane truck, tanker, or construction truck",
                "a crane lifting a container, machine, or cargo with no warehouse loading dock",
                "cargo being loaded onto or unloaded from an airplane, train, rail wagon, or ship",
                "an airport ground handling or air freight loading scene",
                "people loading construction debris, soil, logs, pipes, or scrap into a dump truck or flatbed",
                "a flatbed truck transporting cargo or machinery away from a warehouse dock",
                "cargo tied down on an open flatbed truck or trailer",
                "a truck carrying cargo on a road or in a vehicle yard",
                "people moving furniture through a house, building doorway, or moving van",
                "a shipping container being inspected, repaired, or handled away from a warehouse dock",
                "a truck tail lift or hydraulic loading platform demonstrated as equipment",
                "a close-up inside a passenger cab or non-cargo compartment",
                "a miniature model, toy, or 3D rendering of a truck loading mechanism",
                "a technical drawing, diagram, floor plan, infographic, or screenshot",
                "a product photograph on a plain white background",
                "a vehicle advertisement or product showcase photographed in a studio",
                "a portrait or fashion photograph",
                "an animal, flower, plant, food, landscape, tourist attraction, or artwork",
                "a computer, household object, tool, machinery part, or unrelated product",
                "a logo or QR code on a plain background",
            ),
            scene_evidence=(
                "workers loading or unloading boxes through open rear cargo doors at a warehouse",
                "a worker opening or closing the rear cargo doors of a truck",
                "a truck with its rear cargo doors visibly open at a loading area",
                "a forklift moving goods through open rear cargo doors at a warehouse loading dock",
                "goods being moved through the open rear doorway of a truck",
                "an open truck cargo compartment visibly framed by its rear doors",
            ),
        )


@dataclass(frozen=True)
class SemanticScores:
    relevance: float
    advertisement: float
    mismatch: float
    relevance_prompt: str = ""
    advertisement_prompt: str = ""
    mismatch_prompt: str = ""
    scene_evidence: float = 0.0
    scene_evidence_prompt: str = ""

    @property
    def ad_margin(self) -> float:
        return self.advertisement - self.relevance

    @property
    def mismatch_margin(self) -> float:
        return self.mismatch - self.relevance

    @property
    def scene_evidence_margin(self) -> float:
        return self.mismatch - self.scene_evidence


@dataclass(frozen=True)
class VisualSignals:
    text_area_ratio: float = 0.0
    text_box_count: int = 0
    qr_detected: bool = False
    ocr_text: str = ""
    promotion_hits: tuple[str, ...] = ()
    contact_hits: int = 0


@dataclass(frozen=True)
class FilterThresholds:
    min_relevance: float = 0.225
    mismatch_margin: float = 0.015
    advertisement_score: float = 0.235
    advertisement_margin: float = 0.005
    advertisement_support_score: float = 0.215
    advertisement_support_margin: float = -0.04
    text_area_ratio: float = 0.14
    minimum_scene_evidence: Optional[float] = None
    scene_evidence_margin: Optional[float] = None

    @classmethod
    def from_profile(cls, profile: str) -> "FilterThresholds":
        profiles = {
            "conservative": cls(
                min_relevance=0.205,
                mismatch_margin=0.025,
                advertisement_score=0.245,
                advertisement_margin=0.01,
                advertisement_support_score=0.225,
                advertisement_support_margin=-0.025,
                text_area_ratio=0.18,
            ),
            "balanced": cls(),
            "strict": cls(
                min_relevance=0.225,
                mismatch_margin=0.0,
                advertisement_score=0.23,
                advertisement_margin=0.0,
                advertisement_support_score=0.21,
                advertisement_support_margin=-0.05,
                text_area_ratio=0.12,
            ),
            "precision": cls(
                min_relevance=0.225,
                mismatch_margin=0.015,
                advertisement_score=0.23,
                advertisement_margin=0.0,
                advertisement_support_score=0.21,
                advertisement_support_margin=-0.05,
                text_area_ratio=0.12,
                minimum_scene_evidence=0.225,
                scene_evidence_margin=0.005,
            ),
        }
        try:
            return profiles[profile]
        except KeyError as exc:
            raise ValueError(f"unknown filter profile: {profile}") from exc


@dataclass(frozen=True)
class FilterDecision:
    record_position: int
    path: str
    action: str
    category: str
    reasons: tuple[str, ...]
    scores: Optional[SemanticScores] = None
    signals: VisualSignals = field(default_factory=VisualSignals)
    confidence: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reasons"] = list(self.reasons)
        payload["signals"]["promotion_hits"] = list(self.signals.promotion_hits)
        return payload


class PromptScorer(Protocol):
    def score_images(self, images: Sequence[Image.Image]) -> list[SemanticScores]:
        ...


class SignalAnalyzer(Protocol):
    def analyze(
        self,
        image: Image.Image,
        scores: Optional[SemanticScores] = None,
    ) -> VisualSignals:
        ...


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


class VisualSignalAnalyzer:
    """Detect QR codes and text-heavy advertising signals with optional OCR."""

    def __init__(
        self,
        *,
        enable_ocr: bool = False,
        ocr_all: bool = False,
        ocr_candidate_text_ratio: float = 0.015,
    ) -> None:
        self.enable_ocr = enable_ocr
        self.ocr_all = ocr_all
        self.ocr_candidate_text_ratio = ocr_candidate_text_ratio
        try:
            import cv2

            self.cv2 = cv2
            self._qr_detector = cv2.QRCodeDetector()
        except ImportError:
            self.cv2 = None
            self._qr_detector = None
        self.pytesseract = None
        if enable_ocr:
            try:
                import pytesseract

                pytesseract.get_tesseract_version()
                self.pytesseract = pytesseract
            except (ImportError, OSError):
                pass

    def _estimate_text_area(self, image: Image.Image) -> tuple[float, int]:
        if self.cv2 is None:
            return 0.0, 0
        cv2 = self.cv2
        rgb = np.asarray(image.convert("RGB"))
        height, width = rgb.shape[:2]
        scale = min(1.0, 1000.0 / max(height, width))
        if scale < 1.0:
            rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        gradient = cv2.Sobel(gray, cv2.CV_8U, 1, 0, ksize=3)
        _, binary = cv2.threshold(gradient, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel_width = max(5, int(binary.shape[1] * 0.012))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, 3))
        connected = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(gray)
        box_count = 0
        image_area = float(gray.shape[0] * gray.shape[1])
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            box_area = box_width * box_height
            if box_width < 12 or box_height < 4:
                continue
            if box_height > gray.shape[0] * 0.18 or box_area > image_area * 0.12:
                continue
            aspect = box_width / max(box_height, 1)
            if not 1.2 <= aspect <= 30.0:
                continue
            cv2.rectangle(mask, (x, y), (x + box_width, y + box_height), 255, -1)
            box_count += 1
        ratio = float(np.count_nonzero(mask) / image_area) if image_area else 0.0
        return min(ratio, 1.0), box_count

    def _detect_qr(self, image: Image.Image) -> bool:
        if self._qr_detector is None:
            return False
        try:
            rgb = np.asarray(image.convert("RGB"))
            height, width = rgb.shape[:2]
            scale = min(1.0, 1400.0 / max(height, width))
            if scale < 1.0:
                rgb = self.cv2.resize(
                    rgb, None, fx=scale, fy=scale, interpolation=self.cv2.INTER_AREA
                )
            decoded, _, _ = self._qr_detector.detectAndDecode(
                rgb
            )
            return bool(str(decoded).strip())
        except Exception:
            return False

    def _run_ocr(self, image: Image.Image) -> tuple[str, float, int]:
        if not self.enable_ocr or self.pytesseract is None:
            return "", 0.0, 0
        resized = image.convert("RGB")
        if max(resized.size) > 1600:
            resized.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        try:
            output = self.pytesseract.image_to_data(
                resized,
                lang="eng",
                config="--psm 11",
                output_type=self.pytesseract.Output.DICT,
            )
        except Exception:
            return "", 0.0, 0
        texts: list[str] = []
        area = 0
        count = 0
        for index, text in enumerate(output.get("text", [])):
            text = str(text or "").strip()
            try:
                confidence = float(output["conf"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                confidence = -1.0
            if not text or confidence < 35:
                continue
            texts.append(text)
            width = int(output["width"][index])
            height = int(output["height"][index])
            area += max(width, 0) * max(height, 0)
            count += 1
        total_area = float(resized.width * resized.height)
        return " ".join(texts), min(area / total_area, 1.0) if total_area else 0.0, count

    def analyze(
        self,
        image: Image.Image,
        scores: Optional[SemanticScores] = None,
    ) -> VisualSignals:
        estimated_ratio, estimated_boxes = self._estimate_text_area(image)
        qr_detected = self._detect_qr(image)
        should_ocr = self.ocr_all or qr_detected or estimated_ratio >= self.ocr_candidate_text_ratio
        if scores is not None and scores.ad_margin >= -0.035:
            should_ocr = True
        ocr_text, ocr_ratio, ocr_boxes = (
            self._run_ocr(image) if should_ocr else ("", 0.0, 0)
        )
        lowered = ocr_text.casefold()
        promotion_hits = tuple(term for term in PROMOTION_TERMS if term.casefold() in lowered)
        contact_hits = sum(len(pattern.findall(ocr_text)) for pattern in CONTACT_PATTERNS)
        return VisualSignals(
            text_area_ratio=max(estimated_ratio, ocr_ratio),
            text_box_count=max(estimated_boxes, ocr_boxes),
            qr_detected=qr_detected,
            ocr_text=ocr_text[:1000],
            promotion_hits=promotion_hits,
            contact_hits=contact_hits,
        )


class DecisionPolicy:
    def __init__(self, thresholds: Optional[FilterThresholds] = None) -> None:
        self.thresholds = thresholds or FilterThresholds()

    def decide(
        self,
        *,
        record_position: int,
        path: str,
        scores: SemanticScores,
        signals: VisualSignals,
    ) -> FilterDecision:
        thresholds = self.thresholds
        reasons: list[str] = []
        ad_evidence = 0
        if signals.qr_detected:
            reasons.append("qr_code")
            ad_evidence += 4
        if signals.promotion_hits:
            reasons.append("promotion_terms")
            ad_evidence += 2
        if signals.contact_hits:
            reasons.append("contact_information")
            ad_evidence += min(signals.contact_hits, 2)
        if signals.text_area_ratio >= thresholds.text_area_ratio:
            reasons.append("text_heavy")
            ad_evidence += 1
        semantic_ad = (
            scores.advertisement >= thresholds.advertisement_score
            and scores.ad_margin >= thresholds.advertisement_margin
        )
        if semantic_ad:
            reasons.append("clip_advertisement")
            ad_evidence += 3
        supported_ad = (
            signals.text_area_ratio >= thresholds.text_area_ratio
            and scores.advertisement >= thresholds.advertisement_support_score
            and scores.ad_margin >= thresholds.advertisement_support_margin
        )
        if supported_ad and not semantic_ad:
            reasons.append("text_supported_advertisement")
            ad_evidence += 2
        if ad_evidence >= 3:
            confidence = "high" if (
                signals.qr_detected
                or bool(signals.promotion_hits)
                or signals.contact_hits > 0
                or scores.ad_margin >= 0.02
            ) else "medium"
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="advertisement",
                reasons=tuple(reasons),
                scores=scores,
                signals=signals,
                confidence=confidence,
            )

        mismatch_reasons: list[str] = []
        if scores.relevance < thresholds.min_relevance:
            mismatch_reasons.append("low_relevance")
        if scores.mismatch_margin >= thresholds.mismatch_margin:
            mismatch_reasons.append("negative_prompt_wins")
        if (
            thresholds.minimum_scene_evidence is not None
            and scores.scene_evidence < thresholds.minimum_scene_evidence
        ):
            mismatch_reasons.append("low_scene_evidence")
        if (
            thresholds.scene_evidence_margin is not None
            and scores.scene_evidence_margin >= thresholds.scene_evidence_margin
        ):
            mismatch_reasons.append("scene_negative_prompt_wins")
        if mismatch_reasons:
            confidence = "high" if (
                scores.relevance < thresholds.min_relevance - 0.03
                or scores.mismatch_margin >= thresholds.mismatch_margin + 0.03
                or (
                    thresholds.minimum_scene_evidence is not None
                    and scores.scene_evidence < thresholds.minimum_scene_evidence - 0.03
                )
                or (
                    thresholds.scene_evidence_margin is not None
                    and scores.scene_evidence_margin >= thresholds.scene_evidence_margin + 0.03
                )
            ) else "medium"
            return FilterDecision(
                record_position=record_position,
                path=path,
                action="quarantine",
                category="semantic_mismatch",
                reasons=tuple(mismatch_reasons),
                scores=scores,
                signals=signals,
                confidence=confidence,
            )
        confidence = "high" if (
            scores.relevance >= thresholds.min_relevance + 0.045
            and scores.ad_margin <= -0.02
            and scores.mismatch_margin <= -0.02
        ) else "medium"
        return FilterDecision(
            record_position=record_position,
            path=path,
            action="keep",
            category="relevant",
            reasons=(),
            scores=scores,
            signals=signals,
            confidence=confidence,
        )


def _atomic_write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
    return records


def _record_path(record: dict[str, Any]) -> str:
    return str(record.get("file_path") or record.get("file") or "")


def _manifest_path(record: dict[str, Any]) -> str:
    if record.get("file"):
        return str(record["file"])
    for modality in record.get("modalities") or []:
        value = modality.get("path") or modality.get("uri")
        if value:
            return str(value)
    return ""


class DatasetImageFilter:
    """Evaluate a dataset and optionally apply reversible quarantine."""

    ACTIVE_RUN_FILE = ".active_filter_run.json"
    RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    @classmethod
    def _read_active_runs(cls, dataset: Path) -> list[dict[str, Any]]:
        active_path = dataset / cls.ACTIVE_RUN_FILE
        if not active_path.exists():
            return []
        payload = json.loads(active_path.read_text(encoding="utf-8"))
        raw_runs = payload.get("runs")
        if raw_runs is None:
            raw_runs = [payload]
        if not isinstance(raw_runs, list) or not all(isinstance(item, dict) for item in raw_runs):
            raise ValueError("invalid active filter run stack")
        runs: list[dict[str, Any]] = []
        for item in raw_runs:
            run_id = str(item.get("run_id") or "")
            if not cls.RUN_ID_PATTERN.fullmatch(run_id):
                raise ValueError("active filter run stack contains an invalid run id")
            runs.append({
                "run_id": run_id,
                "run_dir": str(dataset / "_quarantine" / run_id),
                "quarantined": int(item.get("quarantined") or 0),
            })
        return runs

    @classmethod
    def _write_active_runs(cls, dataset: Path, runs: Sequence[dict[str, Any]]) -> None:
        active_path = dataset / cls.ACTIVE_RUN_FILE
        if not runs:
            active_path.unlink(missing_ok=True)
            return
        latest = dict(runs[-1])
        payload = {
            "version": 2,
            "runs": [dict(item) for item in runs],
            **latest,
        }
        _atomic_write_json(active_path, payload)

    def __init__(
        self,
        dataset_dir: str | os.PathLike[str],
        scorer: PromptScorer,
        analyzer: SignalAnalyzer,
        *,
        policy: Optional[DecisionPolicy] = None,
        batch_size: int = 32,
    ) -> None:
        self.dataset_dir = Path(dataset_dir).expanduser().resolve()
        if not self.dataset_dir.is_dir():
            raise NotADirectoryError(f"dataset directory not found: {self.dataset_dir}")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.scorer = scorer
        self.analyzer = analyzer
        self.policy = policy or DecisionPolicy()
        self.batch_size = batch_size

    def _load_records(self) -> list[dict[str, Any]]:
        records = _read_jsonl(self.dataset_dir / "metadata.jsonl")
        if records:
            return records
        return [
            {"file_path": str(path.relative_to(self.dataset_dir))}
            for path in sorted(self.dataset_dir.glob("batch_*/*"))
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ]

    def _resolve_path(self, raw_path: str) -> tuple[Path, str]:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        elif candidate.parts and candidate.parts[0] == self.dataset_dir.name:
            resolved = (self.dataset_dir.parent / candidate).resolve()
        else:
            resolved = (self.dataset_dir / candidate).resolve()
        try:
            relative = resolved.relative_to(self.dataset_dir)
        except ValueError as exc:
            raise ValueError(f"image path escapes dataset directory: {raw_path}") from exc
        return resolved, str(relative)

    @staticmethod
    def _select_positions(total: int, limit: Optional[int]) -> list[int]:
        if limit is None or limit >= total:
            return list(range(total))
        if limit <= 0:
            raise ValueError("limit must be positive")
        if limit == 1:
            return [0]
        return [round(index * (total - 1) / (limit - 1)) for index in range(limit)]

    def evaluate(self, *, limit: Optional[int] = None) -> tuple[list[FilterDecision], int]:
        records = self._load_records()
        positions = self._select_positions(len(records), limit)
        decisions: list[FilterDecision] = []
        progress = tqdm(total=len(positions), desc="DatasetFilter")
        try:
            for offset in range(0, len(positions), self.batch_size):
                batch_positions = positions[offset:offset + self.batch_size]
                valid: list[tuple[int, str, Image.Image]] = []
                for position in batch_positions:
                    raw_path = _record_path(records[position])
                    try:
                        absolute, relative = self._resolve_path(raw_path)
                        with Image.open(absolute) as opened:
                            image = opened.convert("RGB")
                        valid.append((position, relative, image))
                    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError) as exc:
                        decisions.append(FilterDecision(
                            record_position=position,
                            path=raw_path,
                            action="quarantine",
                            category="invalid",
                            reasons=("invalid_or_missing_image",),
                            error=str(exc),
                        ))
                        progress.update(1)
                if not valid:
                    continue
                scores = self.scorer.score_images([item[2] for item in valid])
                if len(scores) != len(valid):
                    raise ValueError("scorer returned a row count different from input images")
                for (position, relative, image), semantic_scores in zip(valid, scores):
                    signals = self.analyzer.analyze(image, semantic_scores)
                    decisions.append(self.policy.decide(
                        record_position=position,
                        path=relative,
                        scores=semantic_scores,
                        signals=signals,
                    ))
                    progress.update(1)
        finally:
            progress.close()
        decisions.sort(key=lambda item: item.record_position)
        return decisions, len(records)

    @staticmethod
    def _percentiles(values: Sequence[float]) -> dict[str, float]:
        if not values:
            return {}
        return {
            key: round(float(value), 6)
            for key, value in zip(
                ("p05", "p25", "p50", "p75", "p95"),
                np.percentile(np.asarray(values, dtype=np.float32), [5, 25, 50, 75, 95]),
            )
        }

    def _build_report(
        self,
        decisions: Sequence[FilterDecision],
        total_records: int,
        *,
        run_id: str,
        applied: bool,
    ) -> dict[str, Any]:
        categories = Counter(item.category for item in decisions)
        actions = Counter(item.action for item in decisions)
        reason_counts = Counter(reason for item in decisions for reason in item.reasons)
        confidence_counts = Counter(item.confidence or "unknown" for item in decisions)
        prompt_counts = Counter(
            item.scores.advertisement_prompt
            for item in decisions
            if item.scores is not None and item.scores.advertisement_prompt
        )
        scored = [item.scores for item in decisions if item.scores is not None]
        report = {
            "run_id": run_id,
            "dataset": str(self.dataset_dir),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "applied": applied,
            "dataset_records": total_records,
            "scanned": len(decisions),
            "actions": dict(actions),
            "categories": dict(categories),
            "reasons": dict(reason_counts),
            "confidence": dict(confidence_counts),
            "advertisement_prompt_hits": dict(prompt_counts.most_common(20)),
            "thresholds": asdict(self.policy.thresholds),
            "score_percentiles": {
                "relevance": self._percentiles([item.relevance for item in scored]),
                "advertisement": self._percentiles([item.advertisement for item in scored]),
                "mismatch": self._percentiles([item.mismatch for item in scored]),
                "scene_evidence": self._percentiles([item.scene_evidence for item in scored]),
                "ad_margin": self._percentiles([item.ad_margin for item in scored]),
                "mismatch_margin": self._percentiles([item.mismatch_margin for item in scored]),
                "scene_evidence_margin": self._percentiles([
                    item.scene_evidence_margin for item in scored
                ]),
            },
        }
        if hasattr(self.scorer, "cache_hits"):
            report["feature_cache"] = {
                "enabled": getattr(self.scorer, "cache_dir", None) is not None,
                "hits": int(getattr(self.scorer, "cache_hits", 0)),
                "misses": int(getattr(self.scorer, "cache_misses", 0)),
            }
        return report

    def _apply(
        self,
        decisions: Sequence[FilterDecision],
        records: Sequence[dict[str, Any]],
        run_dir: Path,
        run_id: str,
    ) -> None:
        active_runs = self._read_active_runs(self.dataset_dir)
        rejected = [item for item in decisions if item.action == "quarantine"]
        invalid = [item for item in rejected if item.category == "invalid"]
        if invalid:
            raise RuntimeError(
                f"cannot apply a run containing {len(invalid)} invalid or missing images"
            )
        paths = [item.path for item in rejected]
        if len(paths) != len(set(paths)):
            raise RuntimeError("cannot apply a run containing duplicate image paths")
        backups = run_dir / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        for name in ("metadata.jsonl", "manifest.jsonl"):
            source = self.dataset_dir / name
            if source.exists():
                shutil.copy2(source, backups / name)

        quarantine_records: list[dict[str, Any]] = []
        moved: list[tuple[Path, Path]] = []
        try:
            for decision in rejected:
                source, relative = self._resolve_path(decision.path)
                if not source.exists():
                    raise FileNotFoundError(f"image disappeared during filtering: {source}")
                destination = run_dir / "files" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise FileExistsError(f"quarantine destination exists: {destination}")
                os.replace(source, destination)
                moved.append((source, destination))
                quarantine_records.append({
                    "original_path": relative,
                    "quarantine_path": str(destination.relative_to(run_dir)),
                    "decision": decision.to_dict(),
                    "metadata": records[decision.record_position],
                })

            rejected_positions = {item.record_position for item in rejected}
            kept_metadata = [
                record for position, record in enumerate(records)
                if position not in rejected_positions
            ]
            _atomic_write_jsonl(self.dataset_dir / "metadata.jsonl", kept_metadata)

            manifest_path = self.dataset_dir / "manifest.jsonl"
            manifest = _read_jsonl(manifest_path)
            if manifest:
                if len(manifest) == len(records):
                    kept_manifest = [
                        record for position, record in enumerate(manifest)
                        if position not in rejected_positions
                    ]
                else:
                    rejected_paths = {item.path for item in rejected}
                    kept_manifest = [
                        record for record in manifest
                        if _manifest_path(record) not in rejected_paths
                    ]
                _atomic_write_jsonl(manifest_path, kept_manifest)

            _atomic_write_jsonl(run_dir / "quarantine_manifest.jsonl", quarantine_records)
            if len(quarantine_records) != len(rejected):
                raise RuntimeError("not all rejected files were moved to quarantine")
            active_runs.append({
                "run_id": run_id,
                "run_dir": str(run_dir),
                "quarantined": len(moved),
            })
            self._write_active_runs(self.dataset_dir, active_runs)
        except Exception:
            for original, quarantined in reversed(moved):
                if quarantined.exists() and not original.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(quarantined, original)
            for name in ("metadata.jsonl", "manifest.jsonl"):
                backup = backups / name
                if backup.exists():
                    shutil.copy2(backup, self.dataset_dir / name)
            raise

    def run(
        self,
        *,
        limit: Optional[int] = None,
        apply: bool = False,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if apply and limit is not None:
            raise ValueError("--apply cannot be combined with --limit")
        run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        if not self.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id may only contain letters, digits, dot, underscore, and dash")
        base = "_quarantine" if apply else "_filter_runs"
        run_dir = self.dataset_dir / base / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        decisions, total_records = self.evaluate(limit=limit)
        records = self._load_records()
        _atomic_write_jsonl(
            run_dir / "decisions.jsonl",
            [decision.to_dict() for decision in decisions],
        )
        report = self._build_report(
            decisions,
            total_records,
            run_id=run_id,
            applied=apply,
        )
        if apply:
            active_runs = self._read_active_runs(self.dataset_dir)
            report["parent_run_id"] = active_runs[-1]["run_id"] if active_runs else None
            self._apply(decisions, records, run_dir, run_id)
            report["quarantined"] = sum(item.action == "quarantine" for item in decisions)
            report["remaining"] = sum(item.action == "keep" for item in decisions)
        _atomic_write_json(run_dir / "report.json", report)
        return report

    @classmethod
    def run_exact_dedupe(
        cls,
        dataset_dir: str | os.PathLike[str],
        *,
        apply: bool = False,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Find exact byte duplicates and optionally quarantine later copies."""
        dataset = Path(dataset_dir).expanduser().resolve()
        if not dataset.is_dir():
            raise NotADirectoryError(f"dataset directory not found: {dataset}")
        run_id = run_id or datetime.now().strftime("exact_dedupe_%Y%m%d_%H%M%S")
        if not cls.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id may only contain letters, digits, dot, underscore, and dash")
        base = "_quarantine" if apply else "_filter_runs"
        run_dir = dataset / base / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        records = _read_jsonl(dataset / "metadata.jsonl")
        seen: dict[str, str] = {}
        duplicate_groups: set[str] = set()
        decisions: list[FilterDecision] = []
        for position, record in enumerate(records):
            raw_path = _record_path(record)
            try:
                absolute, relative = cls._resolve_for_verify(dataset, raw_path)
                digest = cls._sha256(absolute)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise RuntimeError(f"cannot deduplicate invalid image {raw_path}: {exc}") from exc
            original = seen.get(digest)
            if original is None:
                seen[digest] = relative
                decisions.append(FilterDecision(
                    record_position=position,
                    path=relative,
                    action="keep",
                    category="unique",
                    reasons=(),
                    confidence="high",
                ))
                continue
            duplicate_groups.add(digest)
            decisions.append(FilterDecision(
                record_position=position,
                path=relative,
                action="quarantine",
                category="exact_duplicate",
                reasons=("exact_sha256_duplicate",),
                confidence="high",
                error=f"duplicate_of={original}",
            ))

        _atomic_write_jsonl(
            run_dir / "decisions.jsonl",
            [decision.to_dict() for decision in decisions],
        )
        duplicate_count = sum(item.action == "quarantine" for item in decisions)
        report: dict[str, Any] = {
            "run_id": run_id,
            "dataset": str(dataset),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "applied": apply,
            "dataset_records": len(records),
            "scanned": len(records),
            "actions": {
                "keep": len(records) - duplicate_count,
                "quarantine": duplicate_count,
            },
            "categories": {
                "unique": len(records) - duplicate_count,
                "exact_duplicate": duplicate_count,
            },
            "duplicate_groups": len(duplicate_groups),
        }
        if apply:
            active_runs = cls._read_active_runs(dataset)
            report["parent_run_id"] = active_runs[-1]["run_id"] if active_runs else None
            instance = cls.__new__(cls)
            instance.dataset_dir = dataset
            instance._apply(decisions, records, run_dir, run_id)
            report["quarantined"] = duplicate_count
            report["remaining"] = len(records) - duplicate_count
        _atomic_write_json(run_dir / "report.json", report)
        return report

    @staticmethod
    def _perceptual_signature(path: Path) -> tuple[int, float, tuple[float, float, float]]:
        with Image.open(path) as opened:
            rgb = opened.convert("RGB")
            aspect = rgb.width / max(rgb.height, 1)
            mean_color = tuple(
                float(value)
                for value in np.asarray(rgb.resize((1, 1), Image.Resampling.BOX))[0, 0]
            )
            gray = rgb.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
            pixels = np.asarray(gray, dtype=np.int16)
        bits = (pixels[:, 1:] > pixels[:, :-1]).reshape(-1)
        value = 0
        for enabled in bits:
            value = (value << 1) | int(enabled)
        return value, aspect, mean_color

    @classmethod
    def run_near_dedupe(
        cls,
        dataset_dir: str | os.PathLike[str],
        *,
        max_distance: int = 4,
        max_aspect_delta: float = 0.03,
        max_color_distance: float = 35.0,
        apply: bool = False,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Find visually near-identical images with conservative safety gates."""
        if not 0 <= max_distance <= 64:
            raise ValueError("near-duplicate hash distance must be between 0 and 64")
        if max_aspect_delta < 0.0:
            raise ValueError("near-duplicate aspect delta cannot be negative")
        if max_color_distance < 0.0:
            raise ValueError("near-duplicate color distance cannot be negative")
        dataset = Path(dataset_dir).expanduser().resolve()
        if not dataset.is_dir():
            raise NotADirectoryError(f"dataset directory not found: {dataset}")
        run_id = run_id or datetime.now().strftime("near_dedupe_%Y%m%d_%H%M%S")
        if not cls.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id may only contain letters, digits, dot, underscore, and dash")
        base = "_quarantine" if apply else "_filter_runs"
        run_dir = dataset / base / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        records = _read_jsonl(dataset / "metadata.jsonl")
        representatives: list[tuple[str, int, float, tuple[float, float, float]]] = []
        duplicate_groups: set[str] = set()
        decisions: list[FilterDecision] = []
        for position, record in enumerate(records):
            raw_path = _record_path(record)
            try:
                absolute, relative = cls._resolve_for_verify(dataset, raw_path)
                image_hash, aspect, mean_color = cls._perceptual_signature(absolute)
            except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError) as exc:
                raise RuntimeError(f"cannot inspect near duplicate {raw_path}: {exc}") from exc
            best: Optional[tuple[str, int, float]] = None
            for original, known_hash, known_aspect, known_color in representatives:
                aspect_delta = abs(aspect - known_aspect) / max(aspect, known_aspect, 1e-9)
                if aspect_delta > max_aspect_delta:
                    continue
                color_distance = sum(
                    (left - right) ** 2 for left, right in zip(mean_color, known_color)
                ) ** 0.5
                if color_distance > max_color_distance:
                    continue
                distance = (image_hash ^ known_hash).bit_count()
                if distance <= max_distance and (best is None or distance < best[1]):
                    best = (original, distance, color_distance)
            if best is None:
                representatives.append((relative, image_hash, aspect, mean_color))
                decisions.append(FilterDecision(
                    record_position=position,
                    path=relative,
                    action="keep",
                    category="perceptually_unique",
                    reasons=(),
                    confidence="high",
                ))
                continue
            original, distance, color_distance = best
            duplicate_groups.add(original)
            decisions.append(FilterDecision(
                record_position=position,
                path=relative,
                action="quarantine",
                category="near_duplicate",
                reasons=("perceptual_hash_near_duplicate",),
                confidence="medium",
                error=(
                    f"duplicate_of={original};hamming_distance={distance};"
                    f"color_distance={color_distance:.3f}"
                ),
            ))

        _atomic_write_jsonl(
            run_dir / "decisions.jsonl",
            [decision.to_dict() for decision in decisions],
        )
        duplicate_count = sum(item.action == "quarantine" for item in decisions)
        report: dict[str, Any] = {
            "run_id": run_id,
            "dataset": str(dataset),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "applied": apply,
            "dataset_records": len(records),
            "scanned": len(records),
            "actions": {
                "keep": len(records) - duplicate_count,
                "quarantine": duplicate_count,
            },
            "categories": {
                "perceptually_unique": len(records) - duplicate_count,
                "near_duplicate": duplicate_count,
            },
            "duplicate_groups": len(duplicate_groups),
            "thresholds": {
                "max_distance": max_distance,
                "max_aspect_delta": max_aspect_delta,
                "max_color_distance": max_color_distance,
            },
        }
        if apply:
            active_runs = cls._read_active_runs(dataset)
            report["parent_run_id"] = active_runs[-1]["run_id"] if active_runs else None
            instance = cls.__new__(cls)
            instance.dataset_dir = dataset
            instance._apply(decisions, records, run_dir, run_id)
            report["quarantined"] = duplicate_count
            report["remaining"] = len(records) - duplicate_count
        _atomic_write_json(run_dir / "report.json", report)
        return report

    @classmethod
    def run_review_rejections(
        cls,
        dataset_dir: str | os.PathLike[str],
        reject_paths: Sequence[str],
        *,
        apply: bool = False,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Apply an auditable human-review rejection list as a reversible run."""
        dataset = Path(dataset_dir).expanduser().resolve()
        if not dataset.is_dir():
            raise NotADirectoryError(f"dataset directory not found: {dataset}")
        run_id = run_id or datetime.now().strftime("review_reject_%Y%m%d_%H%M%S")
        if not cls.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id may only contain letters, digits, dot, underscore, and dash")

        requested = [str(Path(value.strip())) for value in reject_paths if value.strip()]
        if not requested:
            raise ValueError("review rejection list is empty")
        if len(requested) != len(set(requested)):
            raise ValueError("review rejection list contains duplicate paths")

        records = _read_jsonl(dataset / "metadata.jsonl")
        normalized_records: list[str] = []
        for record in records:
            _, relative = cls._resolve_for_verify(dataset, _record_path(record))
            normalized_records.append(relative)
        available = set(normalized_records)
        missing = sorted(set(requested) - available)
        if missing:
            preview = ", ".join(missing[:5])
            raise ValueError(f"review rejection paths are not active dataset records: {preview}")

        rejected = set(requested)
        decisions = [
            FilterDecision(
                record_position=position,
                path=relative,
                action="quarantine" if relative in rejected else "keep",
                category="manual_review_reject" if relative in rejected else "review_approved",
                reasons=("manual_visual_review",) if relative in rejected else (),
                confidence="high",
            )
            for position, relative in enumerate(normalized_records)
        ]
        base = "_quarantine" if apply else "_filter_runs"
        run_dir = dataset / base / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _atomic_write_jsonl(
            run_dir / "decisions.jsonl",
            [decision.to_dict() for decision in decisions],
        )
        reject_count = len(rejected)
        report: dict[str, Any] = {
            "run_id": run_id,
            "dataset": str(dataset),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "applied": apply,
            "dataset_records": len(records),
            "scanned": len(records),
            "actions": {
                "keep": len(records) - reject_count,
                "quarantine": reject_count,
            },
            "categories": {
                "review_approved": len(records) - reject_count,
                "manual_review_reject": reject_count,
            },
        }
        if apply:
            active_runs = cls._read_active_runs(dataset)
            report["parent_run_id"] = active_runs[-1]["run_id"] if active_runs else None
            instance = cls.__new__(cls)
            instance.dataset_dir = dataset
            instance._apply(decisions, records, run_dir, run_id)
            report["quarantined"] = reject_count
            report["remaining"] = len(records) - reject_count
        _atomic_write_json(run_dir / "report.json", report)
        return report

    @staticmethod
    def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def verify(
        cls,
        dataset_dir: str | os.PathLike[str],
        *,
        run_id: Optional[str] = None,
        verify_images: bool = True,
        verify_hashes: bool = False,
    ) -> dict[str, Any]:
        """Validate indexes, active quarantine files, and optional image hashes."""
        dataset = Path(dataset_dir).expanduser().resolve()
        errors: list[str] = []
        if not dataset.is_dir():
            return {"ok": False, "dataset": str(dataset), "errors": ["dataset directory not found"]}

        def read(path: Path) -> list[dict[str, Any]]:
            try:
                return _read_jsonl(path)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
                return []

        metadata = read(dataset / "metadata.jsonl")
        manifest = read(dataset / "manifest.jsonl")

        def normalized_paths(
            records: Sequence[dict[str, Any]],
            path_getter: Callable[[dict[str, Any]], str],
        ) -> set[str]:
            paths: set[str] = set()
            for record in records:
                raw_path = path_getter(record)
                if not raw_path:
                    continue
                try:
                    _, relative = cls._resolve_for_verify(dataset, raw_path)
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                paths.add(relative)
            return paths

        def manifest_hashes(records: Sequence[dict[str, Any]]) -> dict[str, str]:
            hashes: dict[str, str] = {}
            for record in records:
                raw_path = _manifest_path(record)
                sample_id = str(record.get("id") or "")
                if not raw_path or not sample_id.startswith("sha256:"):
                    continue
                try:
                    _, relative = cls._resolve_for_verify(dataset, raw_path)
                except ValueError:
                    continue
                hashes[relative] = sample_id.removeprefix("sha256:")
            return hashes

        active_manifest_hashes = manifest_hashes(manifest)
        try:
            active_runs = cls._read_active_runs(dataset)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"invalid active run marker: {exc}")
            active_runs = []
        active_ids = [item["run_id"] for item in active_runs]
        if run_id and run_id not in active_ids:
            errors.append(f"requested run {run_id!r} is not active")
        if run_id and not active_runs:
            errors.append(f"requested run {run_id!r} has no active marker")

        image_records = 0
        checked_images = 0
        hashed_images = 0
        seen_paths: set[str] = set()
        for position, record in enumerate(metadata):
            raw = _record_path(record)
            if not raw:
                errors.append(f"metadata record {position} has no image path")
                continue
            try:
                absolute, relative = cls._resolve_for_verify(dataset, raw)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            image_records += 1
            if relative in seen_paths:
                errors.append(f"duplicate active image path: {relative}")
            seen_paths.add(relative)
            if not absolute.exists():
                errors.append(f"active image missing: {relative}")
                continue
            if verify_images:
                try:
                    with Image.open(absolute) as opened:
                        opened.verify()
                    checked_images += 1
                except (OSError, UnidentifiedImageError) as exc:
                    errors.append(f"invalid active image {relative}: {exc}")
            expected_hash = str(record.get("sha256") or active_manifest_hashes.get(relative) or "")
            if verify_hashes and expected_hash:
                actual_hash = cls._sha256(absolute)
                hashed_images += 1
                if actual_hash != expected_hash:
                    errors.append(f"sha256 mismatch for active image: {relative}")

        if manifest and len(manifest) != len(metadata):
            errors.append(
                f"manifest/metadata length mismatch: {len(manifest)} != {len(metadata)}"
            )

        metadata_paths = normalized_paths(metadata, _record_path)
        manifest_paths = normalized_paths(manifest, _manifest_path)
        if manifest and metadata_paths != manifest_paths:
            errors.append("manifest and metadata image paths differ")

        quarantined = 0
        quarantine_checked = 0
        quarantine_hashed = 0
        run_summaries: list[dict[str, Any]] = []
        for active_run in active_runs:
            active_run_id = active_run["run_id"]
            quarantine_run = dataset / "_quarantine" / active_run_id
            if not quarantine_run.is_dir():
                errors.append(f"active quarantine directory missing: {quarantine_run}")
                continue
            qrecords = read(quarantine_run / "quarantine_manifest.jsonl")
            decisions = read(quarantine_run / "decisions.jsonl")
            backup_metadata = read(quarantine_run / "backups" / "metadata.jsonl")
            backup_manifest = read(quarantine_run / "backups" / "manifest.jsonl")
            backup_manifest_hashes = manifest_hashes(backup_manifest)
            run_report_path = quarantine_run / "report.json"
            run_report: dict[str, Any] = {}
            try:
                run_report = json.loads(run_report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid run report {run_report_path}: {exc}")
            if decisions and len(decisions) != len(backup_metadata):
                errors.append(
                    f"run {active_run_id} decision/backup mismatch: "
                    f"{len(decisions)} != {len(backup_metadata)}"
                )
            reported_quarantine = int(run_report.get("quarantined") or 0)
            if reported_quarantine and reported_quarantine != len(qrecords):
                errors.append(
                    f"run {active_run_id} report/quarantine mismatch: "
                    f"{reported_quarantine} != {len(qrecords)}"
                )
            for item in qrecords:
                try:
                    original = str(item["original_path"])
                    qpath = (quarantine_run / str(item["quarantine_path"])).resolve()
                    original_path = (dataset / original).resolve()
                    qpath.relative_to(quarantine_run)
                    original_path.relative_to(dataset)
                except (KeyError, ValueError, TypeError) as exc:
                    errors.append(f"unsafe quarantine record: {exc}")
                    continue
                if original in seen_paths:
                    errors.append(f"image path appears more than once across active runs: {original}")
                seen_paths.add(original)
                if original_path.exists():
                    errors.append(f"quarantined image also active: {original}")
                if not qpath.exists():
                    errors.append(f"quarantined image missing: {qpath}")
                    continue
                quarantined += 1
                if verify_images:
                    try:
                        with Image.open(qpath) as opened:
                            opened.verify()
                        quarantine_checked += 1
                    except (OSError, UnidentifiedImageError) as exc:
                        errors.append(f"invalid quarantined image {qpath}: {exc}")
                expected_hash = str(
                    (item.get("metadata") or {}).get("sha256")
                    or backup_manifest_hashes.get(original)
                    or ""
                )
                if verify_hashes and expected_hash:
                    actual_hash = cls._sha256(qpath)
                    quarantine_hashed += 1
                    if actual_hash != expected_hash:
                        errors.append(f"sha256 mismatch for quarantined image: {original}")
            run_summaries.append({
                "run_id": active_run_id,
                "backup_records": len(backup_metadata),
                "decisions": len(decisions),
                "quarantined": len(qrecords),
            })

        if active_runs:
            baseline = read(
                dataset / "_quarantine" / active_runs[0]["run_id"] / "backups" / "metadata.jsonl"
            )
            baseline_paths = normalized_paths(baseline, _record_path)
            if baseline_paths and baseline_paths != seen_paths:
                missing = len(baseline_paths - seen_paths)
                unexpected = len(seen_paths - baseline_paths)
                errors.append(
                    f"active/quarantine chain differs from baseline: "
                    f"missing={missing}, unexpected={unexpected}"
                )

        result = {
            "ok": not errors,
            "dataset": str(dataset),
            "metadata_records": len(metadata),
            "manifest_records": len(manifest),
            "active_images": image_records,
            "quarantined_images": quarantined,
            "checked_active_images": checked_images,
            "checked_quarantined_images": quarantine_checked,
            "hashed_active_images": hashed_images,
            "hashed_quarantined_images": quarantine_hashed,
            "active_runs": run_summaries,
            "errors": errors,
        }
        return result

    @staticmethod
    def _resolve_for_verify(dataset: Path, raw_path: str) -> tuple[Path, str]:
        candidate = Path(raw_path).expanduser()
        if candidate.is_absolute():
            resolved = candidate.resolve()
        elif candidate.parts and candidate.parts[0] == dataset.name:
            resolved = (dataset.parent / candidate).resolve()
        else:
            resolved = (dataset / candidate).resolve()
        relative = resolved.relative_to(dataset)
        return resolved, str(relative)

    @classmethod
    def restore(
        cls,
        dataset_dir: str | os.PathLike[str],
        run_id: str,
    ) -> dict[str, Any]:
        dataset = Path(dataset_dir).expanduser().resolve()
        if not cls.RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid quarantine run id")
        active_runs = cls._read_active_runs(dataset)
        if not active_runs:
            raise RuntimeError("no active applied filter run")
        active = active_runs[-1]
        if active["run_id"] != run_id:
            raise RuntimeError(f"only the latest run can be restored: {active['run_id']}")
        run_dir = (dataset / "_quarantine" / run_id).resolve()
        if run_dir.parent != (dataset / "_quarantine").resolve():
            raise ValueError("invalid quarantine run id")
        quarantine_records = _read_jsonl(run_dir / "quarantine_manifest.jsonl")
        moves: list[tuple[Path, Path]] = []
        for record in quarantine_records:
            source = (run_dir / str(record["quarantine_path"])).resolve()
            destination = (dataset / str(record["original_path"])).resolve()
            try:
                source.relative_to(run_dir)
                destination.relative_to(dataset)
            except ValueError as exc:
                raise ValueError("quarantine manifest contains an unsafe path") from exc
            if destination.exists():
                raise FileExistsError(f"restore destination exists: {destination}")
            if not source.exists():
                raise FileNotFoundError(f"quarantined file missing: {source}")
            moves.append((source, destination))
        restored_moves: list[tuple[Path, Path]] = []
        try:
            for source, destination in moves:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                restored_moves.append((source, destination))
        except Exception:
            for source, destination in reversed(restored_moves):
                if destination.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, source)
            raise
        for name in ("metadata.jsonl", "manifest.jsonl"):
            backup = run_dir / "backups" / name
            if backup.exists():
                shutil.copy2(backup, dataset / name)
        cls._write_active_runs(dataset, active_runs[:-1])
        result = {
            "run_id": run_id,
            "dataset": str(dataset),
            "restored": len(restored_moves),
            "restored_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_json(run_dir / "restore_report.json", result)
        return result
