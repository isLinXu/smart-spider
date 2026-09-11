# S6 / S11 follow-up (download precheck + incremental filter)

**Date:** 2026-09-07  
**Depends on:** ADR-0011 / P1 hardening

## S6 Download & decode precheck

- `probe_image_header` / `assert_header_within_budget` reject oversized JPEG/PNG/GIF/WEBP before full Pillow decode
- `DatasetCrawler` streams SHA-256 while reading bodies and probes headers early
- `DatasetRepository.commit_image` hashes while writing staging bytes (single pass)
- `AssetStore.stage_image` uses the same write+hash helper

## S11 Incremental filter & clustered near-dedupe

- `DatasetImageFilter.evaluate(..., incremental=True)` reuses decisions for unchanged `mtime_ns+size`
- State: `dataset/_filter_cache/incremental_state.json`
- CLI: `--incremental`
- `run_near_dedupe` clusters via `cluster_embeddings` (signature vectors by default; optional external embeddings), then confirms with existing pHash/aspect/colour gates

## Verify

```bash
pytest tests/test_image_safety.py tests/test_dataset_filter.py \
  tests/test_dataset_repository.py tests/test_repository_crash_e2e.py -q
```
