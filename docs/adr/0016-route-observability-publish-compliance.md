# ADR-0016: Route Observability and Publish Compliance

**Date:** 2026-09-08  
**Status:** Accepted  
**Depends on:** ADR-0014, ADR-0015

## Decision

1. Record route action **and** decision reason / block kinds on `QualityReport` and `MultimodalJobReport`.
2. Emit `unified_report.json` via `UnifiedReport.from_multimodal_report` at job end.
3. Add `compliance.py`: URL redaction, license/source_terms on sample provenance, `.publish_checklist.json` with ready/warnings.
4. Default `respect_robots=True` for multimodal/browse; CLI uses `--ignore-robots` to opt out.

## Consequences

Operators can audit why browser fallback happened and whether a dataset is publish-ready without scraping logs.
