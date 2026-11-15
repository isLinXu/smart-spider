#!/usr/bin/env python3
"""Collect resumable six-class industrial image candidates, never training labels.

Uses the existing DatasetCrawler CLI in separate processes/output directories.
No model is loaded by this launcher; discovery labels remain unpromoted. A final
review queue merges exact content hashes across collection classes without
rewriting the repository's SampleRecord manifests or deleting images.
"""
from __future__ import annotations

import argparse
from collections import deque
import fcntl
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
CLASS_NAMES = ("person", "forklift", "truck_rear", "head", "phone", "helmet")


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def load_spec(path: Path) -> dict:
    spec = json.loads(path.read_text(encoding="utf-8"))
    classes = spec.get("classes", [])
    if spec.get("schema_version") != 1 or spec.get("task_type") != "object_detection":
        raise ValueError("expected schema_version=1 and task_type=object_detection")
    if [item["name"] for item in classes] != list(CLASS_NAMES):
        raise ValueError("class order must be person,forklift,truck_rear,head,phone,helmet")
    if [item["id"] for item in classes] != list(range(6)):
        raise ValueError("class IDs must be 0..5 in the declared order")
    for item in classes:
        queries = item.get("queries")
        if not isinstance(queries, list) or not queries or any(
            not isinstance(query, str) or not query.strip() for query in queries
        ):
            raise ValueError(f"{item['name']}: queries must be non-empty strings")
    return spec


def build_config(item: dict, root: Path, total: int, rate: float, proxy: str | None = None) -> dict:
    return {
        "keywords": list(dict.fromkeys(item["queries"])),
        "total_count": total,
        "output_dir": str(root / item["name"]),
        "batch_size": 100,
        "search_engines": ["baidu", "bing"],
        "use_clip": False,
        "label_policy": {
            "mode": "discovery", "fixed_labels": [item["name"]],
            "aliases": {query: item["name"] for query in item["queries"]},
            "promote_discovered": False,
        },
        "resume": True,
        "image_output_format": "jpg",
        "jpeg_quality": 95,
        "min_width": 640,
        "min_height": 360,
        "rate": rate,
        "proxies": [proxy] if proxy else None,
        "max_workers": 6,
        "max_inflight_pages": 2,
        "max_inflight_downloads": 6,
        "max_pending_candidates": 60,
        "per_domain_concurrency": 1,
        "memory_budget_mb": 256,
        "connect_timeout": 5,
        "read_timeout": 15,
        "max_retries": 1,
        "max_file_size": 12 * 1024 * 1024,
        "max_image_pixels": 50_000_000,
        "disk_guard_mb": 2048,
        "max_domain_share": 0.15,
        "use_curl_cffi": False,
        "allow_private_hosts": False,
        "respect_robots": True,
        "license": "",
        "source_terms": "",
    }


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def inventory(root: Path, names: list[str], target: int) -> tuple[dict, list[dict]]:
    grouped: dict[str, dict] = {}
    counts = {}
    for name in names:
        identifiers = set()
        missing = invalid = 0
        manifest = root / name / "manifest.jsonl"
        if manifest.exists():
            with manifest.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        sample = json.loads(line)
                        identifier = sample["id"]
                        relative = sample["file"]
                        if not isinstance(identifier, str) or not identifier or not isinstance(relative, str) or not relative:
                            raise ValueError("missing ID or image path")
                        path = (root / name / relative).resolve()
                        path.relative_to((root / name).resolve())
                    except (ValueError, KeyError, TypeError):
                        invalid += 1
                        continue
                    if not path.is_file():
                        missing += 1
                        continue
                    identifiers.add(identifier)
                    entry = grouped.setdefault(identifier, {
                        "sample_id": identifier,
                        "intended_task_type": "object_detection",
                        "status": "needs_review",
                        "training_ready": False,
                        "retrieval_classes": [],
                        "sources": [],
                    })
                    if name not in entry["retrieval_classes"]:
                        entry["retrieval_classes"].append(name)
                    source = {
                        "file": str(path.relative_to(root.resolve())),
                        "provenance": sample.get("provenance", {}),
                        "quality": sample.get("quality", {}),
                    }
                    if source not in entry["sources"]:
                        entry["sources"].append(source)
        counts[name] = {
            "registered_candidates": len(identifiers),
            "target": target,
            "shortfall": max(0, target - len(identifiers)),
            "missing_files": missing,
            "invalid_records": invalid,
        }
    total = sum(row["registered_candidates"] for row in counts.values())
    report = {
        "training_ready": False,
        "counts_are": "unreviewed candidates; not verified class presence or bounding boxes",
        "per_class": counts,
        "total_class_memberships": total,
        "unique_content_ids": len(grouped),
        "cross_class_duplicate_memberships": total - len(grouped),
        "pending": ["source_rights_review", "visual_class_review", "bounding_box_annotation",
                    "near_duplicate_clustering", "business_holdout_evaluation"],
    }
    return report, list(grouped.values())


def worker_command(config_path: Path) -> list[str]:
    # dataset_cli's legacy __main__ does not propagate main()'s exit code.
    return [sys.executable, "-B", "-c",
            "from smart_spider.dataset_cli import main; raise SystemExit(main())",
            "--config", str(config_path)]


