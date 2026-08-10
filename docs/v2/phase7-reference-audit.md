# Phase 7 Reference Audit

`ReferenceAudit` is a read-only control-plane scan over the twelve explicit V2
authoritative domains: runtime, memory, rules, evidence, content, knowledge,
codegraph, assets, scenario, profile, system and skills.  The registry owns the
fixed lexical database path, supported marker/version, exact table and column
allow-list, JSON/reference keys and outbox/ledger tables.

The scanner opens existing SQLite files with `mode=ro` and never constructs a
domain Store.  Missing or partial databases, future/unsupported markers,
unknown authoritative tables/columns, malformed or unknown reference JSON,
integrity/FK failures, blocked migration rows, unknown-ledger rows, dangling
logical references and pending/failed/unconsumed outbox rows are blockers.  A
read-only run does not create `.memoryguard`, follow symlinks/junctions, write
source bytes or change a schema.

Every page is keyset-paginated and bound to the current schema fingerprint;
tampered or stale cursors fail closed.  Results include the registry digest,
per-domain schema fingerprints and system manifest generation for CAS callers.

Two epochs may be compared.  The result exposes each epoch's candidate set and
their intersection, but this phase has no physical deletion capability:
`sweep.capability` is always `false`, reason is
`hold_first_not_proven`, and deleted count is always zero.  A later executor
must prove holds, quiescence, lease, generation and outbox safety before any
physical operation.
