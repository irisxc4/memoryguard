from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.memory import MemoryAtomStore
from memoryguard.migration.v2_coordinator import V2MigrationCoordinator
from memoryguard.storage.database import open_database


def _group(root: Path, name: str, *, policy: str = "always", priority: int = 2) -> Path:
    path = root / ".memoryguard" / "shared-memory" / name / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
    )
    conn.execute(
        "CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT, project_ref TEXT, effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT)"
    )
    body = f"body-{name}"
    conn.execute(
        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "same-id",
            body,
            "fact",
            "active",
            0.9,
            1,
            policy,
            priority,
            "[]",
            "[]",
            f"agent-{name}",
            "t0",
            "t1",
            hashlib.sha256(body.encode()).hexdigest(),
            "relevant",
        ),
    )
    conn.execute(
        "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)",
        ("same-id", "agent", f"agent-{name}", "", "include", priority, "t0", "t1"),
    )
    conn.commit()
    conn.close()
    return path


def _history(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE conversation_sessions(session_id TEXT PRIMARY KEY, external_id TEXT, title TEXT, provider TEXT, agent_instance_id TEXT, project_ref TEXT, share_group_id TEXT, created_at TEXT, imported_at TEXT);
        CREATE TABLE conversation_turns(turn_id TEXT PRIMARY KEY, session_id TEXT, ordinal INTEGER, role TEXT, content TEXT, created_at TEXT, event_key TEXT, content_hash TEXT);
        CREATE TABLE session_summaries(session_id TEXT PRIMARY KEY, summary TEXT, summary_kind TEXT, updated_at TEXT);
        """
    )
    conn.execute("INSERT INTO conversation_sessions VALUES (?,?,?,?,?,?,?,?,?)", ("s1", "ext", "Chat", "codex", "agent-a", "/p", "g1", "", ""))
    text = "history body"
    conn.execute("INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?,?)", ("t1", "s1", 0, "user", text, "", "event-1", hashlib.sha256(text.encode()).hexdigest()))
    conn.execute("INSERT INTO session_summaries VALUES (?,?,?,?)", ("s1", "summary", "import", ""))
    conn.commit()
    conn.close()


def _knowledge(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE books(book_id TEXT PRIMARY KEY,title TEXT,root_path TEXT,status TEXT,created_at TEXT,updated_at TEXT);
        CREATE TABLE documents(document_id TEXT PRIMARY KEY,book_id TEXT,relative_path TEXT,media_type TEXT,content_hash TEXT,status TEXT,updated_at TEXT);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,document_id TEXT,book_id TEXT,ordinal INTEGER,text TEXT,text_hash TEXT,sensitivity TEXT,active INTEGER,created_at TEXT);
        CREATE TABLE memory_candidates(candidate_id TEXT PRIMARY KEY,book_id TEXT,chunk_id TEXT,content TEXT,source_text_hash TEXT,status TEXT,created_at TEXT);
        """
    )
    conn.execute("INSERT INTO books VALUES (?,?,?,?,?,?)", ("b1", "Book", "/book", "ready", "", ""))
    conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)", ("d1", "b1", "a.md", "text/plain", "h", "active", ""))
    conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?)", ("c1", "d1", "b1", 0, "knowledge body", "h", "normal", 1, ""))
    conn.commit()
    conn.close()


def _fixture(root: Path) -> tuple[Path, Path, Path, dict[Path, str]]:
    g1 = _group(root, "g1", policy="always", priority=2)
    g2 = _group(root, "g2", policy="relevant", priority=3)  # P3-like lower-priority scope.
    history = root / ".memoryguard" / "history" / "history.sqlite"
    _history(history)
    knowledge = root / "data" / "knowledge" / "knowledge.db"
    _knowledge(knowledge)
    sources = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (g1, g2, history, knowledge)}
    return g1, g2, history, sources


