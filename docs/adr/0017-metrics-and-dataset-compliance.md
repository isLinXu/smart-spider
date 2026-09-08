# ADR-0017: Process Metrics and Dataset Compliance Reports

**Date:** 2026-09-08  
**Status:** Accepted  
**Depends on:** ADR-0013, ADR-0016

## Decision

1. Add dependency-free `metrics.py` with Prometheus text exposition; expose `GET /metrics` on the job API (queue depth gauges + worker task counters).
2. Workers call `observe_task_outcome` on terminal queue statuses.
3. Dataset track writes `unified_report.json` and `.publish_checklist.json`, applies compliance provenance (license/source_terms/URL redaction) like multimodal.

## Consequences

Operators can scrape queue health without a separate metrics stack, and dataset outputs share the same publish-readiness artifacts as multimodal jobs.
