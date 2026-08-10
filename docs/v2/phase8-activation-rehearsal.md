# Phase 8 isolated activation rehearsal

Phase 8 proves the migration/cutover mechanics without granting permission to
activate a real workspace. It emits exactly one JSON document on stdout.

## Safety contract

- `--workspace` is a read-only control. The script fingerprints its manifest
  and every READY V1 source before and after the run.
- The default source is a minimal synthetic V1 fixture in the system temporary
  directory. It does not copy the real workspace's large SQLite databases.
- SQLite sources enter the run fixture only through `sqlite3.Connection.backup`;
  every backup receives an integrity check.
- The run fixture must be new, outside and disjoint from both the control and
  source fixture. A real workspace, its ancestor, or its descendant fails
  closed with `unsafe_fixture_target`.
- Skills and Maintenance databases are initialized only inside the disposable
  run fixture because Phase 2 creates only the ten core databases.
- The production readiness assembler is invoked against the control in a
  read-only preflight. Its transition payload is never consumed there.
- Fixture READY, ACTIVE, and rollback transitions use one `ReadinessGate`.
  Rollback must finish at `V1_ACTIVE`.
- Script-owned fixture trees are removed on success and failure. User-provided
  source fixtures are never removed.

## Result model

The two outcomes are intentionally different:

- `outcomes.synthetic_rehearsal.status=PASS` proves online backup, strict Phase
  2 shadow build, read-only validation, READY, ACTIVE, native V2 read smoke,
  rollback, source immutability, and cleanup. Its generated readiness evidence
  is explicitly fixture-only and is not production evidence.
- `outcomes.real_workspace_preflight.status=BLOCKED` is the expected policy
  result. `readiness_status` and the redacted readiness receipt retain the real
  assembler's blockers. `unchanged=true` proves manifest bytes/logical fields
  and selected source bytes/SHA-256 are unchanged.

Top-level `ok=true` means the isolated rehearsal passed and the real preflight
remained blocked and unchanged. It never means real activation is approved.

## Commands

Default, safe rehearsal (synthetic minimal V1 source):

```powershell
rtk proxy python scripts/accept_v2_phase8.py --json
```

Use an existing disposable V1 source fixture:

```powershell
rtk proxy python scripts/accept_v2_phase8.py `
  --source-fixture C:\temp\memoryguard-v1-fixture `
  --source memory:fixture-group `
  --json
```

If that fixture has Knowledge outside its workspace, add
`--source-data-home C:\temp\fixture-data-home`.

Copying sources from `--workspace` is disabled unless the operator explicitly
accepts the potentially large online backup:

```powershell
rtk proxy python scripts/accept_v2_phase8.py `
  --allow-large-copy `
  --source memory:shared-group `
  --json
```

`--source` without `--source-fixture` or `--allow-large-copy` fails with
`source_selection_requires_explicit_source`. Pointing `--source-fixture` at the
control without `--allow-large-copy` fails with
`real_source_copy_requires_opt_in`.

## Stop conditions

Stop and do not attempt real activation when any of these is true:

- top-level `ok=false` or any required check is false;
- the synthetic state sequence differs from
  `V2_BUILDING,V2_READY,V2_ACTIVE,V1_ACTIVE`;
- production `readiness_status` is not `READY`, or its receipt contains any
  blocker;
- native coverage contains blocker entries;
- control `unchanged` is false;
- either disposable fixture was not cleaned;
- a source hash/byte count changes across backup or rehearsal.

There is no recovery command for the real workspace because this script never
writes it. A failed disposable run is recovered by confirming
`fixture_cleaned=true` and `source_fixture_cleaned=true`; otherwise remove only
the exact script-reported temporary path after verifying it is under the system
temporary directory. Never transition the real manifest based on this receipt.
