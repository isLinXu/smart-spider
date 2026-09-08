# S13 Authorized Browse Policy + Playbook

**Date:** 2026-09-08  
**Depends on:** ADR-0013, ADR-0014

## Scope

| ID | Deliverable |
|----|-------------|
| S13a | `SiteCrawlPolicy` + host rate limit + optional robots |
| S13b | `AuthorizedBrowsePlaybook` (navigate/scroll/extract) |
| S13c | `block_signals` + `complete(terminal=)` |
| S13d | API `/v1/jobs/browse` + worker `authorized_browse` |

## Verification

```bash
pytest tests/test_site_policy.py tests/test_block_signals.py \
  tests/test_browser_playbook.py tests/test_api.py tests/test_worker.py -q
```
