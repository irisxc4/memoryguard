# Changelog

## [0.7.0] - 2026-08-12 (local release acceptance passed; ready for commit/publish, not yet published)

### Scope

- V2-only production boundary: V1 runtime/store modules are physically absent
  from production entrypoint import closure. `V1_ACTIVE` is a migration starting
  state, not a runtime fallback; legacy format input is accepted only by
  `memoryguard.migration`, while other entrypoints fail closed with
  `v2_upgrade_required`.
- V2 Memory, Evidence, History, Source, Binding, and Group planes are explicit;
  raw History remains separate from governed Memory and evidence receipts.

### Added and changed

- Canonical reconciliation builds `shared_baseline`, `agent_overlay`, and
  `project_overlay` bundles, retains source links, activates canonical reads
  only after parity, and shadows old duplicates recoverably.
- Same-group V2 automatic organization performs exact/semantic deduplication;
  Rule duplicate scans create governed merge proposals, and merge/supersede/
  conflict/quarantine decisions retain scope, evidence, idempotency, and undo
  receipts across Agents sharing one trusted group.
- Knowledge accepts files and folders as governed books/documents. Source bodies
  stay in the Content Plane; Knowledge stores metadata/references and supports
  re-ingest, remove/restore/purge, and explicit candidate review.
- GUI Agent discovery/name/instance data, source selection, Binding/Group member
  management, drift, personal/shared groups, leave, and dissolve use V2-native
  system control. Build, Knowledge, import, history, maintenance, release, and
  compatibility work use durable `TaskRun` status, bounded cancellation, and
  owned background-worker/process cleanup before shutdown.
- Graphify metadata-only export feeds CodeGraph under trusted scope with source
  role, provenance, source maps, revisions, tombstones, outbox state, and
  bounded query/path/explain/affected operations. Source bodies are rejected.
- Fail-closed state/scope/provenance/path/metadata checks, isolation, public
  receipt redaction, audit/outbox decisions, and Content Plane held-occurrence
  rollback are part of the V2 release boundary.
- `memoryguard upgrade` provides the 0.6.2-to-0.7.0 preview, `V2_READY`, and
  exact `V2_ACTIVE` confirmation path; failed validation remains non-active and
  V1/migration evidence is retained.

### Local release acceptance evidence

- Local release acceptance passed: `1761 / 1761`, with no skip or xfail.
- V1 retirement + CodeGraph: `15 / 15`; Graphify focused checks: `3 / 3`;
  canonical reconciliation: `ACCEPTED`; RuleMerge: `46 / 46`; v3.2: `27 / 27`.
- The real full-repository Graphify export/projection covered `486 files / 11672
  nodes / 38714 edges → 11667 canonical symbols / 38714 edges`; query/path/
  affected passed and failure atomicity was `0` throughout.
- Final clean wheel: `206 files`, `legacy bad=0`; isolated package, CLI, and MCP
  all reported `0.7.0`; desktop help passed.
- These Graphify results record the focused checks and the real full-repository
  export/projection only. They do not claim that upstream Graphify's full-
  repository test suite passed.

### Release state

- Local release acceptance passed. v0.7.0 is ready for commit/publish, not yet
  published.
- The acceptance result is local and receipt-backed. The Graphify evidence is
  intentionally limited to the focused `3 / 3` checks and the real
  full-repository export/projection; it is not an upstream Graphify
  full-repository test-suite claim.

## [0.6.2] - 2026-08-10

### Fixed

- Memory, Evidence, and Content schema preflights now inspect a private copy of the SQLite main file plus any `-wal`/`-shm` companions. Older SQLite builds can therefore checkpoint the temporary handle without changing the live database.
- The post-failure no-write assertions establish their physical baseline after reading the expected marker, so an older SQLite observer cannot contaminate the no-write check.

### Verification

- Full local test suite: 1678 passed / 0 failed.
- GitHub Linux/Python 3.12 and Windows runtime acceptance: passed on the preceding release candidate.
- GitHub Linux/Python 3.10 rerun is the release gate for this correction.

## [0.6.1] - 2026-08-10

### Fixed

- Restored the Python 3.10 CI contract for V2 schema fail-closed checks.
- Memory and Evidence stores now preflight existing base schema metadata through SQLite `mode=ro` before opening any write-capable WAL connection, so future/unknown schema markers fail without changing the database image.
- V2 migration tests now use genuinely read-only SQLite connections for post-failure observations; older SQLite versions could otherwise checkpoint WAL state from the test itself and make a no-write assertion fail spuriously.

### Verification

- Targeted schema/migration/storage regressions: 81 passed.
- V2 test suite: 591 passed.
- Non-V2 test suite: 1087 passed.
- Split full-suite total: 1678 passed / 0 failed on the local release candidate.
- Release gate: GitHub Linux / Python 3.10 full CI must pass before tag/release.

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
