# ADR-0011: P1 Graded Retries, Default Scene Detector, State Observability

**Date:** 2026-09-07  
**Status:** Accepted  
**Context:** Doubao optimization plan items S1 (continued), S7, S8, S9

## Decision

1. **Module splits (S1):** Extract `rate_limit` / `proxy_pool` / `http_metrics` from `http_client`, and `dataset_filter_scorers` / `signals` / `policy` from `dataset_filter`. Public imports remain on the original modules.
2. **Graded HTTP retries (S7):** `429` honors capped `Retry-After`; `403` and `5xx` use exponential backoff + jitter; other `4xx` are returned without retry.
3. **Default scene detector (S8):** `HeuristicSceneSignalDetector` is the CI-safe default; optional `YoloSceneSignalDetector` when ultralytics is installed. Enabled scene gates auto-wire `default_scene_signal_detector()` unless injected.
4. **State observability (S9):** `DatasetStateStore.collect_stats` / `checkpoint`; WAL `TRUNCATE` on `close`; CLI `smart-spider-db-stats`.

## Consequences

- Gate path becomes runnable without injecting signals, while remaining fail-closed for weak heuristic evidence.
- Long-running jobs can inspect and truncate WAL without ad-hoc SQLite tooling.

## Follow-ups

- S6 download/decode precheck; S10 lineage; S11 incremental filter; calibrate YOLO against truth fixtures before making it the preferred default in production profiles.
