# V2 Phase 3 Conversation Sync (shadow)

`memoryguard.content.ConversationSync` is the fixture/shadow implementation
of the Phase 3 conversation synchronization contract.  It does not activate
the legacy history importers; later wiring may feed normalized provider
events into this API.

## Contract

- `begin_sync(source_id, expected_revision=..., owner_id=...)` reserves a source
  with a SQL compare-and-swap.  Durable owner, expected-revision and manifest
  proof fields are checked on every batch and finish operation; a caller cannot
  substitute a run ID or another owner.
- `stage_batch` applies blobs, occurrences, turns, and staging manifest rows
  atomically.  `max_turns` and `max_chars` are per-batch budgets only.  The
  returned `c1.*` cursor is an unguessable one-time token.  Only its digest and
  server-side source/run/owner/revision/position/batch binding are persisted;
  raw cursor text is never stored.  Forged, stale and cross-run/owner cursors
  fail closed.  There is no lifetime session turn limit.
- Stable provider event IDs map to stable occurrence IDs.  Events without an
  ID receive an independent capture ID, even when their canonical text is
  identical.  Canonical blob storage still deduplicates equal text within a
  namespace.
- `finish_sync` never treats the caller's `coverage_complete` boolean as
  deletion authority.  Tombstoning requires an explicit complete request plus
  a non-empty staging ledger whose rows are all covered/readable, matching
  coverage and previous-manifest digests, and a successful owner/revision CAS.
  Partial, failed, unreadable, tampered or empty runs keep existing
  occurrences active.  A reappearing event reuses its occurrence and restores
  its tombstone.
- Evidence links persist `occurrence_id`, `blob_id`, and `source_revision`, and
  create an active content hold for the referenced blob.  Reads remain exact
  ACL/scope operations; the sync layer returns IDs and status, not snippets or
  unauthorized counts.

## Fixture acceptance

Run `python scripts/accept_v2_phase3.py`.  It creates and removes a temporary
fixture workspace, exercises complete/partial/empty/unreadable/tampered
coverage, cursor forgery/replay/cross-owner rejection, delete/recover,
ACL/evidence holds and 100 no-op replays, and prints
`{ "ok": bool, "gates": {...} }`.  It never opens the real `.memoryguard`
home.  Existing migration tests remain the compatibility check for
`ContentStore`; production history/runtime wiring is intentionally out of
scope for Phase 3.

## V1 history shadow bridge

`memoryguard.content.ConversationShadowBridge` is an explicit, failure-isolated
adapter for legacy history writes.  Callers inject an existing `ContentStore`
and pass `enabled=True`; the bridge never constructs a content database.  A
missing/unknown manifest or `V1_ACTIVE` keeps shadow disabled.  `V2_BUILDING`
and gated `V2_ACTIVE` permit shadow projection while V1 remains the primary
read/write path.

The bridge records pending/complete/failed operation receipts in an atomic JSON
outbox.  Replaying a stable event ID is idempotent; a projector failure marks a
stable diagnostic and a later retry can finish without changing the V1 result.
Long conversations are split into `max_turns`/`max_chars` batches and return a
continuation payload until complete.  Partial, failed and empty shadow runs do
not tombstone prior content.
