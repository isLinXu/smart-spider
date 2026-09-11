# coding=utf-8
"""SiteProfile 与 browse CLI 测试（S17）。"""
from __future__ import annotations

import json

import pytest

from smart_spider.browse_cli import enqueue_profile_jobs, main as browse_main, run_profile_once
from smart_spider.pipeline import LocalSqliteTaskQueue
from smart_spider.site_profile import SiteProfile, load_site_profile


def test_site_profile_defaults_allow_hosts_from_seeds():
    profile = SiteProfile.from_mapping(
        {
            "name": "demo",
            "seed_urls": ["https://example.com/a", "https://www.example.com/b"],
            "max_depth": 1,
        }
    )
    assert "example.com" in profile.policy.allow_hosts
    assert profile.validate_seeds() == [
        "https://example.com/a",
        "https://www.example.com/b",
    ]


def test_site_profile_rejects_out_of_policy_urls():
    profile = SiteProfile.from_mapping(
        {
            "name": "demo",
            "seed_urls": ["https://example.com/"],
            "allow_hosts": ["example.com"],
        }
    )
    with pytest.raises(ValueError, match="denied"):
        profile.validate_seeds(["https://evil.com/x"])


def test_load_site_profile_yaml(tmp_path):
    path = tmp_path / "site.yaml"
    path.write_text(
        "\n".join(
            [
                "name: demo",
                "seed_urls:",
                "  - https://example.com/",
                "allow_hosts:",
                "  - example.com",
                "license: CC0-1.0",
                "max_attempts: 2",
            ]
        ),
        encoding="utf-8",
    )
    profile = load_site_profile(str(path), overrides={"enqueue_links": False})
    assert profile.name == "demo"
    assert profile.license == "CC0-1.0"
    assert profile.enqueue_links is False
    assert profile.max_attempts == 2
    payload = profile.browse_payload("https://example.com/")
    assert payload["profile"] == "demo"
    assert payload["policy"]["allow_hosts"] == ["example.com"]


def test_enqueue_profile_jobs(tmp_path):
    queue = LocalSqliteTaskQueue(str(tmp_path / "q.sqlite3"))
    profile = SiteProfile.from_mapping(
        {
            "name": "demo",
            "seed_urls": ["https://example.com/"],
            "allow_hosts": ["example.com"],
            "max_attempts": 2,
        }
    )
    ids = enqueue_profile_jobs(profile, profile.seed_urls, queue=queue)
    assert len(ids) == 1
    record = queue.get(ids[0])
    assert record.kind == "authorized_browse"
    assert record.payload["url"] == "https://example.com/"
    assert record.max_attempts == 2


def test_run_profile_once_with_fake_controller():
    profile = SiteProfile.from_mapping(
        {
            "name": "demo",
            "seed_urls": ["https://example.com/"],
            "allow_hosts": ["example.com"],
            "action_delay_seconds": 0,
            "scroll_passes": 0,
            "respect_robots": False,
            "requests_per_second": 1000,
        }
    )

    class FakeController:
        current_url = "https://example.com/"

        def navigate(self, url):
            self.current_url = url
            return '<html><body><a href="/next">n</a><p>ok page content here</p></body></html>'

        def scroll(self):
            return True, self.navigate(self.current_url)

        def close(self):
            return None

    results = run_profile_once(profile, list(profile.seed_urls), controller=FakeController())
    assert len(results) == 1
    assert results[0]["profile"] == "demo"
    assert results[0]["block"]["kind"] == "ok"


def test_browse_cli_dry_run_and_reject(tmp_path, capsys):
    path = tmp_path / "site.json"
    path.write_text(
        json.dumps(
            {
                "name": "demo",
                "seed_urls": ["https://example.com/"],
                "allow_hosts": ["example.com"],
            }
        ),
        encoding="utf-8",
    )
    assert browse_main(["--profile", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run"
    assert out["urls"] == ["https://example.com/"]

    with pytest.raises(SystemExit):
        browse_main(
            ["--profile", str(path), "--url", "https://evil.com/"]
        )


def test_browse_cli_enqueue(tmp_path, capsys):
    path = tmp_path / "site.json"
    path.write_text(
        json.dumps(
            {
                "name": "demo",
                "seed_urls": ["https://example.com/"],
                "allow_hosts": ["example.com"],
            }
        ),
        encoding="utf-8",
    )
    queue_path = str(tmp_path / "q.sqlite3")
    assert (
        browse_main(
            ["--profile", str(path), "--enqueue", "--queue", queue_path]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "enqueue"
    assert len(out["task_ids"]) == 1
    queue = LocalSqliteTaskQueue(queue_path)
    assert queue.get(out["task_ids"][0]).kind == "authorized_browse"
