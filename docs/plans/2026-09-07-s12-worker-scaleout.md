# S12 Worker / Queue / API Scale-out

**Date:** 2026-09-07  
**Depends on:** ADR-0009, ADR-0013

## Scope

| ID | Deliverable |
|----|-------------|
| S12a | Queue protocol: lease claim, retry/dead-letter, recover, list, retry |
| S12b | SQLite + Redis queues share semantics |
| S12c | Worker multi-process + graceful shutdown + recover loop |
| S12d | API list / retry / recover endpoints |

## Verification

```bash
pytest tests/test_pipeline_plugins.py tests/test_api.py tests/test_worker.py -q
```
