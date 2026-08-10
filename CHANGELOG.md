# Changelog

## [0.6.0] - 2026-08-10

### Added

- Production-ready MemoryGuard V2 data plane with separate SQLite domains for Memory, Rules, Evidence, Content, Runtime, Projection, Assets, CodeGraph, Skills, and System state.
- Four-state cutover manifest: `V1_ACTIVE → V2_BUILDING → V2_READY → V2_ACTIVE` with fail-closed rollback paths.
- Packaged `memoryguard-v2` operator CLI for read-only status, frozen-source preparation, and explicit activation.
- Coherent SQLite online-backup migration from live V1 sources, immutable frozen-source validation, migration evidence, and repeated live-source drift gates.
- Two-epoch Reference Audit, per-domain storage health reporting, guarded maintenance, and native coverage readiness checks.
- Native V2 Rule lifecycle, RuleMerge, extraction/enrichment, External MCP import, provider control-plane, history, Knowledge reference, and GUI governance routes.
- Safe unbound CLI diagnostics: `doctor` and `mcp-status` expose workspace-level health without tenant counts when no Agent binding is present.

### Changed

- All 233 registered MCP/CLI/GUI/Hook surfaces are explicitly classified: 138 implemented, 95 retired, 0 neutral, 0 blocker.
- V2 no longer silently falls back to legacy stores after READY/ACTIVE. Retired V1 workflows return stable retired results.
- RuleMerge production mutations now use V2 `rules.db` instead of the legacy rule-intelligence store.
- Extraction/enrichment staging now uses the V2 Content Plane and GovernanceV2 Memory/Evidence/Decision flow.
- Memory partial updates preserve untouched fields; failed evidence projection events remain outstanding and retryable until projected.
- Reference Audit distinguishes authoritative references from opaque transport/audit metadata and preserved migration evidence.

### Safety and migration

Existing v0.5.x workspaces are not auto-activated. The supported explicit cutover is:

```bash
memoryguard-v2 status -w .
memoryguard-v2 prepare -w . --apply
memoryguard-v2 activate -w . --confirm V2_ACTIVE
```

Preparation preserves legacy V1 data and `.memoryguard/migration-backups`. Activation performs a fresh live-source drift check before changing the manifest.

### Verification

Release-candidate verification on the production-active workspace:

- V2 test suite: 589 passed.
- Non-V2 test suite: 1087 passed.
- Split full-suite total: 1676 passed / 0 failed.
- Production readiness: `READY`, blockers `[]`.
- 12-domain Reference Audit: `PASS`, blockers `[]`.
- All 12 authoritative SQLite domain reports: integrity `ok`, foreign-key errors `0`.
- Production manifest: `V2_ACTIVE`, generation 11.

## [0.5.2] - 2026-08-09

- Canonical rule reconciliation and governance diagnostics.
- Multi-process runtime lease protection.
- Desktop/neuron graph improvements.
- History and Knowledge Library reliability fixes.

Earlier release history is available from GitHub Releases.
