# coding=utf-8
"""Versioned object-detection annotations and append-only review decisions."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


ANNOTATION_FORMAT_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVIEW_STATES = {"pending", "approved", "rejected"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ontology_fingerprint(
    ontology_id: str,
    ontology_version: str,
    class_names: Sequence[str],
    class_definitions: Sequence[str],
) -> str:
    if len(class_names) != len(class_definitions):
        raise ValueError("class definitions must match class_names")
    material = {
        "ontology_id": ontology_id,
        "ontology_version": ontology_version,
        "classes": [
            {"id": index, "name": name, "definition": class_definitions[index]}
            for index, name in enumerate(class_names)
        ],
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BoundingBox:
    """A YOLO-compatible normalized center-x, center-y, width, height box."""

    class_id: int
    x_center: float
    y_center: float
    width: float
    height: float
    attributes: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.attributes is not None and not isinstance(self.attributes, Mapping):
            raise ValueError("bounding box attributes must be an object")
        object.__setattr__(self, "attributes", dict(self.attributes or {}))

    def validate(self, class_count: int) -> None:
        if isinstance(self.class_id, bool) or not isinstance(self.class_id, int):
            raise ValueError("bounding box class_id must be an integer")
        if not 0 <= self.class_id < class_count:
            raise ValueError(f"bounding box class_id {self.class_id} is outside the ontology")
        values = (self.x_center, self.y_center, self.width, self.height)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("bounding box coordinates must be finite numbers")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("bounding box width and height must be positive")
        if not (0.0 <= self.x_center <= 1.0 and 0.0 <= self.y_center <= 1.0):
            raise ValueError("bounding box center must be normalized to [0, 1]")
        if (
            self.x_center - self.width / 2 < -1e-9
            or self.x_center + self.width / 2 > 1.0 + 1e-9
            or self.y_center - self.height / 2 < -1e-9
            or self.y_center + self.height / 2 > 1.0 + 1e-9
        ):
            raise ValueError("bounding box extends outside the image")
        try:
            json.dumps(dict(self.attributes or {}), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("bounding box attributes must contain finite JSON values") from exc

    def to_dict(self, class_names: Sequence[str] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "class_id": self.class_id,
            "bbox": {
                "coordinate_system": "normalized_xywh_center",
                "x_center": self.x_center,
                "y_center": self.y_center,
                "width": self.width,
                "height": self.height,
            },
            "attributes": dict(self.attributes or {}),
        }
        if class_names is not None:
            result["category"] = class_names[self.class_id]
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BoundingBox":
        coordinates = raw.get("bbox") or raw
        if not isinstance(coordinates, Mapping):
            raise ValueError("annotation bbox must be an object")
        coordinate_system = coordinates.get("coordinate_system", "normalized_xywh_center")
        if coordinate_system != "normalized_xywh_center":
            raise ValueError(f"unsupported bbox coordinate system: {coordinate_system}")
        try:
            return cls(
                class_id=raw.get("class_id", raw.get("category_id")),
                x_center=float(coordinates["x_center"]),
                y_center=float(coordinates["y_center"]),
                width=float(coordinates["width"]),
                height=float(coordinates["height"]),
                attributes=dict(raw.get("attributes") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid bounding box record") from exc


@dataclass(frozen=True)
class AnnotationRecord:
    """Annotations for one immutable image asset; approval is explicit."""

    sample_id: str
    asset_sha256: str
    image_width: int
    image_height: int
    ontology_id: str
    ontology_version: str
    class_names: tuple[str, ...]
    ontology_fingerprint: str
    class_definitions: tuple[str, ...] = ()
    annotations: tuple[BoundingBox, ...] = ()
    review_status: str = "pending"
    revision: int = 0
    audit_log: tuple[Mapping[str, Any], ...] = ()
    format_version: int = ANNOTATION_FORMAT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "class_names", tuple(self.class_names))
        object.__setattr__(self, "class_definitions", tuple(self.class_definitions))
        object.__setattr__(self, "annotations", tuple(self.annotations))
        object.__setattr__(self, "audit_log", tuple(dict(event) for event in self.audit_log))
        self.validate()

    @property
    def annotation_approved(self) -> bool:
        """Whether a human explicitly approved these boxes (not a release gate)."""
        return self.review_status == "approved"

    def validate(self) -> None:
        if (
            isinstance(self.format_version, bool)
            or not isinstance(self.format_version, int)
            or self.format_version != ANNOTATION_FORMAT_VERSION
        ):
            raise ValueError(f"unsupported annotation format_version={self.format_version}")
        if not isinstance(self.sample_id, str) or not self.sample_id.strip():
            raise ValueError("annotation sample_id is required")
        if not isinstance(self.asset_sha256, str) or not _SHA256_RE.fullmatch(self.asset_sha256):
            raise ValueError("asset_sha256 must be a lowercase SHA-256 digest")
        if (
            isinstance(self.image_width, bool)
            or isinstance(self.image_height, bool)
            or not isinstance(self.image_width, int)
            or not isinstance(self.image_height, int)
            or self.image_width <= 0
            or self.image_height <= 0
        ):
            raise ValueError("annotation image dimensions must be positive integers")
        if (
            not isinstance(self.ontology_id, str)
            or not isinstance(self.ontology_version, str)
            or not self.ontology_id.strip()
            or not self.ontology_version.strip()
        ):
            raise ValueError("annotation ontology id and version are required")
        if (
            not isinstance(self.ontology_fingerprint, str)
            or not _SHA256_RE.fullmatch(self.ontology_fingerprint)
        ):
            raise ValueError("ontology_fingerprint must be a lowercase SHA-256 digest")
        if isinstance(self.class_names, str):
            raise ValueError("annotation class_names must be a sequence of names")
        if not self.class_names or any(not isinstance(name, str) or not name.strip() for name in self.class_names):
            raise ValueError("annotation class_names must be non-empty strings")
        if len(self.class_definitions) != len(self.class_names) or any(
            not isinstance(definition, str) for definition in self.class_definitions
        ):
            raise ValueError("annotation class definitions must match class_names")
        if len(set(self.class_names)) != len(self.class_names):
            raise ValueError("annotation class_names must be unique")
        if ontology_fingerprint(
            self.ontology_id,
            self.ontology_version,
            self.class_names,
            self.class_definitions,
        ) != self.ontology_fingerprint:
            raise ValueError("ontology fingerprint does not match class definitions")
        if self.review_status not in _REVIEW_STATES:
            raise ValueError(f"unsupported review_status={self.review_status}")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("annotation revision must be a non-negative integer")
        for annotation in self.annotations:
            annotation.validate(len(self.class_names))
        for event in self.audit_log:
            if not isinstance(event, Mapping):
                raise ValueError("annotation audit events must be objects")
        if self.review_status in {"approved", "rejected"}:
            if len(self.audit_log) != 1 or self.revision != 1:
                raise ValueError("final review status requires exactly one fenced audit event")
            event = self.audit_log[-1]
            expected_decision = "approve" if self.review_status == "approved" else "reject"
            if (
                event.get("decision") != expected_decision
                or not str(event.get("reviewer") or "").strip()
                or not str(event.get("decided_at") or "").strip()
                or event.get("asset_sha256") != self.asset_sha256
                or event.get("ontology_fingerprint") != self.ontology_fingerprint
                or event.get("annotation_sha256") != self.annotation_digest()
                or event.get("revision") != self.revision
            ):
                raise ValueError("annotation content changed or audit event does not match review status")

    def annotation_digest(self) -> str:
        payload = {
            "ontology_fingerprint": self.ontology_fingerprint,
            "annotations": [item.to_dict(self.class_names) for item in self.annotations],
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "format_version": self.format_version,
            "sample_id": self.sample_id,
            "asset_sha256": self.asset_sha256,
            "image": {"width": self.image_width, "height": self.image_height},
            "ontology": {
                "id": self.ontology_id,
                "version": self.ontology_version,
                "fingerprint": self.ontology_fingerprint,
                "classes": [
                    {"id": index, "name": name, "definition": self.class_definitions[index]}
                    for index, name in enumerate(self.class_names)
                ],
            },
            "annotations": [item.to_dict(self.class_names) for item in self.annotations],
            "review": {
                "status": self.review_status,
                "revision": self.revision,
                "audit_log": [dict(event) for event in self.audit_log],
            },
            "annotation_sha256": self.annotation_digest(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AnnotationRecord":
        image = raw.get("image") or {}
        ontology = raw.get("ontology") or {}
        review = raw.get("review") or {}
        if not all(isinstance(value, Mapping) for value in (image, ontology, review)):
            raise ValueError("annotation image, ontology, and review fields must be objects")
        class_names = []
        class_definitions = []
        for item in ontology.get("classes") or ():
            if isinstance(item, str):
                class_names.append(item)
                class_definitions.append("")
            elif isinstance(item, Mapping):
                class_names.append(str(item.get("name") or ""))
                class_definitions.append(str(item.get("definition") or ""))
            else:
                raise ValueError("annotation ontology class must be an object or string")
        record = cls(
            sample_id=str(raw.get("sample_id") or ""),
            asset_sha256=str(raw.get("asset_sha256") or ""),
            image_width=image.get("width", 0),
            image_height=image.get("height", 0),
            ontology_id=str(ontology.get("id") or ""),
            ontology_version=str(ontology.get("version") or ""),
            class_names=tuple(class_names),
            ontology_fingerprint=str(ontology.get("fingerprint") or ""),
            class_definitions=tuple(class_definitions),
            annotations=tuple(BoundingBox.from_dict(item) for item in raw.get("annotations") or ()),
            review_status=str(review.get("status") or "pending"),
            revision=review.get("revision", 0),
            audit_log=tuple(review.get("audit_log") or ()),
            format_version=int(raw.get("format_version", ANNOTATION_FORMAT_VERSION)),
        )
        saved_digest = raw.get("annotation_sha256")
        if saved_digest is not None and saved_digest != record.annotation_digest():
            raise ValueError("annotation digest does not match annotation content")
        return record


@dataclass(frozen=True)
class ReviewDecision:
    sample_id: str
    asset_sha256: str
    expected_revision: int
    decision: str
    reviewer: str
    reason: str = ""
    decided_at: str = ""

    def validate(self) -> None:
        if (
            not isinstance(self.sample_id, str)
            or not self.sample_id.strip()
            or not isinstance(self.asset_sha256, str)
            or not _SHA256_RE.fullmatch(self.asset_sha256)
        ):
            raise ValueError("review decision requires sample_id and asset SHA-256")
        if (
            isinstance(self.expected_revision, bool)
            or not isinstance(self.expected_revision, int)
            or self.expected_revision < 0
        ):
            raise ValueError("review expected_revision must be non-negative")
        if self.decision not in {"approve", "reject"}:
            raise ValueError("review decision must be approve or reject")
        if not isinstance(self.reviewer, str) or not self.reviewer.strip():
            raise ValueError("reviewer is required")
        if not isinstance(self.decided_at, str):
            raise ValueError("decided_at must be an ISO-8601 string")
        if self.decided_at:
            try:
                datetime.fromisoformat(self.decided_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("decided_at must be an ISO-8601 timestamp") from exc
        if not isinstance(self.reason, str):
            raise ValueError("review reason must be a string")
        if self.decision == "reject" and not self.reason.strip():
            raise ValueError("a rejection requires a reason")


def apply_review_decisions(
    records: Sequence[AnnotationRecord],
    decisions: Sequence[ReviewDecision],
) -> list[AnnotationRecord]:
    """Apply a batch atomically in memory, fencing stale or changed-image reviews."""
    by_id = {record.sample_id: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("annotation records contain duplicate sample IDs")
    by_decision: dict[str, ReviewDecision] = {}
    for decision in decisions:
        decision.validate()
        if decision.sample_id in by_decision:
            raise ValueError(f"duplicate review decision for {decision.sample_id}")
        current = by_id.get(decision.sample_id)
        if current is None:
            raise ValueError(f"review decision references unknown sample {decision.sample_id}")
        if current.asset_sha256 != decision.asset_sha256:
            raise ValueError(f"review image hash changed for {decision.sample_id}")
        if current.revision != decision.expected_revision:
            raise ValueError(f"stale review revision for {decision.sample_id}")
        if current.review_status != "pending":
            raise ValueError(f"sample {decision.sample_id} is already {current.review_status}")
        by_decision[decision.sample_id] = decision

    updated: list[AnnotationRecord] = []
    for record in records:
        decision = by_decision.get(record.sample_id)
        if decision is None:
            updated.append(record)
            continue
        event = {
            "decision": decision.decision,
            "reviewer": decision.reviewer.strip(),
            "reason": decision.reason.strip(),
            "decided_at": decision.decided_at or utc_now(),
            "asset_sha256": record.asset_sha256,
            "ontology_fingerprint": record.ontology_fingerprint,
            "annotation_sha256": record.annotation_digest(),
            "revision": record.revision + 1,
        }
        updated.append(replace(
            record,
            review_status="approved" if decision.decision == "approve" else "rejected",
            revision=record.revision + 1,
            audit_log=(*record.audit_log, event),
        ))
    return updated
