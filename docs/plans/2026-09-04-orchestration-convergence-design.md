# Orchestration Convergence Design (P0–P2)

**Date:** 2026-09-04  
**Decisions:** Scope `1C` (P0→P2 full), Strategy `2A` (Facade convergence)

## Goal

Converge the dual image/multimodal tracks onto shared workers while keeping
`DatasetCrawler` as the image-compatible facade and
`MultimodalDatasetOrchestrator` as the modality-neutral orchestrator. Replace
oversized modules and kwargs-only CLIs with typed configs, isolate run
artifacts, add CLIP smoke coverage, and introduce pluggable queue/object-store
plus a thin task API.

## Architecture

```text
CLI / API
  ├─ DatasetCrawlConfig ──► DatasetCrawler (image facade: CLIP, quotas, batch_*)
  └─ MultimodalJobConfig ─► MultimodalDatasetOrchestrator
              │                        │
              └──────────┬─────────────┘
                         ▼
              shared pipeline foundation
              ├─ discovery sources
              ├─ download / materialize
              ├─ commit (DatasetRepository / MultimodalRepository)
              ├─ TaskQueue (local SQLite default)
              └─ ObjectStore (local FS default)
```

## Phase summary

| Phase | Deliverable |
|-------|-------------|
| P0 | `DatasetCrawlConfig`; split layout helpers (`dataset_layout.py`); shared pipeline protocols + local adapters; Multimodal hangs `object_store` |
| P1 | `.gitignore` for `dataset_*` / `.artifacts` / `dinox_*` / `runs/`; CLIP smoke tests (`importorskip`) |
| P2 | `TaskQueue` / `ObjectStore` plugins + local defaults; optional FastAPI job API (`smart-spider-api`) |

## Follow-ups (not in this pass)

- Further split `smart_spider.py` / `dataset_filter.py` giant files
- Worker process that claims API jobs and runs `DatasetCrawler.from_config`
- Redis / S3 plugin backends

## Non-goals (this pass)

- Full Redis/Kafka/S3 production backends (interfaces + local defaults only)
- Rewriting search HTML parsers
- Merging CLIs into one binary
- Removing `DatasetCrawler` public API

## Compatibility

- Existing `DatasetCrawler(**kwargs)` and imports of `DatasetDirManager` etc. keep working via re-exports
- CLI flags unchanged; they map into config objects
- New ADR `0009` documents facade convergence and storage plugins
---

# Implementation Plan

> **For Claude:** Implement task-by-task; keep commits logical if user requests them.

**Goal:** Ship P0–P2 foundation for orchestration convergence.  
**Architecture:** Facade + shared pipeline + plugin storage + thin API.  
**Tech Stack:** Python 3.10+, dataclasses, Protocol, pytest, optional FastAPI.

### Task 1: Design ADR-0009

**Files:**
- Create: `docs/adr/0009-facade-convergence-and-storage-plugins.md`

### Task 2: DatasetCrawlConfig

**Files:**
- Create: `smart_spider/dataset_config.py`
- Modify: `smart_spider/dataset_crawler.py`, `smart_spider/dataset_cli.py`, `smart_spider/__init__.py`
- Test: `tests/test_dataset_config.py`

### Task 3: Split layout helpers out of dataset_crawler

**Files:**
- Create: `smart_spider/dataset_layout.py` (DirManager, Progress, MetadataWriter, ManifestWriter)
- Modify: `dataset_crawler.py` re-export; tests keep importing from either path

### Task 4: Shared pipeline foundation

**Files:**
- Create: `smart_spider/pipeline/__init__.py`, `protocols.py`, `local_store.py`, `local_queue.py`
- Wire facades to use `ObjectStore` / shared download helpers where low-risk

### Task 5: Split filter scoring helpers (partial)

**Files:**
- Create: `smart_spider/dataset_filter_scoring.py` (PromptSet, thresholds, CLIPPromptScorer)
- Re-export from `dataset_filter.py`

### Task 6: P1 artifact isolation

**Files:**
- Modify: `.gitignore`
- Optionally document run dir convention in README short note

### Task 7: P1 ML smoke

**Files:**
- Create: `tests/test_clip_smoke.py` (skip if torch/clip missing)
- Light CLI wiring test for `DatasetCrawlConfig.from_args`

### Task 8: P2 API skeleton

**Files:**
- Create: `smart_spider/api.py` (FastAPI optional)
- Modify: `pyproject.toml` optional `api` extra + console script
- Test: import/skip when fastapi absent

### Task 9: Docs touch-up

**Files:**
- Update README architecture blurb pointing to ADR-0009 and new configs
