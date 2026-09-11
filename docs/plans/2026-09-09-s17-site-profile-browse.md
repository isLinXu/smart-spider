# S17 SiteProfile + Browse CLI

**Date:** 2026-09-09  
**Depends on:** ADR-0018, `docs/plans/2026-09-09-authorized-crawl-roadmap.md`

## Delivered

| Item | Status |
|------|--------|
| `SiteProfile` load/validate | done |
| `smart-spider-browse` dry-run / enqueue / once | done |
| Example profile | `examples/site_profiles/example.com.yaml` |

## Verification

```bash
pytest tests/test_site_profile.py -q
smart-spider-browse --profile examples/site_profiles/example.com.yaml
```

## Next

S18 — declarative interaction steps + same-host BFS.
