# V2 Phase 2 shadow migration

Phase 2 builds Content, MemoryAtom, Evidence, and Rules domains under one
`migration_id`.  It records progress in `manifest.db` and stops at
`V2_BUILDING`; it never enables V2 runtime reads/writes, MCP registration, or
dual-write.

## Source and target boundaries

- V1 history, knowledge, shared-memory groups, ManagedStore JSON, and rule
  intelligence are opened read-only.  Missing optional sources are reported as
  `NO_SOURCE` or `NOT_CONFIGURED`, not fabricated.
- `memory.db` stores atoms, revisions/deltas, scope/ACL and a reference-only
  evidence outbox.  `evidence.db` stores source reference, revision, digest,
  authority, status, metadata, links, maps, and audit refs; it never stores
  conversation/document body.
- Memory and Evidence keep Phase-2 markers in `memory_schema_meta` and
  `evidence_schema_meta`; shared `schema_meta` remains the Phase-1 marker.

## Coordinator guarantees

`V2MigrationCoordinator` persists a checkpoint for source inventory,
initialization, each domain, outbox drain, and validation.  Memory and Rules
evidence projectors are idempotent by stable event IDs; a failed step returns
the manifest to `V1_ACTIVE` for legacy direct callers while retaining target
evidence for inspection.  The production workspace entry point below opts into
retaining `V2_BUILDING` on failure so crash recovery can resume the same batch.
Existing checkpoints are immutable and reruns do not claim a new runtime state.

## Safe workspace entry point

Plan first; default mode performs no writes:

```text
python scripts/prepare_v2_workspace.py --workspace <workspace> --data-home <data-home>
```

Apply only after reviewing the JSON plan and selecting the workspace:

```text
python scripts/prepare_v2_workspace.py --workspace <workspace> --data-home <data-home> --apply
```

Apply mode rejects symlink/reparse or out-of-workspace paths, acquires a
workspace governance lock, takes online backups of the existing manifest and
all discovered legacy sources under `.memoryguard/migration-backups/<migration_id>/`,
records SHA-256 source hashes, and builds missing V2 databases through the
existing coordinator.  Generation is checked before writing; reruns reuse the
same migration identity and immutable backups.  A migration or validator error
keeps the manifest in `V2_BUILDING`, records failure evidence/checkpoints, and
leaves legacy source bytes unchanged.  The report always sets
`readiness_eligible=false`; this command never enters `V2_READY` or
`V2_ACTIVE`.

Promotion is deliberately unavailable in this phase.  A build is acceptable
only when validation reports target integrity/FK success, unchanged V1 source
hashes, zero evidence orphans, zero pending outboxes, zero binding multiset
diff/automatic scope expansion, and explicit loss accounting for configured
authoritative domains.  Unknown authoritative rows block; derived rows are
marked `DERIVED_REBUILD`.

## Acceptance command

Read-only validation (safe for a real project):

```text
python scripts/accept_v2_phase2.py --workspace <workspace> --data-home <data-home>
```

For an isolated fixture/worktree, run the shadow build:

```text
python scripts/accept_v2_phase2.py --workspace <fixture> --data-home <data-home> --write-shadow --migration-id phase2-fixture
```

The JSON result includes `manifest_state`, per-domain counts/loss/orphan/
outbox/binding/auto-expansion/unknown-authoritative metrics, source statuses,
and checkpoints.  `ready` and `can_promote` are always `false`.
