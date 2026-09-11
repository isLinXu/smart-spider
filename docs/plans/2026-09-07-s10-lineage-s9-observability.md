# S10 lineage + S9 lease observability

**Date:** 2026-09-07

## S10

- `smart_spider/dataset_lineage.py`: `DatasetLineage`, config fingerprint, `.dataset_lineage.json`, `manifest.sha256`
- `DatasetCrawler` / `MultimodalDatasetOrchestrator` publish lineage + checksum on finish
- Sample provenance includes `url_normalize_version` + `clip_model`; pipeline carries `dataset_id` / `config_fingerprint`

## S9 follow-up

- `recover_expired_leases` accumulates `lease_recoveries_total` and emits `leases_recovered` events
- Crawler recovers leases on start/end and writes counts into `_dataset_report.json`
- `smart-spider-db-stats --verify-manifest` checks checksum + lineage sidecar

## Verify

```bash
pytest tests/test_dataset_lineage.py tests/test_dataset_state.py tests/test_dataset_config.py -q
```
