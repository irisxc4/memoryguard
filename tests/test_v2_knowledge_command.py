from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from memoryguard.content import ContentReadScope, ContentStore
from memoryguard.knowledge_v2.command import KnowledgeV2CommandService
from memoryguard.knowledge_v2.service import KNOWLEDGE_CANDIDATE_TABLE
from memoryguard.memory import MemoryAtomStore


def _scope(tmp_path: Path) -> ContentReadScope:
    return ContentReadScope(
        namespace_id="knowledge-ns-test",
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="local",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
    )


def _context() -> dict[str, object]:
    return {
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "provider": "local",
        "share_group_id": "group-a",
        "runtime_role": "gui",
        "admin": True,
    }


def _add_and_wait(
    service: KnowledgeV2CommandService,
    source: Path,
    scope: ContentReadScope,
    *,
    title: str = "",
) -> str:
    accepted = service.add(
        {"path": str(source), "title": title},
        scope=scope,
        context=_context(),
    )
    run_id = str(accepted["task"]["run_id"])
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        status = service.task_status({"run_id": run_id}, scope=scope, context=_context())
        if status.get("status") == "succeeded":
            with sqlite3.connect(service.layout.knowledge_db) as conn:
                row = conn.execute(
                    "SELECT asset_id FROM knowledge_assets ORDER BY updated_at DESC,asset_id LIMIT 1"
                ).fetchone()
            assert row is not None
            return str(row[0])
        if status.get("status") in {"failed", "cancelled"}:
            raise AssertionError(status)
        time.sleep(0.02)
    raise AssertionError("knowledge add task did not finish")


def test_knowledge_ingest_keeps_body_only_in_content_store(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    sentinel = "UNIQUE_KNOWLEDGE_BODY_SENTINEL_4b839e"
    (source / "guide.md").write_text(f"# Guide\n\n{sentinel}\n", encoding="utf-8")

    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    asset_id = _add_and_wait(service, source, scope, title="Guide Book")

    with sqlite3.connect(service.layout.knowledge_db) as conn:
        asset = conn.execute(
            "SELECT title,status,metadata_json FROM knowledge_assets WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        document = conn.execute(
            "SELECT path,status,metadata_json FROM knowledge_documents WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
    assert asset is not None and document is not None
    assert asset[0] == "Guide Book"
    assert asset[1] == "active"
    assert document[0] == "guide.md"
    assert document[1] == "active"
    assert sentinel not in str(asset[2])
    assert sentinel not in str(document[2])
    assert sentinel.encode("utf-8") not in service.layout.knowledge_db.read_bytes()

    with sqlite3.connect(ContentStore(tmp_path, initialize=False).db_path) as conn:
        bodies = [str(row[0]) for row in conn.execute("SELECT text FROM content_blobs").fetchall()]
    assert any(sentinel in body for body in bodies)
    service.close()


def test_knowledge_remove_restore_and_purge_use_content_tombstones(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    (source / "guide.md").write_text("# Guide\n\nbody for lifecycle\n", encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    asset_id = _add_and_wait(service, source, scope)

    removed = service.remove({"book_id": asset_id}, scope=scope)
    assert removed["status"] == "succeeded"
    deletion_id = str(removed["data"]["deletion_id"])
    deleted = service.deleted(scope=scope)
    assert [item["deletion_id"] for item in deleted["data"]["items"]] == [deletion_id]

    content_db = ContentStore(tmp_path, initialize=False).db_path
    with sqlite3.connect(content_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_occurrences WHERE active=0").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM content_holds WHERE active=1").fetchone()[0] > 0
        blob_count = conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]

    restored = service.restore({"deletion_id": deletion_id}, scope=scope)
    assert restored["status"] == "succeeded"
    with sqlite3.connect(content_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_occurrences WHERE active=1").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM content_holds WHERE active=1").fetchone()[0] == 0

    removed_again = service.remove({"book_id": asset_id}, scope=scope)
    purged = service.purge(
        {"deletion_id": removed_again["data"]["deletion_id"]},
        scope=scope,
    )
    assert purged["status"] == "succeeded"
    with sqlite3.connect(content_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM content_holds WHERE active=1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0] == blob_count
    service.close()


def test_knowledge_candidate_approval_commits_governed_v2_memory(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    body = "candidate governed body"
    (source / "candidate.md").write_text(body, encoding="utf-8")
    service = KnowledgeV2CommandService(tmp_path)
    scope = _scope(tmp_path)
    asset_id = _add_and_wait(service, source, scope)

    with sqlite3.connect(service.layout.knowledge_db) as kconn:
        doc = kconn.execute(
            "SELECT metadata_json FROM knowledge_documents WHERE asset_id=?",
            (asset_id,),
        ).fetchone()
        assert doc is not None
        metadata = json.loads(doc[0])
        occurrence_id = metadata["occurrence_ids"][0]
        content_hash = metadata["content_hash"]
        kconn.execute(
            f"INSERT INTO {KNOWLEDGE_CANDIDATE_TABLE}(candidate_id,namespace_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class,status,summary,reference,content_hash,source_occurrence_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
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
                "candidate summary",
                occurrence_id,
                content_hash,
                occurrence_id,
            ),
        )
        kconn.commit()

    approved = service.review_candidate(
        {
            "candidate_id": "candidate-1",
            "decision": "approve",
            "target_group_id": scope.share_group_id,
        },
        scope=scope,
        context=_context(),
    )
    assert approved["ok"] is True
    assert approved["data"]["status"] == "approved"
    synced_memory_id = str(approved["data"]["synced_memory_id"])
    assert synced_memory_id

    memory = MemoryAtomStore(tmp_path, readonly=True)
    atom = memory.get_atom(
        synced_memory_id,
        scope={
            "workspace_id": scope.workspace_id,
            "agent_instance_id": scope.agent_instance_id,
            "project_ref": scope.project_ref,
            "provider": scope.provider,
            "share_group_id": scope.share_group_id,
            "runtime_role": "gui",
        },
        include_building=True,
    )
    assert atom is not None
    assert "candidate governed body" in atom.body
    assert atom.visibility == "active"
    assert memory.evidence_ids_for_atom(atom.atom_id)

    with sqlite3.connect(service.layout.knowledge_db) as conn:
        assert conn.execute(
            f"SELECT status FROM {KNOWLEDGE_CANDIDATE_TABLE} WHERE candidate_id='candidate-1'"
        ).fetchone()[0] == "approved"
    service.close()
