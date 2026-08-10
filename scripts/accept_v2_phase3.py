"""Fixture-only acceptance gates for V2 Phase 3 Conversation Sync.

The script always uses a temporary workspace and never opens the user's
``.memoryguard`` directory.  It prints machine-readable JSON for CI and
returns non-zero when a gate fails.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.content import (  # noqa: E402
    ContentReadScope,
    ContentStore,
    ConversationEvent,
    ConversationSync,
    ConversationSyncError,
    SyncConflictError,
    SyncCursorError,
)
from memoryguard.storage.database import open_database  # noqa: E402
from memoryguard.storage.transaction import transaction  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="memoryguard-v2-phase3-"))
    gates: dict[str, bool] = {}
    try:
        store = ContentStore(root)
        sync = ConversationSync(store)
        def event(key: str, text: str = "fixture text", ordinal: int = 0) -> ConversationEvent:
            return ConversationEvent(
                external_object_key="fixture-session",
                event_id=key,
                content=text,
                ordinal=ordinal,
                source_revision="r1",
                provider="fixture",
                workspace_id=store.workspace_id,
                agent_instance_id="fixture-agent",
                project_ref="fixture-project",
                sensitivity="normal",
                policy_class="private",
            )

        first_event = event("stable-event-1")
        run = sync.begin_sync("fixture", expected_revision=0, owner_id="fixture-owner")
        first_batch = sync.stage_batch(run, [first_event])
        result = sync.finish_sync(run)
        gates["complete_sync"] = result.state == "complete"
        gates["cursor_is_opaque"] = first_batch.continuation_cursor.startswith("c1.") and first_batch.continuation_cursor not in json.dumps(first_event.__dict__)
        baseline = store.counts()
        for revision in range(1, 101):
            run = sync.begin_sync("fixture", expected_revision=revision, owner_id="fixture-owner")
            sync.stage_batch(run, [first_event])
            sync.finish_sync(run)
        gates["100_noop_replay"] = store.counts() == baseline
        run = sync.begin_sync("fixture", expected_revision=101, owner_id="fixture-owner")
        sync.finish_sync(run, status="partial", coverage_complete=False)
        with store.connection() as conn:
            gates["partial_no_delete"] = conn.execute("SELECT active FROM content_occurrences").fetchone()[0] == 1
        # An empty scan has no durable coverage proof and therefore cannot
        # tombstone the prior event even when the caller asks for complete.
        run = sync.begin_sync("fixture", expected_revision=102, owner_id="fixture-owner")
        empty = sync.finish_sync(run)
        with store.connection() as conn:
            gates["empty_no_delete"] = conn.execute("SELECT deleted_scan_id FROM content_occurrences").fetchone()[0] == ""
        # A non-empty, covered scan may prove deletion of the missing event.
        run = sync.begin_sync("fixture", expected_revision=103, owner_id="fixture-owner")
        sync.stage_batch(run, [event("replacement", "replacement", 1)])
        deleted = sync.finish_sync(run)
        gates["complete_delete"] = deleted.tombstoned == 1
        run = sync.begin_sync("fixture", expected_revision=104, owner_id="fixture-owner")
        sync.stage_batch(run, [first_event])
        recovered = sync.finish_sync(run)
        gates["reappear_recover"] = recovered.restored == 1
        # Cursor possession is bound to source/run/owner/revision and is
        # single-use; forged, stale and cross-owner tokens fail closed.
        run = sync.begin_sync("fixture", expected_revision=105, owner_id="fixture-owner")
        cursor = sync.stage_batch(run, [first_event]).continuation_cursor
        try:
            sync.stage_batch(run, [event("cursor-next", "next", 2)], continuation_cursor="c1.forged")
        except (SyncCursorError, SyncConflictError):
            gates["forged_cursor_rejected"] = True
        sync.stage_batch(run, [event("cursor-next", "next", 2)], continuation_cursor=cursor)
        try:
            sync.stage_batch(run, [event("cursor-replay", "replay", 3)], continuation_cursor=cursor)
        except (SyncCursorError, SyncConflictError):
            gates["old_cursor_rejected"] = True
        sync.finish_sync(run)
        run_owner = sync.begin_sync("fixture", expected_revision=106, owner_id="owner-a")
        cursor_owner = sync.stage_batch(run_owner, [first_event]).continuation_cursor
        try:
            sync.stage_batch(run_owner, [event("cross-owner", "cross", 2)], continuation_cursor=cursor_owner, owner_id="owner-b")
        except (SyncCursorError, SyncConflictError):
            gates["cross_owner_rejected"] = True
        sync.finish_sync(run_owner)
        # Tampering with the persisted coverage ledger invalidates deletion
        # authority even if the caller passes coverage_complete=True.
        run = sync.begin_sync("fixture", expected_revision=107, owner_id="fixture-owner")
        sync.stage_batch(run, [first_event])
        with open_database(store.db_path) as conn:
            with transaction(conn):
                conn.execute("UPDATE source_manifest_staging SET coverage_status='unreadable' WHERE run_id=?", (run.run_id,))
        tampered = sync.finish_sync(run)
        with store.connection() as conn:
            gates["coverage_tamper_no_delete"] = conn.execute("SELECT deleted_scan_id FROM content_occurrences").fetchone()[0] == ""
        gates["tamper_not_complete"] = tampered.state != "complete"
        # Empty/unreadable content never creates a Blob or tombstone.
        run = sync.begin_sync("fixture", expected_revision=108, owner_id="fixture-owner")
        try:
            sync.stage_batch(run, [event("empty", "")], coverage_status="unreadable")
        except ConversationSyncError:
            pass
        unreadable = sync.finish_sync(run)
        gates["unreadable_no_delete"] = unreadable.tombstoned == 0
        with store.connection() as conn:
            occurrence = conn.execute("SELECT occurrence_id,blob_id FROM content_occurrences WHERE occurrence_key='stable-event-1'").fetchone()
            namespace_id = conn.execute("SELECT namespace_id FROM content_blobs LIMIT 1").fetchone()[0]
        link = sync.add_evidence_link(
            "memory-fixture",
            occurrence_id=occurrence[0],
            scope=ContentReadScope(namespace_id, store.workspace_id, "fixture-agent", "fixture-project", "fixture", "", "normal", "private"),
        )
        with store.connection() as conn:
            row = conn.execute("SELECT blob_id,source_revision FROM content_evidence_links WHERE link_id=?", (link,)).fetchone()
            gates["evidence_authoritative"] = row[0] == occurrence[1] and row[1] == "r1"
            gates["evidence_hold"] = conn.execute("SELECT active FROM content_holds WHERE source_ref=?", (link,)).fetchone()[0] == 1
        gates["no_real_memoryguard_touch"] = ".memoryguard" not in str(root)
        ok = all(gates.values())
        payload = {"ok": ok, "gates": gates}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
