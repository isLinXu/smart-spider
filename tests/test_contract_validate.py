# coding=utf-8
"""契约 validate / format_version 测试（S4）。"""
from __future__ import annotations

import pytest

from smart_spider.dataset_contracts import (
    CONTRACT_FORMAT_VERSION,
    CandidateResource,
    ContractValidationError,
    Modality,
    SampleRecord,
)


def test_candidate_requires_url_and_source():
    with pytest.raises(ContractValidationError):
        CandidateResource(url="", source="x").validate()
    with pytest.raises(ContractValidationError):
        CandidateResource(url="https://example.com/a.jpg", source="").validate()


def test_candidate_roundtrip_adds_format_version():
    item = CandidateResource(url="https://Example.com/A.jpg#frag", source="bing")
    item.validate()
    payload = item.to_dict()
    assert payload["format_version"] == CONTRACT_FORMAT_VERSION
    restored = CandidateResource.from_dict(payload)
    assert restored.url == "https://example.com/A.jpg"


def test_sample_requires_identity_and_asset():
    with pytest.raises(ContractValidationError):
        SampleRecord(sample_id="").validate()
    with pytest.raises(ContractValidationError):
        SampleRecord(sample_id="s1").validate()
    SampleRecord(sample_id="s1", file="batch_0000/0000_abcd.jpg").validate()


def test_sample_rejects_unsupported_format_version():
    with pytest.raises(ContractValidationError):
        SampleRecord.from_dict(
            {
                "format_version": 999,
                "id": "s1",
                "file": "a.jpg",
                "modalities": [],
            }
        )


def test_sample_accepts_image_modality_only():
    record = SampleRecord.from_dict(
        {
            "id": "s1",
            "modalities": [
                {"modality": "image", "role": "image", "uri": "assets/x.jpg"}
            ],
            "provenance": {"source": "test"},
            "pipeline": {},
        }
    )
    assert record.modalities[0].modality == Modality.IMAGE
