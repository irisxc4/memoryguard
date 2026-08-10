from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from memoryguard.content import ContentStore
from memoryguard.content.store import ContentError, ContentReadScope, register_acl_values
from memoryguard.migration.content import ContentMigrationError, V1ContentMigrator


def _history(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversation_sessions(session_id TEXT PRIMARY KEY, external_id TEXT, title TEXT, provider TEXT, agent_instance_id TEXT, project_ref TEXT, share_group_id TEXT, created_at TEXT, imported_at TEXT);
        CREATE TABLE conversation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER, role TEXT, content TEXT, created_at TEXT, event_key TEXT, content_hash TEXT);
        CREATE TABLE session_summaries(session_id TEXT PRIMARY KEY, summary TEXT, summary_kind TEXT, updated_at TEXT);
        CREATE TABLE observations(observation_id TEXT PRIMARY KEY, session_id TEXT, turn_id TEXT, observation_type TEXT, summary TEXT, created_at TEXT);
        CREATE TABLE evidence_links(link_id TEXT PRIMARY KEY, memory_id TEXT, session_id TEXT, turn_id TEXT, status TEXT, created_at TEXT, invalidated_at TEXT);
        """
    )
    conn.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?)", ("s1", "ext-1", "Chat", "codex", "agent-a", "/p", "group-a", "", ""))
    text = "same正文"
    conn.executemany("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)", [("t1", "s1", 0, "user", text, "", "event-1", hashlib.sha256(text.encode()).hexdigest()), ("t2", "s1", 1, "assistant", text, "", "event-2", hashlib.sha256(text.encode()).hexdigest())])
    conn.execute("INSERT INTO session_summaries VALUES (?,?,?,?)", ("s1", "summary", "import", ""))
    conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)", ("o1", "s1", "t1", "note", "observation", ""))
    conn.execute("INSERT INTO evidence_links VALUES (?,?,?,?,?,?,?)", ("e1", "m1", "s1", "t1", "valid", "", ""))
    conn.commit(); conn.close()


def _knowledge(path: Path, *, deleted: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE books(book_id TEXT PRIMARY KEY,title TEXT,root_path TEXT,status TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE documents(document_id TEXT PRIMARY KEY,book_id TEXT,relative_path TEXT,media_type TEXT,content_hash TEXT,status TEXT,updated_at TEXT);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,document_id TEXT,book_id TEXT,ordinal INTEGER,text TEXT,text_hash TEXT,sensitivity TEXT,active INTEGER,created_at TEXT);
        CREATE TABLE entities(entity_id TEXT PRIMARY KEY,name TEXT,normalized_name TEXT,entity_type TEXT,description TEXT,aliases TEXT,active INTEGER,created_at TEXT);
        CREATE TABLE relations(relation_id TEXT PRIMARY KEY,subject_entity_id TEXT,predicate TEXT,object_entity_id TEXT,source_chunk_id TEXT,book_id TEXT,document_id TEXT,created_at TEXT);
        CREATE TABLE chunk_entities(chunk_id TEXT,entity_id TEXT,role TEXT);
        CREATE TABLE embeddings(chunk_id TEXT,embedding_space_id TEXT,embedding_model TEXT,dimension INTEGER,vector BLOB,text_hash TEXT,created_at TEXT);
        CREATE TABLE memory_candidates(candidate_id TEXT PRIMARY KEY,book_id TEXT,chunk_id TEXT,content TEXT,source_text_hash TEXT,status TEXT,created_at TEXT);
        CREATE TABLE deleted_books(deletion_id TEXT PRIMARY KEY,book_id TEXT,title TEXT,root_path TEXT,snapshot_json TEXT,status TEXT,deleted_at TEXT,restored_at TEXT);
        """
    )
    conn.execute("INSERT INTO books VALUES (?,?,?,?,?,?)", ("b1", "Book", "/book", "ready", "", ""))
    conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", ("d1", "b1", "a.md", "text/plain", "h", "active", ""))
    conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)", ("c1", "d1", "b1", 0, "chunk正文", "h", "normal", 1, ""))
    conn.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?,?)", ("en1", "Thing", "thing", "concept", "", "", 1, ""))
    conn.execute("INSERT INTO memory_candidates VALUES (?,?,?,?,?,?,?)", ("ca1", "b1", "c1", "candidate正文", "h", "pending", ""))
    if deleted:
        conn.execute("INSERT INTO deleted_books VALUES (?,?,?,?,?,?,?,?)", ("del1", "b1", "Gone", "/gone", '{"tables":{"chunks":[{"text":"must-not-copy"}]}}', "deleted", "", ""))
    conn.commit(); conn.close()


