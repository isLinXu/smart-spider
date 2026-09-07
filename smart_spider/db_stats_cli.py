# coding=utf-8
"""SQLite state-store observability CLI (db stats + optional WAL checkpoint).

用法
----
python -m smart_spider.db_stats_cli --db ./dataset_output/.dataset_state.sqlite3
python -m smart_spider.db_stats_cli --db ./state.sqlite3 --checkpoint TRUNCATE
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .dataset_state import DatasetStateStore


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect DatasetStateStore table counts, candidate states, and WAL size",
    )
    parser.add_argument(
        "--db",
        required=True,
        help="Path to .dataset_state.sqlite3 (or equivalent)",
    )
    parser.add_argument(
        "--checkpoint",
        choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"],
        default=None,
        help="Optional WAL checkpoint mode before printing stats",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--verify-manifest",
        action="store_true",
        help="Also verify manifest.sha256 under the dataset directory containing --db",
    )
    args = parser.parse_args(argv)

    store = DatasetStateStore(args.db)
    try:
        checkpoint = None
        if args.checkpoint:
            checkpoint = store.checkpoint(mode=args.checkpoint)
        stats = store.collect_stats()
        if checkpoint is not None:
            stats["checkpoint"] = checkpoint
        if args.verify_manifest:
            from pathlib import Path

            from .dataset_lineage import load_lineage, verify_manifest_checksum

            dataset_dir = Path(args.db).expanduser().resolve().parent
            stats["manifest_checksum"] = verify_manifest_checksum(dataset_dir)
            lineage = load_lineage(dataset_dir)
            stats["lineage"] = lineage.to_dict() if lineage else None
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(f"db: {stats['path']}")
        print(f"db_bytes={stats['db_bytes']} wal_bytes={stats['wal_bytes']} shm_bytes={stats['shm_bytes']}")
        print(
            f"expired_leases={stats['expired_leases']} "
            f"lease_recoveries_total={stats.get('lease_recoveries_total', 0)}"
        )
        print("tables:")
        for name, count in sorted(stats["table_counts"].items()):
            print(f"  {name}: {count}")
        print("candidate_states:")
        states = stats["candidate_states"]
        if not states:
            print("  (empty)")
        else:
            for state, count in sorted(states.items()):
                print(f"  {state}: {count}")
        if checkpoint is not None:
            print(
                "checkpoint: "
                f"busy={checkpoint['busy']} log={checkpoint['log']} "
                f"checkpointed={checkpoint['checkpointed']}"
            )
        if args.verify_manifest:
            checksum = stats.get("manifest_checksum") or {}
            print(f"manifest_checksum_ok={checksum.get('ok')}")
            lineage = stats.get("lineage") or {}
            if lineage:
                print(
                    f"dataset_id={lineage.get('dataset_id')} "
                    f"version={lineage.get('version')} "
                    f"config_fingerprint={lineage.get('config_fingerprint')}"
                )
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
