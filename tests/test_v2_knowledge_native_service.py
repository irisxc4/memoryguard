from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from memoryguard.content import ContentReadScope, ContentStore
from memoryguard.knowledge_v2 import (
    KNOWLEDGE_CANDIDATE_META,
    KNOWLEDGE_CANDIDATE_SCHEMA,
    KNOWLEDGE_CANDIDATE_TABLE,
    KnowledgeV2ReadonlyService,
)
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_database


def _scope(root: Path, *, agent: str = "agent-a") -> ContentReadScope:
    return ContentReadScope(
        namespace_id="ns-knowledge",
        workspace_id=str(root.resolve()),
        agent_instance_id=agent,
        project_ref="project-a",
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
    )


def _content_fixture(root: Path) -> tuple[ContentStore, ContentReadScope, str]:
    store = ContentStore(root)
    namespace = store.ensure_namespace(namespace_id="ns-knowledge", trust_domain="knowledge")
    blob = store.put_blob(namespace.namespace_id, "SECRET BODY MUST NOT ESCAPE")
    assert blob
    scope = _scope(root)
    occurrence = store.upsert_occurrence(
        source_object_id="knowledge-source",
        occurrence_key="book-1",
        blob_id=blob,
        namespace_id=namespace.namespace_id,
        workspace_id=scope.workspace_id,
        agent_instance_id=scope.agent_instance_id,
        project_ref=scope.project_ref,
        provider=scope.provider,
        share_group_id=scope.share_group_id,
        sensitivity=scope.sensitivity,
        policy_class=scope.policy_class,
        locator={"title": "Visible title", "body": "blocked"},
    )
    return store, scope, occurrence


def test_book_service_is_exact_scope_reference_only_and_zero_write(tmp_path: Path) -> None:
    store, scope, occurrence = _content_fixture(tmp_path)
    service = KnowledgeV2ReadonlyService(tmp_path)
    db_before = store.db_path.read_bytes()
    wal_path = Path(str(store.db_path) + "-wal")
    wal_before = wal_path.read_bytes() if wal_path.exists() else None
    result = service.dispatch("memoryguard_knowledge_book", {}, scope=scope)
    assert result["ok"] is True
    assert result["status"] == "READY"
    assert result["total"] == 1
    item = result["references"][0]
    assert item == {
        "summary": "Visible title",
        "ref": occurrence,
        "hash": hashlib.sha256("SECRET BODY MUST NOT ESCAPE".encode()).hexdigest(),
        "trust": "reference_only",
    }
    assert not any(key in item for key in ("body", "text", "content", "scope"))
    assert service.book(_scope(tmp_path, agent="other-agent")) == ()
    assert store.db_path.read_bytes() == db_before
    wal_after = wal_path.read_bytes() if wal_path.exists() else None
    assert wal_after == wal_before


def test_book_missing_db_fails_closed_without_creating_workspace(tmp_path: Path) -> None:
    service = KnowledgeV2ReadonlyService(tmp_path)
    result = service.dispatch("memoryguard_knowledge_book", {}, scope=_scope(tmp_path))
    assert result == {
        "ok": False,
        "status": "BLOCKED",
        "service": "knowledge_book",
        "code": "content_db_missing",
        "error": "content_db_missing",
    }
    assert not (tmp_path / ".memoryguard").exists()


def test_book_future_aux_schema_fails_closed(tmp_path: Path) -> None:
    store, scope, _occurrence = _content_fixture(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE content_schema_meta SET value='999' WHERE key='version'")
    result = KnowledgeV2ReadonlyService(tmp_path).dispatch(
        "book", {}, scope=scope
    )
    assert result["ok"] is False
    assert result["status"] == "BLOCKED"
    assert result["code"] == "content_schema_future"


def test_candidates_require_explicit_v2_schema_and_acl(tmp_path: Path) -> None:
    # The V2 knowledge domain is initialized explicitly by the fixture; the
    # service itself remains read-only and never creates this plane.
    layout = WorkspaceV2Layout(tmp_path)
    initialize_database(layout.knowledge_db, "knowledge", layout=layout)
    scope = _scope(tmp_path)
    with sqlite3.connect(layout.knowledge_db) as conn:
        conn.executescript(KNOWLEDGE_CANDIDATE_SCHEMA)
        conn.execute(
            f"INSERT INTO knowledge_v2_schema_meta(key,value) VALUES ('version','1')"
        )
        conn.execute(
            f"INSERT INTO {KNOWLEDGE_CANDIDATE_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-1",
                scope.namespace_id,
                scope.workspace_id,
                scope.agent_instance_id,
                scope.project_ref,
                scope.provider,
                scope.share_group_id,
                scope.sensitivity,
                scope.policy_class,
                "pending",
                "Candidate summary",
                "occurrence-ref",
                "hash-1",
                "occurrence-ref",
                "2026-01-01",
                "2026-01-01",
            ),
        )
    service = KnowledgeV2ReadonlyService(tmp_path)
    rows = service.candidates(scope)
    assert rows == (
        {
            "candidate_id": "candidate-1",
            "summary": "Candidate summary",
            "ref": "occurrence-ref",
            "hash": "hash-1",
            "status": "pending",
            "trust": "reference_only",
        },
    )
    assert service.candidates(_scope(tmp_path, agent="other-agent")) == ()


