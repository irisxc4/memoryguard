# Phase 9 — Native Rule Merge Governance

`NativeRuleMergeService` owns the native boundary for:

- `memoryguard_rule_merge_capability_issue`
- `memoryguard_rule_merge_approve`
- `memoryguard_rule_merge_acknowledge`
- `memoryguard_rule_merge_cooldown_clear`

Every mutation resolves the process-issued transport authority, reconstructs a
trusted `AccessContext`, requires an admin/trusted session, checks `V2_ACTIVE`
and a strict manifest generation against the injected state provider, and binds
the request to a receipt, idempotency key, capability token and (for approval)
the exact proposal definition revisions. Errors are stable codes only; bearer
tokens, paths, SQL and exception text are never returned.

The `rule_merge_native_requests` ledger is created by the authoritative
`RuleMergeStore` schema (new reservations use schema version 2; version-1
records remain readable for fail-closed compatibility). Reservation,
capability consumption and the rule mutation share one re-entrant SQLite write
transaction. Missing,
partial, future-version, unsafe/reparse and orphaned-pending stores fail closed
without writes. Approve/acknowledge/cooldown-clear replays are durable and
token-free.

Capability issuance is replay-safe when the client supplies a canonical,
unpadded base64url `recovery_secret` decoding to at least 32 bytes. The ledger
stores only `SHA-256(recovery_secret)` and a SHA-256 bearer fingerprint. The
bearer is `HMAC-SHA256(secret, canonical(version, workspace, principal,
proposal, request_key, manifest_generation, scope))`, encoded as base64url.
After a lost response or process restart, the same request key and secret
reconstruct the original bearer and return it without creating another grant.
Consumed, expired, revoked, random/v1 capabilities, wrong/weak/missing
secrets, cross-context bindings, and drifted/orphaned pending rows fail closed.

## Native activation

All four MCP operations are implemented by `NativeV2RuntimePort` through the
same `NativeRuleMergeService`; no legacy route or service override is used.
The capability issuance replay blocker is cleared only after deterministic
secret-binding, restart/concurrency, terminal-token, pending-state, and
plaintext-redaction tests pass.
