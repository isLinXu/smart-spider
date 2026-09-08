# coding=utf-8
"""合规 URL 脱敏与发布清单测试。"""
from __future__ import annotations

import json
import os

from smart_spider.compliance import (
    CompliancePolicy,
    apply_compliance_to_provenance,
    build_publish_checklist,
    redact_url,
    write_publish_checklist,
)


def test_redact_url_strips_secrets_and_fragment():
    url = "https://example.com/a?token=secret&q=cat#frag"
    assert redact_url(url) == "https://example.com/a?q=cat"


def test_apply_compliance_and_checklist(tmp_path):
    policy = CompliancePolicy(
        license="CC-BY-4.0",
        source_terms="example.com ToS",
        respect_robots=True,
        allow_hosts=("example.com",),
    )
    provenance = {"url": "https://example.com/x?api_key=abc&keep=1"}
    apply_compliance_to_provenance(provenance, policy)
    assert provenance["license"] == "CC-BY-4.0"
    assert provenance["url"] == "https://example.com/x?keep=1"
    assert provenance["url_redacted"] is True

    checklist = build_publish_checklist(
        job_id="j1",
        policy=policy,
        route_counts={"static": 1, "browser": 1},
        route_reasons={"blocked_or_rate_limited:forbidden": 1},
        block_kinds={"forbidden": 1},
    )
    assert checklist.ready is True
    path = write_publish_checklist(str(tmp_path), checklist)
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["license"] == "CC-BY-4.0"
    assert payload["ready"] is True


def test_checklist_warns_when_license_missing():
    checklist = build_publish_checklist(
        job_id="j2",
        policy=CompliancePolicy(respect_robots=False),
        block_kinds={"challenge": 2},
    )
    assert checklist.ready is False
    assert "license_missing" in checklist.warnings
    assert "robots_disabled" in checklist.warnings
    assert "challenge_pages_observed" in checklist.warnings
