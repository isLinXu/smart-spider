# ADR-0019: Playbook steps, unified CLI, session retry, publish exit

**Date:** 2026-09-14  
**Status:** Accepted  
**Depends on:** ADR-0010, ADR-0014, ADR-0018, authorized-crawl roadmap S18–S20

## Decision

1. Split `DatasetCrawler` internals into discovery / download window / scene admission / repository commit (facade methods stay).
2. Unify `smart-spider <subcommand>`; keep legacy console scripts and bare `smart-spider --keywords` as aliases.
3. S18: declarative `wait_for` / `click` / `type` playbook steps plus same-host BFS under `max_depth`.
4. S19: headful `--login` exports Playwright `storage_state`; API/CLI can list dead tasks by `block_kind` and retry after a new session.
5. S20: browse metrics (`policy_denied`, `robots_skip`, `challenge_dead`); CLI exits non-zero when `publish_checklist.ready` is false unless `--allow-unready`.

## Non-goals

CAPTCHA solving, fingerprint spoofing, merging the dataset/multimodal facades.

## Follow-up (2026-09-14)

- Extract image ingest (bounded fetch/decode/size/variance + JPEG conversion) from `_download_and_save`.
- Browse `--once` writes `unified_report.json` with `track=browse`.
- Worker `--kind` empty claims any queued kind (`dataset_crawl` and `authorized_browse`).
- `--no-bfs` means skip same-host BFS, not “cross-domain BFS”.
- Extract quota reservation + repository/legacy materialize from `_download_and_save`.
- Browse `--once` prints a seed → block/reason summary and stores it on `unified_report.extras.summary`.
- Extract CLIP / image-query filters into `dataset_semantic_gate` (torch-free; crawler still supplies embeddings).
- Combine semantic + scene quality into `admit_ingested_image`; assemble SampleRecord/metadata in `dataset_sample_builder`.
- Extract `ImageDownloadSession` for URL/content claims, download-window slots, candidate reject/fail, and quota rollback.
- Collapse `_download_and_save` into a session/scene/ingest/admit/commit orchestrator (`bind_record_builder`, `_commit_admitted_image`).
- Search-page crawl uses `page_offsets` / `items_to_discovered` instead of inlined range/URL loops.
- Extract dataset job-end reporting (`lineage` / publish checklist / `unified_report.json`) into `dataset_job_report`; `_generate_report` stays a crawler orchestrator.
- Search/site/spider_tools loops share `_collection_limit_reached` / `_save_discovered_images`; page planning uses `estimate_pages_needed`, `discover_page_images`, and `drain_inflight_window`.
- Collapse `crawl()` into a job orchestrator (`remaining_quota`, keyword/site/spider_tools stages, `_finish_job`); SiteCrawler kwargs and spider_tools URL mapping live in `dataset_crawl_plan`.