def run_jobs(configs: dict[str, Path], root: Path, target: int, parallel: int) -> int:
    pending = deque(configs)
    running: dict[str, tuple[subprocess.Popen, object]] = {}
    exits: dict[str, int] = {}
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        for process, _handle in running.values():
            if process.poll() is None:
                process.terminate()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    next_report = 0.0
    try:
        while running or (pending and not stopping):
            while pending and len(running) < parallel and not stopping:
                name = pending.popleft()
                handle = (root / name / "crawl.log").open("ab", buffering=0)
                try:
                    process = subprocess.Popen(worker_command(configs[name]), cwd=ROOT,
                                               stdout=handle, stderr=subprocess.STDOUT)
                except BaseException:
                    handle.close()
                    raise
                running[name] = (process, handle)
                print(f"started {name}: pid={process.pid}", flush=True)
            for name, (process, handle) in list(running.items()):
                code = process.poll()
                if code is not None:
                    exits[name] = code
                    handle.close()
                    del running[name]
                    print(f"finished {name}: exit={code}", flush=True)
            if time.monotonic() >= next_report:
                report, _ = inventory(root, list(configs), target)
                report.update(exit_codes=exits, running=list(running), pending_classes=list(pending))
                write_json(root / "run_summary.json", report)
                print(json.dumps({name: row["registered_candidates"] for name, row
                                  in report["per_class"].items()}), flush=True)
                next_report = time.monotonic() + 30
            if stopping:
                break
            if running:
                time.sleep(1)
    finally:
        for name, (process, handle) in running.items():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            exits[name] = process.returncode
            handle.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        report, queue = inventory(root, list(configs), target)
        report.update(exit_codes=exits, interrupted=stopping, running=[], pending_classes=list(pending))
        write_json(root / "run_summary.json", report)
        temporary = root / "review_queue.jsonl.tmp"
        with temporary.open("w", encoding="utf-8") as handle:
            for entry in queue:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        temporary.replace(root / "review_queue.jsonl")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    complete = all(row["shortfall"] == row["missing_files"] == row["invalid_records"] == 0
                   for row in report["per_class"].values())
    # Exit 2 from a child means its publication checklist remains unready.
    healthy = len(exits) == len(configs) and all(code in (0, 2) for code in exits.values())
    return 0 if complete and healthy and not stopping else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=ROOT / "examples/industrial_objects.json")
    parser.add_argument("--output-root", type=Path, default=Path("dataset_industrial_objects"))
    parser.add_argument("--total-per-class", type=positive_int, default=5000)
    parser.add_argument("--classes", nargs="+", choices=CLASS_NAMES, default=list(CLASS_NAMES))
    parser.add_argument("--parallel", type=positive_int, default=2)
    parser.add_argument("--rate", type=float, default=2.0, help="per-crawler request rate")
    parser.add_argument("--proxy", help="explicit existing proxy URL without embedded credentials")
    parser.add_argument("--query-overrides", type=Path, help="JSON class-to-query-list mapping for a new acquisition round")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print plan without writing or fetching")
    mode.add_argument("--prepare-only", action="store_true", help="write plan and configs, no fetching")
    mode.add_argument("--status", action="store_true", help="read committed manifests, no fetching")
    args = parser.parse_args(argv)
    if not 0 < args.rate <= 10:
        parser.error("--rate must be in (0, 10]")
    if args.proxy:
        try:
            proxy = urlsplit(args.proxy)
            if proxy.scheme not in ("http", "https", "socks5", "socks5h") or not proxy.hostname:
                raise ValueError("invalid proxy URL")
            if proxy.username is not None or proxy.password is not None:
                raise ValueError("embedded proxy credentials cannot be written to crawl configs")
            if proxy.path not in ("", "/") or proxy.query or proxy.fragment:
                raise ValueError("proxy URL cannot contain a path, query or fragment")
            proxy.port
        except ValueError as exc:
            parser.error(str(exc))
    try:
        spec = load_spec(args.spec)
        if args.query_overrides:
            overrides = json.loads(args.query_overrides.read_text(encoding="utf-8"))
            if not isinstance(overrides, dict) or not overrides or set(overrides) - set(CLASS_NAMES):
                raise ValueError("query overrides must map known classes to non-empty query lists")
            for name, queries in overrides.items():
                if not isinstance(queries, list) or not queries or any(
                    not isinstance(query, str) or not query.strip() for query in queries
                ):
                    raise ValueError(f"{name}: query override must be non-empty strings")
            for item in spec["classes"]:
                if item["name"] in overrides:
                    item["queries"] = list(dict.fromkeys(overrides[item["name"]]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    root = args.output_root.resolve()
    names = list(dict.fromkeys(args.classes))
    configs = {item["name"]: build_config(item, root, args.total_per_class, args.rate, args.proxy)
               for item in spec["classes"] if item["name"] in names}
    if args.status:
        print(json.dumps(inventory(root, names, args.total_per_class)[0], ensure_ascii=False, indent=2))
        return 0
    plan = {"spec": spec, "candidate_target_per_class": args.total_per_class,
            "training_ready": False, "configs": configs}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".batch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"another batch owns {root}; use --status to inspect it")
        spec_path = root / "class_spec.json"
        if spec_path.exists() and json.loads(spec_path.read_text(encoding="utf-8")) != spec:
            parser.error("existing class specification differs; choose a new output root")
        write_json(spec_path, spec)
        write_json(root / "crawl_plan.json", plan)
        paths = {}
        for name, config in configs.items():
            (root / name).mkdir(exist_ok=True)
            paths[name] = root / name / "crawl_config.json"
            write_json(paths[name], config)
        if args.prepare_only:
            print(f"prepared {len(paths)} classes, {args.total_per_class} candidates each: {root}")
            return 0
        return run_jobs(paths, root, args.total_per_class, args.parallel)


if __name__ == "__main__":
    raise SystemExit(main())
