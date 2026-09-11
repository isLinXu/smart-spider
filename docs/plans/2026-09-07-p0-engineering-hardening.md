# P0 Engineering Hardening Implementation Plan

> **For Claude:** Implement task-by-task. Keep public facades stable.

**Goal:** Deliver S1–S5 from the 2026-09-07 optimization review without changing external crawl behavior.

**Architecture:** Progressive engineering — tests first, then contracts/config/report, then file splits with re-exports.

**Tech Stack:** Python 3.10+, dataclasses, PyYAML (optional soft dep via PyYAML or stdlib-only JSON fallback + yaml if available), pytest, GitHub Actions.

---

### Task 1: Scene gate truth fixtures (S5)
- Create: `tests/fixtures/scene_gate/truth_cases.json`
- Create: `tests/test_scene_gate_truth.py`

### Task 2: Repository crash / idempotency E2E (S5)
- Create/extend: `tests/test_repository_crash_e2e.py`

### Task 3: CI smoke for CLIs (S5)
- Modify: `.github/workflows/ci.yml`

### Task 4: Contract validate + format_version (S4)
- Modify: `smart_spider/dataset_contracts.py`
- Create: `tests/test_contract_validate.py`

### Task 5: Config YAML IO (S2)
- Create: `smart_spider/config_io.py`
- Modify: `dataset_cli.py`, `dataset_config.py`
- Create: `tests/test_config_io.py`

### Task 6: Unified Report schema (S3)
- Create: `smart_spider/report.py`
- Wire soft adapters from crawler/smart_spider report writers
- Create: `tests/test_report.py`

### Task 7: Split http_client helpers (S1 partial)

**Status:** Deferred in this pass — extraction risk around `_build_session` / banner boundaries.
Previous splits already landed (`dataset_layout`, `crawl_types`, `dedup`, `media_io`, `dataset_filter_types`).
Resume after S5 green on CI with line-accurate extraction.

### Task 8: Split dataset_filter scorers/signals/policy (S1 partial)

**Status:** Deferred with Task 7; types already extracted to `dataset_filter_types.py`.
