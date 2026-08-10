from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.content import (
    ContentReadScope,
    ContentStore,
    ConversationEvent,
    ConversationSync,
    SyncConflictError,
    SyncCursorError,
)


def _event(store: ContentStore, key: str = "e1", text: str = "hello", ordinal: int = 0):
    return ConversationEvent(
        external_object_key="chat-1",
        event_id=key,
        content=text,
        ordinal=ordinal,
        provider="codex",
        workspace_id=store.workspace_id,
        agent_instance_id="agent-a",
        project_ref="project-x",
        sensitivity="normal",
        policy_class="private",
    )


def test_batches_have_no_total_turn_cap_and_cursor_is_opaque(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    run = sync.begin_sync("codex", expected_revision=0)
    events = [_event(store, f"e-{i}", f"turn-{i}", i) for i in range(10_001)]
    for start in range(0, len(events), 1000):
        result = sync.stage_batch(run, events[start : start + 1000], max_turns=1000, max_chars=100_000)
        assert result.continuation_cursor.startswith("c1.")
    sync.finish_sync(run)
    assert store.counts()["conversation_turns"] == 10_001


def test_noop_replay_and_same_text_occurrence_identity(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    first = [_event(store, "a", "same", 0), _event(store, "b", "same", 1)]
    run = sync.begin_sync("codex", expected_revision=0)
    sync.stage_batch(run, first); sync.finish_sync(run)
    baseline = store.counts()
    for revision in range(1, 101):
        run = sync.begin_sync("codex", expected_revision=revision)
        sync.stage_batch(run, first); sync.finish_sync(run)
    assert store.counts() == baseline
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM content_occurrences").fetchone()[0] == 2


def test_partial_does_not_delete_then_complete_delete_and_recover(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    event = _event(store)
    run = sync.begin_sync("codex", 0); sync.stage_batch(run, [event]); sync.finish_sync(run)
    run = sync.begin_sync("codex", 1); sync.finish_sync(run, status="partial", coverage_complete=False)
    with store.connection() as conn:
        assert conn.execute("SELECT active FROM content_occurrences").fetchone()[0] == 1
    # Empty coverage is not deletion authority, even when the caller asks for
    # a complete finish.
    run = sync.begin_sync("codex", 2); empty = sync.finish_sync(run)
    assert empty.tombstoned == 0
    run = sync.begin_sync("codex", 3); sync.stage_batch(run, [_event(store, "replacement", "replacement", 1)]); deleted = sync.finish_sync(run)
    assert deleted.tombstoned == 1
    run = sync.begin_sync("codex", 4); sync.stage_batch(run, [event]); restored = sync.finish_sync(run)
    assert restored.restored == 1
    with store.connection() as conn:
        assert tuple(conn.execute("SELECT active,deleted_scan_id FROM content_occurrences").fetchone()) == (1, "")


def test_cursor_is_server_bound_one_time_and_cross_owner_fails(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    run = sync.begin_sync("codex", 0, owner_id="owner-a")
    cursor = sync.stage_batch(run, [_event(store)]).continuation_cursor
    with pytest.raises(SyncCursorError):
        sync.stage_batch(run, [_event(store, "forged", "forged", 1)], continuation_cursor="c1.forged")
    sync.stage_batch(run, [_event(store, "next", "next", 1)], continuation_cursor=cursor)
    with pytest.raises(SyncCursorError):
        sync.stage_batch(run, [_event(store, "replay", "replay", 2)], continuation_cursor=cursor)
    sync.finish_sync(run)

    run = sync.begin_sync("codex", 1, owner_id="owner-a")
    cursor = sync.stage_batch(run, [_event(store)]).continuation_cursor
    with pytest.raises(SyncConflictError):
        sync.stage_batch(run, [_event(store, "cross", "cross", 1)], continuation_cursor=cursor, owner_id="owner-b")


def test_manifest_and_coverage_tamper_fail_closed(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    event = _event(store)
    run = sync.begin_sync("codex", 0)
    sync.stage_batch(run, [event])
    # A direct tamper cannot be hidden by the caller's coverage boolean; use a
    # writable connection only for this fixture mutation.
    from memoryguard.storage.database import open_database
    from memoryguard.storage.transaction import transaction
    with open_database(store.db_path) as conn:
        with transaction(conn):
            conn.execute("UPDATE source_manifest_staging SET coverage_status='unreadable' WHERE run_id=?", (run.run_id,))
    result = sync.finish_sync(run)
    assert result.state != "complete" and result.tombstoned == 0
    with store.connection() as conn:
        assert conn.execute("SELECT deleted_scan_id FROM content_occurrences").fetchone()[0] == ""


def test_stale_cas_and_old_owner_cannot_apply(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    run = sync.begin_sync("codex", 0, owner_id="one")
    with pytest.raises(SyncConflictError):
        sync.begin_sync("codex", 0, owner_id="two")
    sync.stage_batch(run, [_event(store)])
    sync.finish_sync(run)
    with pytest.raises(SyncConflictError):
        sync.begin_sync("codex", 0, owner_id="stale")


def test_evidence_pins_blob_and_source_revision_with_exact_acl(tmp_path: Path):
    store = ContentStore(tmp_path)
    sync = ConversationSync(store)
    event = _event(store); event = ConversationEvent(**{**event.__dict__, "source_revision": "r1"})
    run = sync.begin_sync("codex", 0); batch = sync.stage_batch(run, [event]); sync.finish_sync(run)
    with store.connection() as conn:
        turn_id = conn.execute("SELECT turn_id FROM conversation_turns").fetchone()[0]
        namespace_id = conn.execute("SELECT namespace_id FROM content_blobs").fetchone()[0]
    link = sync.add_evidence_link("memory-1", turn_id=turn_id, scope=ContentReadScope(namespace_id, store.workspace_id, "agent-a", "project-x", "codex", "", "normal", "private"))
    with store.connection() as conn:
        row = conn.execute("SELECT occurrence_id,blob_id,source_revision FROM content_evidence_links WHERE link_id=?", (link,)).fetchone()
        assert row[2] == "r1"
        assert conn.execute("SELECT COUNT(*) FROM content_holds WHERE blob_id=? AND source_ref=?", (row[1], link)).fetchone()[0] == 1