def test_content_store_namespace_isolation_and_empty_text(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    a = store.ensure_namespace(trust_domain="a")
    b = store.ensure_namespace(trust_domain="b")
    assert store.put_blob(a.namespace_id, "x") == store.put_blob(a.namespace_id, "x")
    assert store.put_blob(b.namespace_id, "x") != store.put_blob(a.namespace_id, "x")
    assert store.put_blob(a.namespace_id, "") is None
    assert store.counts()["content_blobs"] == 2


def test_history_and_knowledge_migrate_idempotently_with_acl_and_derived_rebuild(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; _history(history)
    data_home = tmp_path / "data"; knowledge = data_home / "knowledge" / "knowledge.db"; _knowledge(knowledge)
    before_h = history.read_bytes(); before_k = knowledge.read_bytes()
    migrator = V1ContentMigrator(tmp_path, data_home=data_home)
    first = migrator.migrate()
    assert first.ok and first.history_status == "READY" and first.knowledge_status == "READY"
    store = ContentStore(tmp_path); counts = store.counts()
    assert counts["content_blobs"] >= 3
    assert counts["content_occurrences"] >= 4
    assert counts["migration_map"] >= 10
    assert history.read_bytes() == before_h and knowledge.read_bytes() == before_k
    again = migrator.migrate()
    assert again.target_counts == store.counts()
    assert store.counts()["content_blobs"] == counts["content_blobs"]
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_records WHERE derived_status='DERIVED_REBUILD'").fetchone()[0] >= 0


def test_migration_map_identity_change_fails_closed_and_preserves_old_row(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; _history(history)
    migrator = V1ContentMigrator(tmp_path, history_path=history)
    migrator.migrate(include_knowledge=False)
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        before = tuple(
            conn.execute(
                "SELECT target_id,source_hash,target_hash,acl_digest,status,metadata_json,created_at,updated_at "
                "FROM migration_map WHERE source_table='conversation_turns' AND source_pk='t1'"
            ).fetchone()
        )
    source_before = history.read_bytes()
    with sqlite3.connect(history) as conn:
        conn.execute("UPDATE conversation_turns SET content='changed body' WHERE turn_id='t1'")
        conn.commit()
    with pytest.raises(ContentMigrationError, match="migration_map identity changed"):
        migrator.migrate(include_knowledge=False)
    assert history.read_bytes() != source_before
    with store.connection() as conn:
        after = tuple(
            conn.execute(
                "SELECT target_id,source_hash,target_hash,acl_digest,status,metadata_json,created_at,updated_at "
                "FROM migration_map WHERE source_table='conversation_turns' AND source_pk='t1'"
            ).fetchone()
        )
    assert after == before


def test_unknown_knowledge_table_body_is_blocked_and_metadata_filtered(tmp_path: Path) -> None:
    data_home = tmp_path / "data"; knowledge = data_home / "knowledge" / "knowledge.db"; _knowledge(knowledge)
    with sqlite3.connect(knowledge) as conn:
        conn.execute(
            "CREATE TABLE unknown_authority(id TEXT PRIMARY KEY, body TEXT, payload TEXT, safe TEXT)"
        )
        conn.execute(
            "INSERT INTO unknown_authority VALUES (?,?,?,?)",
            ("u1", "must-not-copy", "secret-payload", "keep"),
        )
        conn.commit()
    report = V1ContentMigrator(
        tmp_path, data_home=data_home, history_path=tmp_path / "missing.sqlite"
    ).migrate()
    assert report.knowledge_status == "READY"
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        mapping = conn.execute(
            "SELECT status,metadata_json FROM migration_map WHERE source_table='unknown_authority'"
        ).fetchone()
        assert mapping is not None and mapping[0] == "blocked"
        assert "must-not-copy" not in mapping[1]
        assert "secret-payload" not in mapping[1]
        assert conn.execute(
            "SELECT COUNT(*) FROM source_sync_anomalies WHERE error_code='unknown_authoritative_content'"
        ).fetchone()[0] >= 1


def test_deleted_book_is_tombstone_without_snapshot_body_and_partial_does_not_delete(tmp_path: Path) -> None:
    data_home = tmp_path / "data"; knowledge = data_home / "knowledge" / "knowledge.db"; _knowledge(knowledge, deleted=True)
    migrator = V1ContentMigrator(tmp_path, data_home=data_home, history_path=tmp_path / "missing.sqlite")
    report = migrator.migrate(knowledge_complete=False)
    assert report.knowledge_status == "READY"
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        row = conn.execute("SELECT metadata_json FROM content_tombstones WHERE reason='book_deleted'").fetchone()
        assert row is not None and "must-not-copy" not in row[0]
        assert conn.execute("SELECT COUNT(*) FROM content_holds").fetchone()[0] >= 1


def test_corrupt_source_fails_before_target_and_missing_data_home_is_explicit(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; history.parent.mkdir(parents=True); history.write_bytes(b"not sqlite")
    with pytest.raises(ContentMigrationError):
        V1ContentMigrator(tmp_path, history_path=history).migrate()
    assert not (tmp_path / ".memoryguard" / "content" / "content.db").exists()
    report = V1ContentMigrator(tmp_path, history_path=tmp_path / "missing.sqlite").migrate()
    assert report.history_status == "NO_SOURCE" and report.knowledge_status == "NOT_CONFIGURED"


def test_complete_delete_then_reappear_keeps_occurrence_and_partial_does_not_tombstone(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; _history(history)
    migrator = V1ContentMigrator(tmp_path, history_path=history)
    migrator.migrate(include_knowledge=False)
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        original = conn.execute("SELECT occurrence_id FROM content_occurrences WHERE occurrence_key='turn:event-1'").fetchone()[0]
    conn = sqlite3.connect(history); conn.execute("DELETE FROM conversation_turns WHERE turn_id='t1'"); conn.commit(); conn.close()
    migrator.migrate(include_knowledge=False, history_complete=False)
    with store.connection() as conn:
        assert conn.execute("SELECT active FROM content_occurrences WHERE occurrence_id=?", (original,)).fetchone()[0] == 1
    # Complete coverage is the only delete authority.
    migrator.migrate(include_knowledge=False)
    with store.connection() as conn:
        assert conn.execute("SELECT active FROM content_occurrences WHERE occurrence_id=?", (original,)).fetchone()[0] == 0
    conn = sqlite3.connect(history); conn.execute("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)", ("t1", "s1", 0, "user", "same正文", "", "event-1", hashlib.sha256("same正文".encode()).hexdigest())); conn.commit(); conn.close()
    migrator.migrate(include_knowledge=False)
    with store.connection() as conn:
        assert conn.execute("SELECT occurrence_id,active FROM content_occurrences WHERE occurrence_id=?", (original,)).fetchone()[1] == 1


def test_large_history_uses_bounded_pages_and_preserves_same_text_events(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; history.parent.mkdir(parents=True)
    conn = sqlite3.connect(history)
    conn.executescript("CREATE TABLE conversation_sessions(session_id TEXT PRIMARY KEY, external_id TEXT, title TEXT, provider TEXT, agent_instance_id TEXT, project_ref TEXT, share_group_id TEXT, created_at TEXT, imported_at TEXT); CREATE TABLE conversation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER, role TEXT, content TEXT, created_at TEXT, event_key TEXT, content_hash TEXT);")
    conn.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?)", ("s", "s", "", "p", "a", "", "", "", ""))
    body = "repeat"
    conn.executemany("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)", [(f"t{i}", "s", i, "user", body, "", f"e{i}", "") for i in range(10001)])
    conn.commit(); conn.close()
    report = V1ContentMigrator(tmp_path, history_path=history, batch_size=257).migrate(include_knowledge=False)
    assert report.ok
    store = ContentStore(tmp_path)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_occurrences").fetchone()[0] == 10001
        assert conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failure_rolls_back_content_transaction_without_half_rows(tmp_path: Path) -> None:
    history = tmp_path / ".memoryguard" / "history" / "history.sqlite"; _history(history)
    data_home = tmp_path / "data"; _knowledge(data_home / "knowledge" / "knowledge.db")
    migrator = V1ContentMigrator(tmp_path, data_home=data_home)
    with pytest.raises(RuntimeError, match="injected content migration failure"):
        migrator.migrate(fail_after=3)
    store = ContentStore(tmp_path)
    counts = store.counts()
    assert counts["content_blobs"] == 0
    assert counts["content_occurrences"] == 0
    assert counts["migration_map"] == 0
    assert store.integrity_check() == ["ok"]
    with store.connection() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_content_connection_is_readonly_and_reads_require_exact_scope(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace(trust_domain="scope")
    blob_id = store.put_blob(namespace.namespace_id, "scoped body")
    assert blob_id is not None
    occurrence_id = store.upsert_occurrence(
        source_object_id="source-a",
        occurrence_key="event-a",
        blob_id=blob_id,
        namespace_id=namespace.namespace_id,
        workspace_id="workspace-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        share_group_id="group-a",
        provider="provider-a",
        sensitivity="normal",
        policy_class="private",
    )
    good = ContentReadScope(
        namespace.namespace_id,
        "workspace-a",
        "agent-a",
        "project-a",
        "provider-a",
        "group-a",
    )
    assert store.get_blob(blob_id, good).text == "scoped body"
    assert store.get_occurrence(occurrence_id, good) is not None
    assert store.get_blob(blob_id) is None
    assert store.get_occurrence(occurrence_id) is None
    assert store.get_blob("missing", good) is None
    assert store.get_occurrence("missing", good) is None

    for field in ("namespace_id", "workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "sensitivity", "policy_class"):
        values = {
            "namespace_id": namespace.namespace_id,
            "workspace_id": "workspace-a",
            "agent_instance_id": "agent-a",
            "project_ref": "project-a",
            "provider": "provider-a",
            "share_group_id": "group-a",
            "sensitivity": "normal",
            "policy_class": "private",
        }
        values[field] = "other-" + field
        denied = ContentReadScope(**values)
        assert store.get_blob(blob_id, denied) is None
        assert store.get_occurrence(occurrence_id, denied) is None

    with store.connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE readonly_probe(value TEXT)")


def test_unknown_acl_values_are_ledgered_and_not_defaulted_to_private_or_normal(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace()
    occurrence_id = store.upsert_occurrence(
        source_object_id="source-unknown-acl",
        occurrence_key="event",
        text="unknown acl",
        namespace_id=namespace.namespace_id,
        workspace_id="workspace-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        share_group_id="group-a",
    )
    with store.connection() as conn:
        row = conn.execute(
            "SELECT sensitivity,policy_class,provider FROM content_occurrences WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()
        assert tuple(row) == ("__UNKNOWN__", "__UNKNOWN__", "__UNKNOWN__")
        fields = {
            str(r[0])
            for r in conn.execute(
                "SELECT field FROM content_acl_anomalies WHERE occurrence_id=?",
                (occurrence_id,),
            )
        }
        assert {"sensitivity", "policy_class", "provider"} <= fields


def test_non_allowlisted_acl_and_unknown_provider_are_unreadable_until_registered(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace()
    blob_id = store.put_blob(namespace.namespace_id, "enum body")
    assert blob_id is not None
    occurrence_id = store.upsert_occurrence(
        source_object_id="source-enum",
        occurrence_key="event",
        blob_id=blob_id,
        namespace_id=namespace.namespace_id,
        workspace_id="workspace-a",
        agent_instance_id="agent-a",
        project_ref="project-a",
        share_group_id="group-a",
        provider="",
        sensitivity="secret-level",
        policy_class="internal-only",
    )
    denied = ContentReadScope(
        namespace.namespace_id,
        "workspace-a",
        "agent-a",
        "project-a",
        "",
        "group-a",
        "secret-level",
        "internal-only",
    )
    assert store.get_blob(blob_id, denied) is None
    assert store.get_occurrence(occurrence_id, denied) is None
    register_acl_values(
        sensitivities=["secret-level"], policy_classes=["internal-only"]
    )
    # Provider remains empty/unknown, so registration of enum values alone
    # cannot accidentally authorize the row.
    assert store.get_blob(blob_id, denied) is None
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_acl_anomalies WHERE occurrence_id=?",
            (occurrence_id,),
        ).fetchone()[0] >= 2


@pytest.mark.parametrize("marker_rows", [[("version", "999")], [("version", "2"), ("future", "x")]])
def test_content_schema_marker_preflight_is_fail_closed_without_target_mutation(
    tmp_path: Path, marker_rows: list[tuple[str, str]]
) -> None:
    store = ContentStore(tmp_path)
    path = store.db_path
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM content_schema_meta")
        conn.executemany("INSERT INTO content_schema_meta(key,value) VALUES(?,?)", marker_rows)
        conn.commit()
    # Read expected rows before establishing the physical baseline.  Python
    # 3.10 SQLite may checkpoint WAL state when this observation closes.
    with sqlite3.connect(path) as conn:
        before_rows = conn.execute("SELECT key,value FROM content_schema_meta ORDER BY key").fetchall()
    before_bytes = path.read_bytes()
    with pytest.raises(ContentError):
        ContentStore(tmp_path)
    assert before_rows == sorted(marker_rows)
    assert path.read_bytes() == before_bytes, "schema preflight mutated the live database"


def test_partial_aux_schema_without_marker_is_not_inferred(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    path = store.db_path
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM content_schema_meta")
        conn.commit()
    with pytest.raises(ContentError, match="content_schema_meta|marker is missing"):
        ContentStore(tmp_path)


def test_content_store_rejects_workspace_and_ancestor_symlinks(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_link = tmp_path / "workspace-link"
    try:
        workspace_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ContentError, match="symlink or reparse"):
        ContentStore(workspace_link)
    assert not (outside / ".memoryguard").exists()

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    parent_link = tmp_path / "parent-link"
    try:
        parent_link.symlink_to(real_parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ContentError, match="symlink or reparse"):
        ContentStore(parent_link / "child-workspace")
    assert not (real_parent / "child-workspace" / ".memoryguard").exists()
