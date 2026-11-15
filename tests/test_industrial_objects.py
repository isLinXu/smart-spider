"""Candidate acquisition must not imply reviewed detection labels."""
import hashlib
import json
from pathlib import Path

import pytest

import crawl_industrial_objects as batch
from smart_spider.dataset_config import DatasetCrawlConfig
from smart_spider.dataset_contracts import LabelDecision, SampleRecord


def add_sample(root, name, content=b"local licensed test fixture", filename="batch_0000/image.jpg"):
    directory = root / name
    image = directory / filename
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(content)
    identifier = "sha256:" + hashlib.sha256(content).hexdigest()
    sample = SampleRecord(identifier, file=filename, provenance={"source": "test", "query": name})
    with (directory / "manifest.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(sample.to_json() + "\n")
    return image


def test_all_six_configs_validate_and_do_not_promote_search_labels(tmp_path):
    spec = batch.load_spec(batch.ROOT / "examples/industrial_objects.json")
    assert [row["name"] for row in spec["classes"]] == list(batch.CLASS_NAMES)
    for row in spec["classes"]:
        config = DatasetCrawlConfig.from_mapping(batch.build_config(row, tmp_path, 5000, 2))
        assert config.total_count == 5000
        assert config.resume and not config.use_clip
        assert not config.allow_private_hosts
        assert not config.license and not config.source_terms
        for query in config.keywords:
            decision = config.label_policy.resolve([LabelDecision(query, 1, "query")])
            assert decision.labels == []
            assert [item.name for item in decision.candidates] == [row["name"]]


def test_inventory_merges_cross_class_candidates_without_implying_labels(tmp_path):
    add_sample(tmp_path, "person")
    add_sample(tmp_path, "head")
    report, queue = batch.inventory(tmp_path, ["person", "head"], 5000)
    assert report["total_class_memberships"] == 2
    assert report["unique_content_ids"] == 1
    assert report["cross_class_duplicate_memberships"] == 1
    assert report["per_class"]["head"]["shortfall"] == 4999
    assert queue[0]["retrieval_classes"] == ["person", "head"]
    assert len(queue[0]["sources"]) == 2
    assert queue[0]["status"] == "needs_review"
    assert not queue[0]["training_ready"]
    assert "labels" not in queue[0] and "annotations" not in queue[0]


def test_inventory_does_not_count_missing_files_duplicates_or_escaped_paths(tmp_path):
    add_sample(tmp_path, "person")
    add_sample(tmp_path, "person")
    add_sample(tmp_path, "person", b"missing", "batch_0000/missing.jpg").unlink()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    with (tmp_path / "person/manifest.jsonl").open("a") as handle:
        handle.write(json.dumps({"id": "escape", "file": "../outside.jpg"}) + "\n")
        handle.write('{"id": "unfinished"')
    report, queue = batch.inventory(tmp_path, ["person"], 5000)
    assert report["per_class"]["person"]["registered_candidates"] == 1
    assert report["per_class"]["person"]["missing_files"] == 1
    assert report["per_class"]["person"]["invalid_records"] == 2
    assert len(queue) == 1


def test_dry_run_is_offline_and_does_not_create_output(tmp_path, capsys):
    output = tmp_path / "output"
    assert batch.main(["--dry-run", "--output-root", str(output)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert sum(config["total_count"] for config in plan["configs"].values()) == 30000
    assert not output.exists()


def test_query_round_keeps_class_ids_and_unpromoted_policy(tmp_path, capsys):
    overrides = tmp_path / "queries.json"
    overrides.write_text(json.dumps({"phone": ["worker using smartphone"]}))
    batch.main(["--dry-run", "--query-overrides", str(overrides)])
    plan = json.loads(capsys.readouterr().out)
    phone = plan["configs"]["phone"]
    assert phone["keywords"] == ["worker using smartphone"]
    assert phone["label_policy"]["aliases"] == {"worker using smartphone": "phone"}
    assert not phone["label_policy"]["promote_discovered"]
    assert [row["id"] for row in plan["spec"]["classes"]] == list(range(6))


@pytest.mark.parametrize("value", [{"unknown": ["worker"]}, {"head": []}, {"head": "worker"}, []])
def test_invalid_query_overrides_do_not_start_jobs(tmp_path, value):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(value))
    with pytest.raises(SystemExit):
        batch.main(["--dry-run", "--query-overrides", str(path)])


def test_explicit_proxy_keeps_private_target_protection(tmp_path, capsys):
    proxy = "http://192.168.1.20:8080"
    assert batch.main(["--dry-run", "--proxy", proxy, "--output-root", str(tmp_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert all(config["proxies"] == [proxy] and not config["allow_private_hosts"]
               for config in plan["configs"].values())


@pytest.mark.parametrize("proxy", ["http://user:secret@proxy.test", "file:///tmp/proxy",
                                    "http://proxy.test?token=secret", "http://proxy.test:abc"])
def test_proxy_secrets_or_invalid_urls_are_rejected(proxy):
    with pytest.raises(SystemExit):
        batch.main(["--dry-run", "--proxy", proxy])


@pytest.mark.parametrize("args", [["--total-per-class", "0"], ["--parallel", "0"],
                                  ["--rate", "nan"], ["--rate", "-1"]])
def test_invalid_budgets_are_rejected(args):
    with pytest.raises(SystemExit) as exc:
        batch.main(args + ["--dry-run"])
    assert exc.value.code == 2


def test_preparation_is_resumable_but_schema_change_requires_new_root(tmp_path):
    args = ["--prepare-only", "--output-root", str(tmp_path)]
    assert batch.main(args) == 0
    assert batch.main(args) == 0
    spec_path = tmp_path / "class_spec.json"
    spec = json.loads(spec_path.read_text())
    spec["classes"][2]["definition"] = "different annotation policy"
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(SystemExit):
        batch.main(args)


@pytest.mark.parametrize("count,target,exit_code,expected", [(1, 1, 2, 0), (1, 2, 0, 1), (1, 1, 1, 1)])
def test_batch_reports_actual_quota_and_preserves_unready_status(
    tmp_path, monkeypatch, count, target, exit_code, expected
):
    add_sample(tmp_path, "person")
    class FinishedProcess:
        pid = 123
        def poll(self):
            return exit_code
    monkeypatch.setattr(batch.subprocess, "Popen", lambda *a, **kw: FinishedProcess())
    assert batch.run_jobs({"person": Path("config.json")}, tmp_path, target, 1) == expected
    report = json.loads((tmp_path / "run_summary.json").read_text())
    assert not report["training_ready"]
    assert report["per_class"]["person"]["registered_candidates"] == count
    assert report["exit_codes"]["person"] == exit_code
    assert (tmp_path / "review_queue.jsonl").is_file()
