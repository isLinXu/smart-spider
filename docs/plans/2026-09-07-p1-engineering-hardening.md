# P1 Engineering Hardening (S1 continuation + S7/S8/S9)

**Date:** 2026-09-07  
**Depends on:** P0 (`docs/plans/2026-09-07-p0-engineering-hardening.md`, ADR-0010)

## Scope

| ID | Deliverable |
|----|-------------|
| S1 | Split `http_client` (`rate_limit` / `proxy_pool` / `http_metrics`) and `dataset_filter` (`scorers` / `signals` / `policy`) with re-exports |
| S7 | Graded HTTP retries: 429 + Retry-After (capped), 5xx/403 backoff+jitter, hard 4xx no retry |
| S8 | Default runnable `SceneSignalDetector` (`HeuristicSceneSignalDetector`, optional YOLO) wired into crawler/orchestrator |
| S9 | `DatasetStateStore.collect_stats` / `checkpoint`; `smart-spider-db-stats` CLI; WAL truncate on close |

## Non-goals

- S6 download/decode hot-path rewrite
- S10 dataset lineage / S11 incremental filter
- Production YOLO calibration (heuristic is CI-safe default)

## Verification

```bash
pytest tests/test_http_client.py tests/test_dataset_filter.py \
  tests/test_scene_signal_detector.py tests/test_dataset_state.py \
  tests/test_scene_gate_truth.py -q
```
