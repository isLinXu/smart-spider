# ADR-0015: Playbook Router Fallback and Session State

**Date:** 2026-09-08  
**Status:** Accepted  
**Depends on:** ADR-0014

## Decision

1. Add Playwright `storage_state` import/export on `PersistentBrowserSession` / `BrowserController`, with helpers in `session_state.py` and `SiteCrawlPolicy.storage_state_path`.
2. Add `PlaybookBrowserSource` so `AdaptiveSourceRouter` can fall back to `AuthorizedBrowsePlaybook` when static discovery is blocked/empty.
3. `StaticPageSource` records `block_signals` metadata; router reasons include block kind.
4. Multimodal CLI: `--browser-playbook`, `--allow-hosts`, `--storage-state`, `--session-cookies`, `--export-storage-state`.

## Non-goals

Anti-bot evasion, CAPTCHA solving, or silent bypass of challenges.

## Consequences

Authorized login sessions can be reused; static→browser playbook fallback is first-class without changing dual-facade architecture.
