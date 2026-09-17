# coding=utf-8
"""Engine HTML/JSON fixture regression: parsers must keep extracting from frozen pages."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_spider.engines import get_engine

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "engines"
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", MANIFEST, ids=[item["file"] for item in MANIFEST])
def test_engine_fixture_extracts_expected_items(row):
    engine = get_engine(row["engine"])
    payload = (FIXTURE_DIR / row["file"]).read_text(encoding="utf-8")
    items = engine.extract_items(payload)
    assert len(items) >= int(row["min_items"])
    for item in items:
        assert item.get("url")
