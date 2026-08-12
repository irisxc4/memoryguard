from __future__ import annotations

from pathlib import Path
import sqlite3

from memoryguard.content import ContentStore


def _one_occurrence(tmp_path: Path) -> tuple[ContentStore, str, str]:
    store = ContentStore(
        tmp_path,
        workspace_id=str(tmp_path.resolve()),
        trust_domain="knowledge:test",
        sensitivity="normal",
        retention_authority="workspace",
    )
    ns = store.ensure_namespace()
    blob_id = store.put_blob("knowledge body", namespace_id=ns.namespace_id)
    assert blob_id
    occurrence_id = store.upsert_occurrence(
        source_object_id="source-object-1",
        occurrence_key="block:0",
        blob_id=blob_id,
        namespace_id=ns.namespace_id,
        source_id="source-1",
        source_kind="knowledge",
        external_object_key="book/file.md",
        object_type="document",
        source_revision="rev-1",
        ordinal=0,
        locator={"path": "file.md", "line_start": 1, "line_end": 1},
        content_role="knowledge",
        sensitivity="normal",
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref="project-a",
        share_group_id="group-a",
        policy_class="private",
        provider="local",
        access_scope={"mode": "knowledge"},
    )
    return store, occurrence_id, blob_id


def test_restore_tombstone_reactivates_occurrence_and_releases_hold(tmp_path: Path) -> None:
    store, occurrence_id, blob_id = _one_occurrence(tmp_path)
    tombstone_id = store.tombstone_occurrence(
        occurrence_id,
        reason="knowledge_remove",
        scan_id="remove-1",
        metadata={"asset_id": "book-1"},
    )

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT active FROM content_occurrences WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT active FROM content_holds WHERE blob_id=? AND source_ref=?",
            (blob_id, occurrence_id),
        ).fetchone()[0] == 1

    restored = store.restore_tombstone(tombstone_id)
    assert restored["occurrence_id"] == occurrence_id
    assert restored["blob_id"] == blob_id

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT active,deleted_scan_id FROM content_occurrences WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone() == (1, "")
        assert conn.execute(
            "SELECT active FROM content_tombstones WHERE tombstone_id=?",
            (tombstone_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT active FROM content_holds WHERE blob_id=? AND source_ref=?",
            (blob_id, occurrence_id),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE blob_id=?",
            (blob_id,),
        ).fetchone()[0] == 1


def test_purge_tombstone_releases_hold_but_does_not_delete_blob(tmp_path: Path) -> None:
    store, occurrence_id, blob_id = _one_occurrence(tmp_path)
    tombstone_id = store.tombstone_occurrence(
        occurrence_id,
        reason="knowledge_remove",
        scan_id="remove-2",
        metadata={"asset_id": "book-1"},
    )

    purged = store.purge_tombstone(tombstone_id)
    assert purged["occurrence_id"] == occurrence_id

    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute(
            "SELECT active FROM content_occurrences WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT active FROM content_tombstones WHERE tombstone_id=?",
            (tombstone_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT active FROM content_holds WHERE blob_id=? AND source_ref=?",
            (blob_id, occurrence_id),
        ).fetchone()[0] == 0
        # Physical orphan reclamation belongs to guarded maintenance.
        assert conn.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE blob_id=?",
            (blob_id,),
        ).fetchone()[0] == 1
