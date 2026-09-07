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
    args = parser.parse_args(argv)

    store = DatasetStateStore(args.db)
    try:
        checkpoint = None
        if args.checkpoint:
            checkpoint = store.checkpoint(mode=args.checkpoint)
        stats = store.collect_stats()
        if checkpoint is not None:
            stats["checkpoint"] = checkpoint
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        print(f"db: {stats['path']}")
        print(f"db_bytes={stats['db_bytes']} wal_bytes={stats['wal_bytes']} shm_bytes={stats['shm_bytes']}")
        print(f"expired_leases={stats['expired_leases']}")
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
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