def test_phase2_first_run_idempotent_and_sources_unchanged(tmp_path: Path) -> None:
    _g1, _g2, _history_path, before = _fixture(tmp_path)
    coordinator = V2MigrationCoordinator(tmp_path, data_home=tmp_path / "data", migration_id="phase2-test-1")

    first = coordinator.run(strict=False)
    assert first.status == "V2_BUILDING"
    assert first.manifest_state == "V2_BUILDING"
    assert first.ready is False and first.to_dict()["can_promote"] is False
    assert first.validation["status"] == "PASS"
    assert "phase2_data_validated" in first.checkpoints

    second = coordinator.run(strict=False)
    assert second.status == "V2_BUILDING"
    assert not second.errors
    assert second.manifest_state == "V2_BUILDING"
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before

    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    atoms = [item for group in ("g1", "g2") for item in memory.list_atoms(scope={"share_group_id": group, "workspace_id": str(tmp_path)}, include_building=True)]
    assert {atom.share_group_id for atom in atoms} == {"g1", "g2"}
    assert memory.validate(evidence).orphan_count == 0
    assert memory.status()["outbox_pending"] == 0

    probe = V2MigrationCoordinator(
        tmp_path, data_home=tmp_path / "data"
    ).run(dry_run=True)
    assert probe.migration_id == "phase2-test-1"
    assert probe.checkpoints == first.checkpoints


def test_phase2_failure_returns_v1_and_new_batch_restarts(tmp_path: Path) -> None:
    _fixture(tmp_path)
    failed = V2MigrationCoordinator(tmp_path, data_home=tmp_path / "data", migration_id="phase2-fail", fail_at="rules_migrated").run(strict=False)
    assert failed.status == "FAILED"
    assert failed.manifest_state == "V1_ACTIVE"
    assert failed.ready is False

    retried = V2MigrationCoordinator(tmp_path, data_home=tmp_path / "data", migration_id="phase2-retry").run(strict=False)
    assert retried.status == "V2_BUILDING"
    assert retried.manifest_state == "V2_BUILDING"
    assert "phase2_data_validated" in retried.checkpoints


def test_phase2_rerun_blocks_changed_source_hash_without_overwriting_checkpoint(tmp_path: Path) -> None:
    g1, _g2, _history_path, _before = _fixture(tmp_path)
    coordinator = V2MigrationCoordinator(tmp_path, data_home=tmp_path / "data", migration_id="phase2-hash")
    first = coordinator.run(strict=False)
    assert first.status == "V2_BUILDING"
    expected = first.checkpoints["phase2_sources"]["hashes"]
    with g1.open("ab") as handle:
        handle.write(b"source-hash-change")
    rerun = coordinator.run(strict=False)
    assert rerun.status == "FAILED"
    assert rerun.manifest_state == "V1_ACTIVE"
    assert "source hash changed" in " ".join(rerun.errors)
    assert first.checkpoints["phase2_sources"]["hashes"] == expected


def test_coordinator_rejects_workspace_or_ancestor_symlink(tmp_path: Path):
    real = tmp_path / "real-workspace"
    real.mkdir()
    link = tmp_path / "workspace-link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError, match="symlink|reparse"):
        V2MigrationCoordinator(link)


@pytest.mark.parametrize(
    "store_cls,table,domain",
    [
        (MemoryAtomStore, "schema_meta", "memory"),
        (EvidenceStore, "schema_meta", "evidence"),
        (MemoryAtomStore, "memory_schema_meta", "memory"),
        (EvidenceStore, "evidence_schema_meta", "evidence"),
    ],
)
def test_phase2_store_rejects_future_base_marker_without_downgrade(tmp_path: Path, store_cls, table: str, domain: str) -> None:
    store = store_cls(tmp_path)
    path = store.path
    with sqlite3.connect(path) as conn:
        conn.execute(f"UPDATE {table} SET version=99, marker='future-marker' WHERE domain=?", (domain,))
        conn.commit()
    before = path.read_bytes()
    with pytest.raises(RuntimeError):
        store_cls(path)
    # Keep the verification read physically read-only.  A normal
    # sqlite3.connect() may checkpoint WAL state on close with older SQLite
    # versions, which would make the test itself mutate the file bytes.
    with open_database(path, readonly=True) as conn:
        assert tuple(conn.execute(f"SELECT version,marker FROM {table} WHERE domain=?", (domain,)).fetchone()) == (99, "future-marker")
    assert path.read_bytes() == before
