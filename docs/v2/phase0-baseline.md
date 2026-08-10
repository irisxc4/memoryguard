# Phase 0 baseline, measurements, and Golden Queries

Phase 0 supplies a repeatable audit tool. It does not claim migration savings
or represent a production-scale report until a user explicitly chooses a
workspace.

Run the deterministic fixture gate:

```text
python scripts/accept_v2_phase0.py
python -m pytest tests/test_v2_phase0_acceptance.py -q
```

The default fixture uses the real `ContentStore` and `ConversationSync` in a
temporary directory. It executes these behavior checks, rather than merely
listing their names:

- exact ACL allow/deny and evidence scope denial;
- same text in two event identities (one Blob, two occurrences);
- stable event replay and 100 no-op replays with zero content growth;
- partial, failed/unreadable, and empty scans never tombstone;
- complete delete followed by complete recovery;
- 10,001 turns through server-issued continuation cursors;
- evidence pinned to Blob ID and source revision, with an active hold;
- SQLite integrity check and table-count baseline.

The report has fixed top-level keys: `source_inventory`, `acl_scenarios`,
`golden_queries`, `metrics`, `baseline_digest`, `failures`, `gates`, and `ok`.
The digest covers stable fixture observations only; wall-clock performance
measurements are reported separately and therefore do not make the digest
change between runs.

For a real workspace, use metadata-only mode:

```text
python scripts/accept_v2_phase0.py --workspace <workspace>
```

This mode opens existing SQLite files read-only, reports file/page/WAL/index
metadata and table row counts, and never creates or modifies `.memoryguard`.
Missing storage is an explicit `NOT_CONFIGURED` result. It emits no source
正文 or other content. A production baseline must be generated only after the
workspace owner selects the scope and confirms its permissions.
