# ADR-0013: Worker Scale-out Queue Semantics

**Date:** 2026-09-07  
**Status:** Accepted  
**Context:** Doubao plan S12 — Worker / queue / API scale-out

## Decision

1. Extend `TaskQueue` with lease-aware `claim`, failure retry → `pending` or `dead`, `recover_expired_claims`, `list_tasks`, and `retry`.
2. Keep SQLite as the default local queue; Redis implements the same protocol (injectable client for tests).
3. Worker supports multi-process pools, per-claim `worker_id` + lease, periodic lease recovery, and SIGINT/SIGTERM graceful stop.
4. API exposes `GET /v1/jobs`, `POST /v1/jobs/{id}/retry`, `POST /v1/worker/recover`, plus richer task fields (`attempts`, `lease_until`, `claimed_by`).

## Consequences

Crash/hang workers no longer strand tasks forever. Dead-letter + retry give operators a clear recovery path without changing the dual-facade architecture.
