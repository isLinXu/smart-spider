# ADR-0018: SiteProfile and Browse CLI

**Date:** 2026-09-09  
**Status:** Accepted  
**Depends on:** ADR-0014, authorized-crawl roadmap

## Decision

1. Introduce `SiteProfile` (YAML/JSON): reusable allow/deny hosts, rate/robots/session, seed URLs, license/source_terms, enqueue defaults.
2. Empty `allow_hosts` defaults to hosts derived from `seed_urls`.
3. Add `smart-spider-browse`: dry-run validation, `--enqueue` into `authorized_browse` queue, `--once` local playbook execution.
4. Reject out-of-policy URLs before enqueue/run.

## Consequences

Authorized sites become first-class config; operators no longer hand-write ad-hoc queue payloads for each seed.
