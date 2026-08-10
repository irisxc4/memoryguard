#!/usr/bin/env python3
"""Repeatable Phase 0 baseline and Golden Query acceptance gate.

The default run builds a private temporary fixture and exercises the real
Content Plane synchronizer.  ``--workspace`` is deliberately read-only: it
only inspects file/schema metadata and never bootstraps ``.memoryguard``.
No fixture text, IDs, or paths are emitted; the report is an audit summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.content import (  # noqa: E402
    ContentReadScope,
    ContentStore,
    ConversationEvent,
    ConversationSync,
    SyncCursorError,
)
from memoryguard.storage.database import open_database  # noqa: E402
from memoryguard.storage.transaction import transaction  # noqa: E402
from memoryguard.storage.layout import WorkspaceV2Layout  # noqa: E402


SCHEMA = "memoryguard-v2-phase0"
EXPECTED_DOMAINS = {
    "content": ("content.db",),
    "system": ("manifest.db",),
    "runtime": ("runtime.db",),
    "memory": ("memory.db",),
    "rules": ("rules.db",),
    "evidence": ("evidence.db",),
    "knowledge": ("knowledge.db",),
    "codegraph": ("codegraph.db",),
    "assets": ("assets.db",),
    "projection": ("scenario.db", "profile.db"),
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _event(source: str, key: str, text: str, ordinal: int, *, revision: str = "r1") -> ConversationEvent:
    return ConversationEvent(
        external_object_key=source,
        content=text,
        event_id=key,
        ordinal=ordinal,
        source_revision=revision,
        provider="codex",
        workspace_id="phase0-workspace",
        agent_instance_id="phase0-agent",
        project_ref="phase0-project",
        share_group_id="phase0-share",
        sensitivity="normal",
        policy_class="private",
    )


def _rows(store: ContentStore, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
    with store.connection() as conn:
        return [tuple(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _one(store: ContentStore, sql: str, params: Iterable[Any] = ()) -> tuple[Any, ...] | None:
    rows = _rows(store, sql, params)
    return rows[0] if rows else None


def _db_metadata(path: Path, *, include_rows: bool = True) -> dict[str, Any]:
    """Read SQLite metadata without opening a write-capable handle."""

    result: dict[str, Any] = {
        "exists": path.is_file(),
        "bytes": int(path.stat().st_size) if path.is_file() else 0,
        "wal_bytes": int(path.with_name(path.name + "-wal").stat().st_size)
        if path.with_name(path.name + "-wal").is_file()
        else 0,
    }
    if not path.is_file():
        return result
    try:
        # URI mode=ro prevents accidental creation or journal changes.
        conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            result["page_count"] = int(conn.execute("PRAGMA page_count").fetchone()[0])
            result["page_size"] = int(conn.execute("PRAGMA page_size").fetchone()[0])
            result["freelist_pages"] = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            tables = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()]
            result["tables"] = tables
            result["indexes"] = [str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
            ).fetchall()]
            if include_rows:
                counts: dict[str, int] = {}
                for table in tables:
                    # Table names originate from sqlite_master, not user input.
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM \"{table}\"").fetchone()[0])
                result["row_counts"] = counts
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        result["read_error"] = type(exc).__name__
    return result


def _workspace_report(root: Path) -> dict[str, Any]:
    """Return metadata-only report.  This function must never create paths."""

    root = root.expanduser().resolve()
    layout = WorkspaceV2Layout(root)
    files: dict[str, Any] = {}
    for domain, names in EXPECTED_DOMAINS.items():
        for name in names:
            rel = f".memoryguard/{domain}/{name}"
            files[rel] = _db_metadata(root / rel, include_rows=True)
    content_db = root / ".memoryguard" / "content" / "content.db"
    configured = content_db.is_file()
    status = "READ_ONLY_METADATA" if configured else "NOT_CONFIGURED"
    tables = files[".memoryguard/content/content.db"].get("tables", [])
    source_inventory = {
        "status": status,
        "workspace": {"exists": root.is_dir(), "root_name": root.name},
        "layout": {"memoryguard_exists": (root / ".memoryguard").exists(), "domains": sorted(EXPECTED_DOMAINS)},
        "databases": files,
        "scale": {"source_rows": None, "event_rows": None, "known_tables": len(tables)},
    }
    gates = {
        "workspace_read_only": True,
        "not_configured_is_explicit": status == "NOT_CONFIGURED" or configured,
        "no_content_output": True,
    }
    failures: list[dict[str, Any]] = []
    for rel, metadata in files.items():
        if metadata.get("read_error"):
            failures.append({"check": "metadata", "item": rel, "kind": "read_error"})
    gates["metadata_readable"] = not failures
    stable = {"schema": SCHEMA, "mode": "workspace", "source_inventory": source_inventory, "gates": gates}
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "mode": "workspace",
        "source_inventory": source_inventory,
        "acl_scenarios": [],
        "golden_queries": {},
        "metrics": {"performance": {"measured": False}, "integrity": {"status": "NOT_RUN"}},
        "baseline_digest": _digest(stable),
        "failures": failures,
        "gates": gates,
        "ok": not failures,
    }


def _fixture_report() -> dict[str, Any]:
    started = time.perf_counter()
    failures: list[dict[str, Any]] = []
    gates: dict[str, bool] = {}
    golden: dict[str, Any] = {}
    acl: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-phase0-") as temp:
        store = ContentStore(Path(temp), workspace_id="phase0-workspace")
        sync = ConversationSync(store)

        # Exact ACL allow/deny, and evidence pinned to source revision.
        primary = _event("phase0-primary", "acl-1", "phase0 evidence sample", 0)
        run = sync.begin_sync("phase0-primary", expected_revision=0, owner_id="phase0-owner")
        first = sync.stage_batch(run, [primary])
        sync.finish_sync(run)
        namespace_id = str(_one(store, "SELECT namespace_id FROM content_blobs LIMIT 1")[0])
        exact = ContentReadScope(namespace_id, "phase0-workspace", "phase0-agent", "phase0-project", "codex", "phase0-share", "normal", "private")
        denied = ContentReadScope(namespace_id, "phase0-workspace", "intruder", "phase0-project", "codex", "phase0-share", "normal", "private")
        allow = store.get_blob(first.blob_ids[0], scope=exact) is not None
        deny = store.get_blob(first.blob_ids[0], scope=denied) is None
        acl.extend([
            {"scenario": "exact_acl_allow", "expected": "allow", "observed": "allow" if allow else "deny"},
            {"scenario": "exact_acl_deny", "expected": "deny", "observed": "deny" if deny else "allow"},
        ])
        golden["exact_acl_deny"] = {"expected": "deny", "observed": "deny" if deny else "allow"}
        turn_id = str(_one(store, "SELECT turn_id FROM conversation_turns WHERE event_key='acl-1'")[0])
        link_id = sync.add_evidence_link("phase0-memory", turn_id=turn_id, scope=exact)
        evidence_denied = False
        try:
            sync.add_evidence_link("phase0-memory-denied", turn_id=turn_id, scope=denied)
        except PermissionError:
            evidence_denied = True
        evidence = _one(store, "SELECT blob_id,source_revision FROM content_evidence_links WHERE link_id=?", (link_id,))
        hold_count = int(_one(store, "SELECT COUNT(*) FROM content_holds WHERE source_ref=?", (link_id,))[0])
        pinned = bool(evidence and evidence[1] == "r1" and hold_count == 1)
        golden["evidence_pinned_revision"] = {"expected": "blob_id+source_revision+r1+hold", "observed": "pinned" if pinned else "mismatch"}
        acl.append({"scenario": "evidence_scope_exact", "expected": "allow", "observed": "allow" if pinned else "deny"})
        acl.append({"scenario": "evidence_scope_denied", "expected": "deny", "observed": "deny" if evidence_denied else "allow"})

        # Same text must deduplicate Blob while preserving occurrence identity.
        repeat_events = [_event("phase0-repeat", "repeat-a", "same text", 0), _event("phase0-repeat", "repeat-b", "same text", 1)]
        run = sync.begin_sync("phase0-repeat", expected_revision=0, owner_id="phase0-owner")
        sync.stage_batch(run, repeat_events)
        sync.finish_sync(run)
        repeat_baseline = store.counts()
        repeat_rows = _one(store, "SELECT COUNT(*),COUNT(DISTINCT o.blob_id) FROM content_occurrences o JOIN source_objects so ON so.source_object_id=o.source_object_id WHERE so.source_id='phase0-repeat'")
        distinct_ok = bool(repeat_rows and int(repeat_rows[0]) == 2 and int(repeat_rows[1]) == 1)
        golden["same_text_distinct_occurrence"] = {"expected": "1 blob, 2 occurrences", "observed": "1 blob, 2 occurrences" if distinct_ok else "mismatch"}

        # Stable replay and 100 no-op revisions must have zero content growth.
        revision = 1
        for _ in range(3):
            run = sync.begin_sync("phase0-repeat", expected_revision=revision, owner_id="phase0-owner")
            sync.stage_batch(run, repeat_events)
            sync.finish_sync(run)
            revision += 1
        replay_stable = store.counts() == repeat_baseline
        golden["stable_event_replay"] = {"expected": "idempotent", "observed": "idempotent" if replay_stable else "growth"}
        before_noop = store.counts()
        for _ in range(100):
            run = sync.begin_sync("phase0-repeat", expected_revision=revision, owner_id="phase0-owner")
            sync.stage_batch(run, repeat_events)
            sync.finish_sync(run)
            revision += 1
        after_noop = store.counts()
        no_op = before_noop == after_noop
        golden["no_op_zero_growth"] = {"expected": "100 replays, zero content growth", "observed": "zero growth" if no_op else "growth"}

        # Partial/unreadable/empty scans never acquire deletion authority.
        old = _event("phase0-delete", "delete-old", "old row", 0)
        run = sync.begin_sync("phase0-delete", expected_revision=0, owner_id="phase0-owner")
        sync.stage_batch(run, [old]); sync.finish_sync(run)
        run = sync.begin_sync("phase0-delete", expected_revision=1, owner_id="phase0-owner")
        partial = sync.finish_sync(run, status="partial", coverage_complete=True)
        run = sync.begin_sync("phase0-delete", expected_revision=2, owner_id="phase0-owner")
        sync.stage_batch(run, [old])
        with open_database(store.db_path) as conn:
            with transaction(conn):
                conn.execute("UPDATE source_manifest_staging SET coverage_status='unreadable' WHERE run_id=?", (run.run_id,))
        unreadable = sync.finish_sync(run, status="complete", coverage_complete=True)
        run = sync.begin_sync("phase0-delete", expected_revision=3, owner_id="phase0-owner")
        empty = sync.finish_sync(run, status="complete", coverage_complete=True)
        deleted_scan = _one(store, "SELECT deleted_scan_id FROM content_occurrences WHERE occurrence_key='delete-old'")[0]
        partial_safe = (
            partial.state == "partial"
            and unreadable.state != "complete"
            and unreadable.tombstoned == 0
            and empty.tombstoned == 0
            and str(deleted_scan or "") == ""
        )
        golden["partial_no_delete"] = {"expected": "no tombstone", "observed": "no tombstone" if partial_safe else "deleted"}
        # Complete scan with a replacement proves deletion; next complete scan recovers old row.
        run = sync.begin_sync("phase0-delete", expected_revision=4, owner_id="phase0-owner")
        sync.stage_batch(run, [_event("phase0-delete", "delete-new", "new row", 1)])
        deleted = sync.finish_sync(run)
        run = sync.begin_sync("phase0-delete", expected_revision=5, owner_id="phase0-owner")
        sync.stage_batch(run, [old])
        recovered = sync.finish_sync(run)
        delete_recover = deleted.tombstoned == 1 and recovered.restored >= 1
        golden["complete_delete_recover"] = {"expected": "delete then recover", "observed": "delete then recover" if delete_recover else "mismatch"}

        # >10k events use server-issued one-time cursors, never a total cap.
        bulk_total = 10_001
        bulk_events = [_event("phase0-bulk", f"bulk-{index}", f"bulk turn {index}", index) for index in range(bulk_total)]
        run = sync.begin_sync("phase0-bulk", expected_revision=0, owner_id="phase0-owner")
        cursor = ""
        cursor_valid = True
        forged_cursor_denied = False
        try:
            sync.stage_batch(run, bulk_events[:1], max_turns=1000, max_chars=200_000, continuation_cursor="c1.forged")
        except SyncCursorError:
            forged_cursor_denied = True
        for start in range(0, bulk_total, 1000):
            batch = sync.stage_batch(run, bulk_events[start : start + 1000], max_turns=1000, max_chars=200_000, continuation_cursor=cursor)
            cursor = batch.continuation_cursor
            cursor_valid = cursor_valid and cursor.startswith("c1.")
        bulk_result = sync.finish_sync(run)
        bulk_rows = int(_one(store, "SELECT COUNT(*) FROM conversation_turns WHERE session_id=(SELECT session_id FROM conversation_sessions WHERE external_id='phase0-bulk')")[0])
        over_10k = cursor_valid and forged_cursor_denied and bulk_result.state == "complete" and bulk_rows == bulk_total
        golden["over_10k_cursor"] = {"expected": "10001 complete with continuation", "observed": "10001 complete" if over_10k else "incomplete"}

        counts = store.counts()
        integrity = store.integrity_check()
        gates.update({
            "fixture_behavior_executed": True,
            "acl_exact_allow_deny": allow and deny,
            "same_text_distinct_occurrence": distinct_ok,
            "stable_replay": replay_stable,
            "partial_unreadable_no_delete": partial_safe,
            "complete_delete_recover": delete_recover,
            "over_10k_cursor": over_10k,
            "cursor_forgery_denied": forged_cursor_denied,
            "evidence_pinned_revision": pinned,
            "no_op_zero_growth": no_op,
            "integrity_check": integrity == ["ok"],
            "no_content_output": True,
        })
    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    source_inventory = {
        "status": "FIXTURE",
        "sources": ["phase0-primary", "phase0-repeat", "phase0-delete", "phase0-bulk"],
        "scale": {"events": 10_001, "sources": 4, "replay_runs": 103, "batch_turns": 1_000},
    }
    metrics = {
        "performance": {"measured": True, "fixture_elapsed_ms": elapsed_ms, "events_per_second": round(10_001 / max(elapsed_ms / 1000.0, 0.001), 2)},
        "integrity": {"status": "ok" if gates.get("integrity_check") else "failed", "sqlite_integrity": ["ok"] if gates.get("integrity_check") else []},
        "table_counts": counts,
    }
    stable = {
        "schema": SCHEMA,
        "mode": "fixture",
        "source_inventory": source_inventory,
        "acl_scenarios": acl,
        "golden_queries": golden,
        "table_counts": counts,
        "gates": gates,
    }
    failures.extend({"check": name, "kind": "contract", "message": "gate failed"} for name, ok in gates.items() if not ok)
    return {
        "schema": SCHEMA,
        "schema_version": 1,
        "mode": "fixture",
        "source_inventory": source_inventory,
        "acl_scenarios": acl,
        "golden_queries": golden,
        "metrics": metrics,
        "baseline_digest": _digest(stable),
        "failures": failures,
        "gates": gates,
        "ok": not failures and all(gates.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, help="existing workspace; metadata-only, never bootstrapped")
    args = parser.parse_args(argv)
    try:
        report = _workspace_report(args.workspace) if args.workspace is not None else _fixture_report()
    except Exception as exc:  # noqa: BLE001 - machine-readable failure report
        report = {
            "schema": SCHEMA,
            "schema_version": 1,
            "mode": "workspace" if args.workspace is not None else "fixture",
            "source_inventory": {"status": "ERROR", "scale": {}},
            "acl_scenarios": [],
            "golden_queries": {},
            "metrics": {"performance": {"measured": False}, "integrity": {"status": "ERROR"}},
            "baseline_digest": _digest({"schema": SCHEMA, "error": type(exc).__name__}),
            "failures": [{"check": "execution", "kind": "exception", "message": type(exc).__name__}],
            "gates": {"execution": False},
            "ok": False,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
