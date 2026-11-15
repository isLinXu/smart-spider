"""Offline, advisory visual triage of the six industrial collection classes.

Only verified local manifest assets are encoded. No image is downloaded, moved,
deleted, relabeled, or promoted to a training sample by this module.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import importlib.metadata
import json
import math
import os
from pathlib import Path
from urllib.parse import quote

from .image_safety import decode_image_bytes

CLASS_NAMES = ("person", "forklift", "truck_rear", "head", "phone", "helmet")
MODEL_ID = "openai/CLIP-ViT-B/32"
WEIGHT_SHA256 = "40d365715913c9da98579312b702a82c18be219cc2a73407c4526f58eba950af"
MODEL_SOURCE = "https://github.com/openai/CLIP"
WEIGHT_SOURCE = f"https://openaipublic.azureedge.net/clip/models/{WEIGHT_SHA256}/ViT-B-32.pt"


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_profile(path: Path) -> dict:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("threshold_status") != "provisional_uncalibrated":
        raise ValueError("this pilot only supports uncalibrated, advisory thresholds")
    groups = profile["groups"]
    required = {f"{name}.{kind}" for name in CLASS_NAMES for kind in ("positive", "negative")}
    required.update(("scene", "photo", "style_negative"))
    if set(groups) != required:
        raise ValueError("profile must contain positive/negative prompts for all six classes and scene/style prompts")
    if any(not isinstance(prompts, list) or not prompts or any(
        not isinstance(prompt, str) or not prompt.strip() for prompt in prompts
    ) for prompts in groups.values()):
        raise ValueError("prompt groups must be non-empty lists of strings")
    thresholds = profile["thresholds"]
    expected = {"priority_similarity", "class_margin", "scene_similarity", "style_margin", "low_similarity", "negative_margin"}
    if set(thresholds) != expected or any(
        not isinstance(v, (int, float)) or not math.isfinite(v) or not -1 <= v <= 1
        for v in thresholds.values()
    ):
        raise ValueError("invalid triage thresholds")
    if thresholds["low_similarity"] > thresholds["priority_similarity"] or thresholds["negative_margin"] < 0:
        raise ValueError("inconsistent triage thresholds")
    return profile


def load_candidates(roots: list[Path], *, max_bytes: int = 12 * 1024 * 1024) -> tuple[list[dict], list[dict]]:
    """Merge manifests by actual content digest; untrusted paths cannot escape a class root."""
    grouped: dict[str, dict] = {}
    invalid = []
    for root in dict.fromkeys(path.resolve() for path in roots):
        if not root.is_dir():
            raise ValueError(f"input root does not exist: {root}")
        for name in CLASS_NAMES:
            class_root = (root / name).resolve()
            class_root.relative_to(root)
            manifest = class_root / "manifest.jsonl"
            if not manifest.exists():
                continue
            with manifest.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        raw_path = record["file"]
                        if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
                            raise ValueError("expected a relative local asset path")
                        path = (class_root / raw_path).resolve()
                        path.relative_to(class_root)
                        with path.open("rb") as source:
                            content = source.read(max_bytes + 1)
                        if len(content) > max_bytes:
                            raise ValueError("asset exceeds byte limit")
                        identifier = "sha256:" + hashlib.sha256(content).hexdigest()
                        if record["id"] != identifier:
                            raise ValueError("asset content hash does not match manifest ID")
                        row = grouped.setdefault(identifier, {
                            "sample_id": identifier, "path": str(path),
                            "retrieval_classes": [], "sources": [],
                        })
                        if name not in row["retrieval_classes"]:
                            row["retrieval_classes"].append(name)
                        source = {"file": str(path), "manifest": str(manifest),
                                  "retrieval_class": name, "provenance": record.get("provenance", {})}
                        if source not in row["sources"]:
                            row["sources"].append(source)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        invalid.append({"manifest": str(manifest), "line": number,
                                        "reason": str(exc), "status": "invalid_asset", "training_ready": False})
    return list(grouped.values()), invalid


class LocalCLIPScorer:
    """One encoder, one image embedding, all class and distractor prompts."""

    def __init__(self, weights: Path, profile: dict, *, device: str = "cpu", threads: int = 4):
        # Verify pinned bytes before importing optional model code. Passing a local
        # filename to CLIPInference prevents clip.load from downloading weights.
        weights = weights.expanduser().resolve(strict=True)
        digest = file_sha256(weights)
        if digest != WEIGHT_SHA256:
            raise ValueError("weights do not match the pinned OpenAI CLIP ViT-B/32 revision")
        import torch
        import clip
        from .perception.clip_inference import CLIPInference

        if threads <= 0:
            raise ValueError("threads must be positive")
        torch.set_num_threads(threads)
        self.encoder = CLIPInference(model_name=str(weights), device=device)
        self.groups = profile["groups"]
        self.slices = {}
        vectors = []
        offset = 0
        for name, prompts in self.groups.items():
            self.slices[name] = (offset, offset + len(prompts))
            vectors.extend(self.encoder.encode_text(prompt) for prompt in prompts)
            offset += len(prompts)
        self.text_features = torch.cat(vectors, dim=0)
        package = importlib.metadata.distribution("clip")
        direct = json.loads(package.read_text("direct_url.json") or "{}")
        # Record immutable model and code evidence, never credentials from a package URL.
        code_hashes = {filename: hashlib.sha256((Path(clip.__file__).parent / filename).read_bytes()).hexdigest()
                       for filename in ("clip.py", "model.py", "simple_tokenizer.py")}
        self.metadata = {
            "model_id": MODEL_ID, "revision": f"sha256:{digest}", "source": MODEL_SOURCE,
            "weights_source": WEIGHT_SOURCE, "license": "MIT",
            "license_source": MODEL_SOURCE + "/blob/main/LICENSE",
            "package_version": package.version,
            "code_revision": direct.get("vcs_info", {}).get("commit_id"),
            "code_file_sha256": code_hashes,
            "device": device, "evidence_modality": "local_image_bytes",
            "score_type": "cosine_similarity", "advisory": True,
        }

    def score_images(self, images: list) -> list[dict]:
        import numpy as np
        image_features = self.encoder.encode_images(images)
        scores = (image_features @ self.text_features.T).detach().float().cpu().numpy()
        if scores.shape != (len(images), self.text_features.shape[0]) or not np.isfinite(scores).all():
            raise RuntimeError("invalid model scores; refusing to classify this batch")
        results = []
        for row in scores:
            grouped = {}
            for name, (start, end) in self.slices.items():
                values = row[start:end]
                index = int(values.argmax())
                grouped[name] = {"score": float(values[index]), "prompt": self.groups[name][index]}
            results.append(grouped)
        return results


def triage(groups: dict, name: str, thresholds: dict) -> dict:
    required = (f"{name}.positive", f"{name}.negative", "scene", "photo", "style_negative")
    values = {key: float(groups[key]["score"]) for key in required}
    if any(not math.isfinite(value) or not -1.0001 <= value <= 1.0001 for value in values.values()):
        raise ValueError("non-finite or out-of-range similarity")
    positive, negative = values[required[0]], values[required[1]]
    class_margin = positive - negative
    style_margin = values["photo"] - values["style_negative"]
    reasons = []
    if positive < thresholds["priority_similarity"]:
        reasons.append("weak_target_evidence")
    if class_margin < thresholds["class_margin"]:
        reasons.append("confusable_object_or_background")
    if values["scene"] < thresholds["scene_similarity"]:
        reasons.append("weak_industrial_scene_evidence")
    if style_margin < thresholds["style_margin"]:
        reasons.append("illustration_advertisement_or_product_risk")
    low = positive < thresholds["low_similarity"] or class_margin < -thresholds["negative_margin"] or style_margin < -thresholds["negative_margin"]
    status = "lower_priority" if low else "needs_review" if reasons else "priority_review"
    return {"status": status, "score": positive, "class_margin": class_margin,
            "style_margin": style_margin, "reasons": reasons,
            "evidence": {key: groups[key] for key in required}, "advisory": True,
            "training_ready": False}


def screen_candidates(candidates: list[dict], scorer, profile: dict, *, batch_size: int = 8) -> tuple[list[dict], list[dict]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    output, invalid = [], []
    for start in range(0, len(candidates), batch_size):
        images, rows = [], []
        try:
            for candidate in candidates[start:start + batch_size]:
                try:
                    with Path(candidate["path"]).open("rb") as handle:
                        content = handle.read(12 * 1024 * 1024 + 1)
                    if len(content) > 12 * 1024 * 1024 or "sha256:" + hashlib.sha256(content).hexdigest() != candidate["sample_id"]:
                        raise ValueError("asset changed after manifest validation")
                    image = decode_image_bytes(content, max_pixels=50_000_000)
                    images.append(image)
                    rows.append(candidate)
                except (OSError, ValueError) as exc:
                    invalid.append({"sample_id": candidate["sample_id"], "file": candidate["path"],
                                    "reason": str(exc), "status": "invalid_asset", "training_ready": False})
            if not images:
                continue
            scores = scorer.score_images(images)
            if len(scores) != len(rows):
                raise RuntimeError("model returned an incomplete batch")
            for candidate, groups in zip(rows, scores):
                output.append({**candidate, "status": "needs_review", "training_ready": False,
                               "model": dict(scorer.metadata), "profile_fingerprint": fingerprint(profile),
                               "threshold_status": profile["threshold_status"],
                               "class_evidence": {name: triage(groups, name, profile["thresholds"]) for name in CLASS_NAMES}})
        finally:
            for image in images:
                image.close()
        print(f"screened {min(start + batch_size, len(candidates))}/{len(candidates)} unique images", flush=True)
    return output, invalid


def write_gallery(path: Path, records: list[dict], *, limit: int = 30) -> None:
    sections = []
    for name in CLASS_NAMES:
        ordered = sorted(records, key=lambda row: (
            {"priority_review": 0, "needs_review": 1, "lower_priority": 2}[row["class_evidence"][name]["status"]],
            -row["class_evidence"][name]["class_margin"], row["sample_id"]))[:limit]
        cards = []
        for row in ordered:
            evidence = row["class_evidence"][name]
            relative = quote(os.path.relpath(row["path"], path.parent), safe="/")
            caption = html.escape(f"{evidence['status']} · similarity {evidence['score']:.3f} · margin {evidence['class_margin']:.3f}")
            cards.append(f'<figure><a href="{relative}"><img loading="lazy" src="{relative}"></a><figcaption>{caption}<br>{html.escape(row["sample_id"])}<br>{html.escape(", ".join(evidence["reasons"]))}</figcaption></figure>')
        sections.append(f'<h2 id="{name}">{name}</h2><div class="grid">{"".join(cards)}</div>')
    path.write_text('<!doctype html><meta charset="utf-8"><title>Industrial image review</title>'
                    '<style>body{font:15px sans-serif;margin:24px;background:#fafafa}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:12px}figure{margin:0;background:white;padding:10px;border:1px solid #ddd}img{width:100%;height:220px;object-fit:contain}figcaption{overflow-wrap:anywhere;font-size:12px}</style>'
                    '<h1>工业图像复核队列</h1><p>仅本地图像；分数为未校准的模型相似度。优先复核不代表标签正确。尚未审核授权或检测框。</p>'
                    + ' · '.join(f'<a href="#{name}">{name}</a>' for name in CLASS_NAMES) + ''.join(sections), encoding="utf-8")


def write_results(output: Path, records: list[dict], invalid: list[dict], profile: dict, metadata: dict) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    summary = {"training_ready": False, "unique_images_scored": len(records), "invalid_assets": len(invalid),
               "model": metadata, "profile_fingerprint": fingerprint(profile), "threshold_status": profile["threshold_status"],
               "triage_by_retrieval_class": {}, "all_image_review_priorities": {}}
    for name in CLASS_NAMES:
        retrieved = [row for row in records if name in row["retrieval_classes"]]
        summary["triage_by_retrieval_class"][name] = dict(Counter(row["class_evidence"][name]["status"] for row in retrieved))
        summary["all_image_review_priorities"][name] = dict(Counter(row["class_evidence"][name]["status"] for row in records))
    for filename, rows in (("visual_review.jsonl", records), ("invalid_assets.jsonl", invalid)):
        temporary = output / (filename + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        temporary.replace(output / filename)
    (output / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_gallery(output / "review.html", records)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True, help="existing pinned ViT-B-32.pt; no model download")
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parents[1] / "examples/industrial_visual_profile.json")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args(argv)
    if args.batch_size <= 0 or args.threads <= 0:
        parser.error("batch size and threads must be positive")
    profile = load_profile(args.profile)
    candidates, invalid = load_candidates(args.input_roots)
    if not candidates:
        parser.error(f"no valid local candidates ({len(invalid)} invalid records)")
    scorer = LocalCLIPScorer(args.weights, profile, device=args.device, threads=args.threads)
    records, decode_errors = screen_candidates(candidates, scorer, profile, batch_size=args.batch_size)
    summary = write_results(args.output.resolve(), records, invalid + decode_errors, profile, scorer.metadata)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not invalid and not decode_errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
