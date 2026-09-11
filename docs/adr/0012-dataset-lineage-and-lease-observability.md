# ADR-0012: Dataset Lineage and Lease Observability

**Date:** 2026-09-07  
**Status:** Accepted  
**Context:** Doubao plan S10 + S9 follow-up

## Decision

1. Publish `.dataset_lineage.json` (dataset_id, version, config fingerprint, provenance) and `manifest.sha256` when a dataset job finishes.
2. Carry `url_normalize_version` / CLIP model identity in sample provenance; carry `dataset_id` / `config_fingerprint` in sample pipeline.
3. Count `recover_expired_leases` into store stats and crawler/multimodal reports; expose verification via `smart-spider-db-stats --verify-manifest`.

## Consequences

Datasets become auditable after the fact without scanning every sample. Lease recovery is no longer silent.