def test_readonly_outputs_reject_paths_and_sensitive_values_without_mutation(tmp_path: Path) -> None:
    store, scope, _occurrence = _content_fixture(tmp_path)
    namespace = store.ensure_namespace(namespace_id="ns-knowledge", trust_domain="knowledge")
    for index, locator in enumerate(
        (
            {"path": "C:/SECRET/path.txt"},
            {"body": "BODY MUST NOT ESCAPE"},
            {"secret": "SECRET MUST NOT ESCAPE"},
            {"title": "git+ssh://git.example/repo"},
            {"title": "ftp://files.example/item"},
            {"title": "custom://service/resource"},
            {"title": "data:opaque-secret"},
        ),
        start=1,
    ):
        blob = store.put_blob(namespace.namespace_id, f"payload-{index}")
        store.upsert_occurrence(
            source_object_id=f"sensitive-{index}",
            occurrence_key="row",
            blob_id=blob,
            namespace_id=namespace.namespace_id,
            workspace_id=scope.workspace_id,
            agent_instance_id=scope.agent_instance_id,
            project_ref=scope.project_ref,
            provider=scope.provider,
            share_group_id=scope.share_group_id,
            sensitivity=scope.sensitivity,
            policy_class=scope.policy_class,
            locator=locator,
        )

    layout = WorkspaceV2Layout(tmp_path)
    initialize_database(layout.knowledge_db, "knowledge", layout=layout)
    with sqlite3.connect(layout.knowledge_db) as conn:
        conn.executescript(KNOWLEDGE_CANDIDATE_SCHEMA)
        conn.execute(
            f"INSERT INTO {KNOWLEDGE_CANDIDATE_META}(key,value) VALUES ('version','1')"
        )
        conn.execute(
            f"INSERT INTO {KNOWLEDGE_CANDIDATE_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-safe",
                scope.namespace_id,
                scope.workspace_id,
                scope.agent_instance_id,
                scope.project_ref,
                scope.provider,
                scope.share_group_id,
                scope.sensitivity,
                scope.policy_class,
                "pending",
                "Normal title",
                "occurrence-ref",
                "hash-1",
                "occurrence-ref",
                "2026-01-01",
                "2026-01-01",
            ),
        )
        for index, uri in enumerate(
            (
                "git+ssh://git.example/repo",
                "ftp://files.example/item",
                "custom://service/resource",
                "data:opaque-secret",
            ),
            start=1,
        ):
            conn.execute(
                f"INSERT INTO {KNOWLEDGE_CANDIDATE_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"candidate-uri-{index}",
                    scope.namespace_id,
                    scope.workspace_id,
                    scope.agent_instance_id,
                    scope.project_ref,
                    scope.provider,
                    scope.share_group_id,
                    scope.sensitivity,
                    scope.policy_class,
                    "pending",
                    uri,
                    uri,
                    uri,
                    f"occurrence-uri-{index}",
                    "2026-01-01",
                    "2026-01-01",
                ),
            )
        conn.execute(
            f"INSERT INTO {KNOWLEDGE_CANDIDATE_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "candidate-sensitive",
                scope.namespace_id,
                scope.workspace_id,
                scope.agent_instance_id,
                scope.project_ref,
                scope.provider,
                scope.share_group_id,
                scope.sensitivity,
                scope.policy_class,
                "pending",
                "C:/SECRET/path.txt",
                "body-reference",
                "secret-hash",
                "occurrence-ref",
                "2026-01-01",
                "2026-01-01",
            ),
        )

    service = KnowledgeV2ReadonlyService(tmp_path)
    before_content = store.db_path.read_bytes()
    before_knowledge = layout.knowledge_db.read_bytes()
    content_result = service.book(scope)
    candidate_result = service.candidates(scope)
    serialized = json.dumps(
        {"book": content_result, "candidates": candidate_result},
        ensure_ascii=False,
        sort_keys=True,
    )
    for forbidden in (
        "C:/SECRET/path.txt",
        "BODY MUST NOT ESCAPE",
        "SECRET MUST NOT ESCAPE",
        "body-reference",
        "secret-hash",
        "git+ssh://git.example/repo",
        "ftp://files.example/item",
        "custom://service/resource",
        "data:opaque-secret",
    ):
        assert forbidden not in serialized
    assert any(item["summary"] == "Normal title" for item in candidate_result)
    for index in range(1, 5):
        row = next(item for item in candidate_result if item["candidate_id"] == f"candidate-uri-{index}")
        assert row["summary"] == row["ref"] == row["hash"] == ""
    assert store.db_path.read_bytes() == before_content
    assert layout.knowledge_db.read_bytes() == before_knowledge
