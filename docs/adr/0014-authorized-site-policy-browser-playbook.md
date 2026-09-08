# ADR-0014: Authorized Site Policy and Browser Playbook

**Date:** 2026-09-08  
**Status:** Accepted  
**Context:** Product goal is capable authorized crawling with human-like browser actions — not unrestricted anti-bot evasion.

## Decision

1. Introduce `SiteCrawlPolicy`: allow/deny hosts, per-host polite rate limit, optional robots.txt, session cookie path, depth/link caps.
2. Introduce `AuthorizedBrowsePlaybook`: navigate → delay → scroll → extract links → filter by policy/robots.
3. Classify block signals (`rate_limited` / `auth_required` / `challenge` / …); wire to queue via `complete(..., terminal=)` so non-retryable challenges go dead immediately.
4. API `POST /v1/jobs/browse` and worker kind `authorized_browse` enqueue follow-up links within `max_depth`.

## Non-goals

Fingerprint spoofing, CAPTCHA solving, WAF evasion, or any guarantee of bypassing site protections.

## Consequences

Operators can crawl sites they are authorized to access with observable refusals; hung/challenged pages surface as dead-letter for human intervention.
