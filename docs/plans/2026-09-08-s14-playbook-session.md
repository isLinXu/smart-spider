# S14 Playbook Router Fallback + Session State

**Date:** 2026-09-08  
**Depends on:** ADR-0014, ADR-0015

## Verification

```bash
pytest tests/test_session_state.py tests/test_multimodal_pipeline.py \
  tests/test_multimodal_cli.py tests/test_site_policy.py \
  tests/test_browser_playbook.py tests/test_worker.py -q
```
