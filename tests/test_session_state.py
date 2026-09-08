# coding=utf-8
"""会话状态读写测试。"""
from __future__ import annotations

import json

import pytest

from smart_spider.session_state import (
    load_cookies,
    load_storage_state,
    resolve_session_inputs,
    write_json_file,
)
from smart_spider.site_policy import SiteCrawlPolicy


def test_load_cookies_and_storage_state(tmp_path):
    cookies_path = tmp_path / "c.json"
    cookies_path.write_text(
        json.dumps({"cookies": [{"name": "a", "value": "1", "domain": "example.com"}]}),
        encoding="utf-8",
    )
    state_path = tmp_path / "s.json"
    write_json_file(
        state_path,
        {
            "cookies": [{"name": "sid", "value": "x", "domain": ".example.com"}],
            "origins": [],
        },
    )
    assert load_cookies(cookies_path)[0]["name"] == "a"
    assert load_storage_state(state_path)["cookies"][0]["name"] == "sid"

    state, cookies = resolve_session_inputs(
        storage_state_path=str(state_path),
        cookies_path=str(cookies_path),
    )
    assert state is not None
    names = {item["name"] for item in cookies}
    assert names == {"sid", "a"}


def test_site_policy_resolve_session(tmp_path):
    state_path = tmp_path / "state.json"
    write_json_file(
        state_path,
        {"cookies": [{"name": "t", "value": "1", "domain": "example.com"}], "origins": []},
    )
    policy = SiteCrawlPolicy.from_mapping(
        {"storage_state_path": str(state_path), "allow_hosts": ["example.com"]}
    )
    state, cookies = policy.resolve_session()
    assert state["cookies"][0]["name"] == "t"
    assert policy.load_cookies()[0]["name"] == "t"


def test_invalid_storage_state_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_storage_state(bad)
