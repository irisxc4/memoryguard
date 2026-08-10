from __future__ import annotations

import json
import shutil
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from memoryguard.codegraph_v2 import (
    CodeGraphPathError,
    CodeGraphScope,
    CodeGraphScopeError,
    CodeGraphStore,
)
from memoryguard.content import ContentReadScope, ContentStore
from memoryguard.knowledge_v2 import KnowledgeV2Adapter
from memoryguard.migration.codegraph import V1CodeGraphMigrator


def _scope(root: Path) -> CodeGraphScope:
    return CodeGraphScope(str(root), "agent-a", "project-a", "codex", "group-a", "hook")


def _write_legacy_source(path: Path, *rows: tuple[str, str, str, str, str, str]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE files(id TEXT PRIMARY KEY, path TEXT, content_hash TEXT, name TEXT, authority TEXT, ownership TEXT)")
        conn.executemany("INSERT INTO files VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()


def _assert_renamable_and_deleted(path: Path) -> None:
    assert path.is_file()
    renamed = path.with_name(path.name + ".renamed")
    path.rename(renamed)
    renamed.unlink()


def test_knowledge_adapter_is_reference_only_and_scope_exact(tmp_path: Path) -> None:
    store = ContentStore(tmp_path)
    namespace = store.ensure_namespace(trust_domain="knowledge")
    blob = store.put_blob(namespace.namespace_id, "SECRET BODY MUST NOT ESCAPE")
    assert blob
    occurrence = store.upsert_occurrence(
        source_object_id="source",
        occurrence_key="doc-1",
        blob_id=blob,
        namespace_id=namespace.namespace_id,
        workspace_id=str(tmp_path),
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="codex",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
        locator={"title": "Safe title", "body": "blocked"},
    )
    scope = ContentReadScope(namespace.namespace_id, str(tmp_path), "agent-a", "project-a", "codex", "group-a", "normal", "private")
    item = KnowledgeV2Adapter(store).get(occurrence, scope)
    assert item == {
        "summary": "Safe title",
        "ref": occurrence,
        "hash": __import__("hashlib").sha256("SECRET BODY MUST NOT ESCAPE".encode()).hexdigest(),
        "trust": "reference_only",
    }
    assert not any(key in item for key in ("body", "text", "authority", "ownership", "exists"))
    denied = ContentReadScope(namespace.namespace_id, str(tmp_path), "other-agent", "project-a", "codex", "group-a", "normal", "private")
    assert KnowledgeV2Adapter(store).get(occurrence, denied) is None


def test_codegraph_revisions_acl_and_affected_are_deterministic(tmp_path: Path) -> None:
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    file = store.upsert_source_file(
        "src/a.py",
        "hash-1",
        scope=scope,
        symbols=(
            {"name": "caller", "kind": "function", "signature": "caller()", "line_start": 1, "line_end": 2},
            {"name": "callee", "kind": "function", "signature": "callee()", "line_start": 4, "line_end": 5},
        ),
    )
    same = store.upsert_source_file("src/a.py", "hash-1", scope=scope)
    assert same.file_id == file.file_id and same.revision_id == file.revision_id
    symbols = store.get_symbols(file.file_id, scope=scope)
    store.put_edges(({"from_id": symbols[0].symbol_id, "to_id": symbols[1].symbol_id, "relation": "calls"},), scope=scope)
    assert store.affected(symbols[1].symbol_id, scope=scope, depth=2, limit=10) == [symbols[0].symbol_id]
    assert store.affected(symbols[1].symbol_id, scope=scope, depth=2, limit=10) == [symbols[0].symbol_id]
    changed = store.upsert_source_file("src/a.py", "hash-2", scope=scope)
    assert changed.revision_id != file.revision_id
    assert store.counts(scope=scope)["revisions"] == 2
    assert store.list_source_files(scope=CodeGraphScope(str(tmp_path), "other", "project-a", "codex", "group-a", "hook")) == ()
    tombstone = store.tombstone_source_file("src/a.py", scope=scope)
    assert tombstone and store.list_source_files(scope=scope) == ()


def test_codegraph_rejects_body_and_escape_and_rolls_back_failed_file(tmp_path: Path) -> None:
    store = CodeGraphStore(tmp_path)
    scope = _scope(tmp_path)
    with pytest.raises(CodeGraphPathError):
        store.upsert_source_file("../outside.py", "hash", scope=scope)
    with pytest.raises(ValueError):
        store.upsert_source_file("src/x.py", "", scope=scope)
    with pytest.raises(Exception):
        from memoryguard.codegraph_v2 import CodeGraphProjector

        CodeGraphProjector(store).project({"path": "src/x.py", "content_hash": "hash", "body": "no"}, scope=scope)
    assert store.db_path.is_file()
    assert store.counts(scope=scope)["source_files"] == 0


def test_legacy_migration_unknown_acl_is_blocked_and_source_is_read_only(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    conn = sqlite3.connect(source)
    try:
        conn.execute("CREATE TABLE files(id TEXT PRIMARY KEY, path TEXT, content_hash TEXT, name TEXT, authority TEXT, ownership TEXT)")
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", ("known", "src/known.py", "h1", "known", "trusted", "workspace"))
        conn.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", ("unknown", "src/unknown.py", "h2", "unknown", "", ""))
        conn.commit()
    finally:
        conn.close()
    before = source.read_bytes()
    scope = CodeGraphScope(str(tmp_path), "migration", "legacy", "migration", "group", "acceptance")
    report = V1CodeGraphMigrator(tmp_path, source_path=source, scope=scope).migrate()
    assert report.status == "BLOCKED"
    assert report.unknown_authority >= 1 and report.unknown_ownership >= 1
    assert source.read_bytes() == before
    store = CodeGraphStore(tmp_path)
    assert store.counts(scope=scope)["unknown_blocked"] >= 1


def test_legacy_migration_releases_sqlite_handles_after_success(tmp_path: Path) -> None:
    source = tmp_path / "legacy.db"
    _write_legacy_source(source, ("known", "src/known.py", "h1", "known", "trusted", "workspace"))
    scope = CodeGraphScope(str(tmp_path), "migration", "legacy", "migration", "group", "acceptance")
    migrator = V1CodeGraphMigrator(tmp_path, source_path=source, scope=scope)

    assert migrator.migrate(write_shadow=True).status == "OK"
    # A pre-existing target exercises the SQLite online-backup compensation
    # snapshot that previously left migration-backup handles open on Windows.
    assert migrator.migrate(write_shadow=True).status == "OK"

    target = CodeGraphStore(tmp_path).db_path
    backup = Path(str(target) + ".migration-backup")
    assert not backup.exists()
    _assert_renamable_and_deleted(target)
    _assert_renamable_and_deleted(source)
    shutil.rmtree(target.parent.parent)
    assert not target.parent.parent.exists()


def test_legacy_migration_releases_sqlite_handles_after_compensation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "legacy.db"
    _write_legacy_source(source, ("known", "src/known.py", "h1", "known", "trusted", "workspace"))
    scope = CodeGraphScope(str(tmp_path), "migration", "legacy", "migration", "group", "acceptance")
    migrator = V1CodeGraphMigrator(tmp_path, source_path=source, scope=scope)
    assert migrator.migrate(write_shadow=True).status == "OK"

    def fail_counts(self: CodeGraphStore, *, scope: CodeGraphScope | None = None) -> dict[str, int]:
        raise RuntimeError("injected counts failure")

    monkeypatch.setattr(CodeGraphStore, "counts", fail_counts)
    with pytest.raises(RuntimeError, match="injected counts failure"):
        migrator.migrate(write_shadow=True)

    target = CodeGraphStore(tmp_path).db_path
    backup = Path(str(target) + ".migration-backup")
    assert migrator.last_report is not None and migrator.last_report.status == "FAILED"
    assert target.is_file() and not backup.exists()
    _assert_renamable_and_deleted(target)
    _assert_renamable_and_deleted(source)
    shutil.rmtree(target.parent.parent)
    assert not target.parent.parent.exists()


def test_phase5_acceptance_default_is_read_only_json(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "accept_v2_phase5.py"
    result = subprocess.run([sys.executable, str(script), "--workspace", str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "V2_BUILDING"
    assert payload["ready"] is False and payload["can_promote"] is False
    assert payload["dry_run"] is True
    assert not (tmp_path / ".memoryguard").exists()
