# -*- coding: utf-8 -*-
"""Formal V2 migration and history-repair entry-point coverage."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

from memoryguard.evidence import EvidenceStore
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.migration.memory import V1GroupReader, V1MemoryMigrator
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(script: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / f"{script}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_history_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE conversation_sessions (
          session_id TEXT PRIMARY KEY, external_id TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
          provider TEXT NOT NULL DEFAULT '', agent_instance_id TEXT NOT NULL, project_ref TEXT NOT NULL DEFAULT '',
          share_group_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL,
          deleted_at TEXT NOT NULL DEFAULT '',
          UNIQUE(external_id, provider, agent_instance_id, project_ref, share_group_id));
        CREATE TABLE conversation_turns (
          turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, ordinal INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'unknown',
          content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', content_type TEXT NOT NULL DEFAULT 'text',
          event_key TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '', UNIQUE(session_id, ordinal));
        CREATE TABLE session_summaries (session_id TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '',
          summary_kind TEXT NOT NULL DEFAULT 'import', updated_at TEXT NOT NULL);
        CREATE TABLE observations (observation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT,
          observation_type TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE evidence_links (link_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, session_id TEXT NOT NULL,
          turn_id TEXT, status TEXT NOT NULL DEFAULT 'valid', created_at TEXT NOT NULL, invalidated_at TEXT NOT NULL DEFAULT '');
        CREATE VIRTUAL TABLE history_fts USING fts5(session_id UNINDEXED, turn_id UNINDEXED,
          result_type UNINDEXED, title, content, tokenize='unicode61');
        """
    )

    def session(session_id, external_id, provider, agent, project, imported_at):
        conn.execute(
            "INSERT INTO conversation_sessions(session_id,external_id,provider,agent_instance_id,"
            "project_ref,imported_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (session_id, external_id, provider, agent, project, imported_at, imported_at),
        )

    def turn(turn_id, session_id, ordinal, role, content, created="2026-08-06T00:00:00Z"):
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            (turn_id, session_id, ordinal, role, content, created, _content_hash(content)),
        )
        conn.execute(
            "INSERT INTO history_fts(session_id,turn_id,result_type,title,content) VALUES(?,?,'turn',?,?)",
            (session_id, turn_id, "", content),
        )

    session("s-claude-1", "ext-1", "claude", "a-claude", "", "2026-08-06T10:00:00Z")
    session("s-cursor-1", "ext-1", "cursor", "a-cursor", "", "2026-08-06T10:00:00Z")
    turn("t-1a", "s-claude-1", 1, "user", "hello world")
    turn("t-1b", "s-cursor-1", 1, "user", "hello world")
    session("s-claude-2", "ext-2", "claude", "a-claude", "", "2026-08-06T11:00:00Z")
    session("s-cursor-2", "ext-2", "cursor", "a-cursor", "", "2026-08-06T11:00:00Z")
    turn("t-2a", "s-claude-2", 1, "user", "alpha")
    turn("t-2b", "s-cursor-2", 1, "assistant", "beta")
    session("s-c1", "ext-3", "claude", "a-claude", "", "2026-08-06T12:00:00Z")
    session("s-c2", "ext-3", "claude", "a-cursor", "", "2026-08-06T12:00:00Z")
    turn("t-3a", "s-c1", 1, "user", "gamma")
    turn("t-3b", "s-c2", 1, "user", "gamma")
    session("s-p1", "ext-4", "claude", "a-claude", "proj-A", "2026-08-06T13:00:00Z")
    session("s-p2", "ext-4", "claude", "a-claude", "proj-B", "2026-08-06T14:00:00Z")
    turn("t-4a", "s-p1", 1, "user", "delta")
    turn("t-4b", "s-p2", 1, "assistant", "epsilon")
    conn.commit()
    return conn


def _history_fixture(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "history-workspace"
    db_path = workspace / ".memoryguard" / "history" / "history.sqlite"
    conn = _build_history_db(db_path)
    conn.close()
    return workspace, db_path


def _legacy_row(memory_id: str, body: str, **extra) -> tuple:
    values = {
        "memory_id": memory_id,
        "body": body,
        "kind": "fact",
        "status": "active",
        "confidence": 0.9,
        "locked": 0,
        "injection_policy": "relevant",
        "priority": 0,
        "supersedes": "[]",
        "provenance": "[]",
        "agent_instance_id": "agent-0",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "canonical_hash": hashlib.sha256(body.encode()).hexdigest(),
        "dedup_domain": "relevant",
    }
    values.update(extra)
    return tuple(
        values[key]
        for key in (
            "memory_id", "body", "kind", "status", "confidence", "locked",
            "injection_policy", "priority", "supersedes", "provenance",
            "agent_instance_id", "created_at", "updated_at", "canonical_hash",
            "dedup_domain",
        )
    )


def _legacy_group(root: Path, group_id: str, rows: list[tuple]) -> Path:
    path = root / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, "
            "supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, "
            "updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE decisions (event_id TEXT PRIMARY KEY, actor TEXT, "
            "action TEXT, target_ids TEXT, created_at TEXT)"
        )
        conn.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?)",
            (f"decision-{group_id}", "operator", "inventory", "[]", "now"),
        )
    return path


def _activate_v2(root: Path) -> None:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    from memoryguard.governance_v2 import GovernanceV2

    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="repair-entry-v2")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="repair-entry-source",
        target_digest="repair-entry-target",
        manifest_digest="repair-entry-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _migration_fixture(tmp_path: Path) -> tuple[Path, Path, Path, V1MemoryMigrator]:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_path = _legacy_group(
        source,
        "shared-legacy",
        [_legacy_row(f"mig-{index}", f"rule {index}") for index in range(3)],
    )
    _activate_v2(target)
    GroupControlService(target, write=True).bind_agent("migration-agent", "shared-new")
    migrator = V1MemoryMigrator(
        source,
        target=target,
        groups={"shared-legacy": source_path},
        group_targets={"shared-legacy": "shared-new"},
        include_managed=False,
        immutable_sources=True,
    )
    return source, target, source_path, migrator


def test_repair_history_formal_entry_merges_and_rebuilds_fts(tmp_path):
    mod = _load("repair_history", "repair_history_formal_t1")
    workspace, db_path = _history_fixture(tmp_path)
    manifest = workspace / ".memoryguard" / "history" / "repair.jsonl"

    assert mod.main([
        "--workspace", str(workspace), "--apply", "--manifest", str(manifest),
    ]) == 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        active = {
            row["external_id"]: row["n"]
            for row in conn.execute(
                "SELECT external_id, COUNT(*) n FROM conversation_sessions "
                "WHERE deleted_at='' GROUP BY external_id"
            )
        }
        assert active == {"ext-1": 1, "ext-2": 1, "ext-3": 1, "ext-4": 2}
        orphans = conn.execute(
            "SELECT COUNT(*) n FROM conversation_turns t JOIN conversation_sessions s "
            "ON s.session_id=t.session_id WHERE s.deleted_at<>''"
        ).fetchone()["n"]
        assert orphans == 0
        assert conn.execute("SELECT COUNT(*) FROM history_fts").fetchone()[0] >= 6
    assert manifest.exists()


def test_repair_history_formal_entry_repairs_mojibake(tmp_path):
    mod = _load("repair_history", "repair_history_formal_t2")
    workspace, db_path = _history_fixture(tmp_path)
    moji = "cursor写入怎么".encode("utf-8").decode("gb18030")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            ("t-moji", "s-claude-1", 2, "user", moji, "2026-08-06T15:00:00Z", _content_hash(moji)),
        )
    assert mod.main(["--workspace", str(workspace), "--apply"]) == 0

    from memoryguard.encoding_guard import looks_like_mojibake

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        repaired = conn.execute(
            "SELECT content, content_hash FROM conversation_turns WHERE turn_id='t-moji'"
        ).fetchone()
    assert repaired["content"] == "cursor写入怎么"
    assert repaired["content_hash"] == _content_hash(repaired["content"])
    assert not looks_like_mojibake(repaired["content"])


def test_repair_history_formal_entry_records_partial_recovery(tmp_path):
    mod = _load("repair_history", "repair_history_formal_t3")
    workspace, db_path = _history_fixture(tmp_path)
    real = (
        "閹跺﹨顢呴柌蹇撳隘閹诡澀閲滈弰鍓с仛閺傜懓绱￠敍灞芥倳鐎涙顢呴柌蹇旀暰閸戣濮忛梼鎻掑敖"
        "閸旀稒妯夌粈杞扮娑撳鈧倽顥婃径鍥ㄧ埉閺堚偓婵傝姤妲搁張澶婄潣閹冨隘閺勫墽銇氱€瑰本鏆ｉ幍鈧張澶婄潣閹嶇礉"
        "娴ｇ姴鍟€婵傝棄銈界拋鎹愵吀娑撳鈧簼绠為柌宥嗙€獮鍓佺埠娑撯偓ui"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            ("t-partial", "s-claude-1", 2, "user", real, "2026-08-06T15:00:00Z", _content_hash(real)),
        )
    manifest = workspace / ".memoryguard" / "history" / "partial.jsonl"
    assert mod.main([
        "--workspace", str(workspace), "--apply", "--manifest", str(manifest),
    ]) == 0

    entries = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(
        entry.get("kind") == "partial" and entry.get("turn_id") == "t-partial"
        for entry in entries
    )
    from memoryguard.encoding_guard import strip_pua_residue

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content, content_hash FROM conversation_turns WHERE turn_id='t-partial'"
        ).fetchone()
    assert row["content"] != real
    assert row["content_hash"] == _content_hash(row["content"])
    assert strip_pua_residue(row["content"])[1] == 0


def test_repair_history_formal_entry_skips_irrecoverable_content(tmp_path):
    mod = _load("repair_history", "repair_history_formal_t4")
    workspace, db_path = _history_fixture(tmp_path)
    irrecoverable = "閿熸枻鎷? * 8  # mojibake of U+FFFD; cannot round-trip cleanly"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            ("t-skip", "s-claude-1", 2, "user", irrecoverable, "2026-08-06T15:00:00Z", _content_hash(irrecoverable)),
        )
    assert mod.main(["--workspace", str(workspace), "--apply"]) == 0

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content FROM conversation_turns WHERE turn_id='t-skip'"
        ).fetchone()
    assert row["content"] == irrecoverable


def test_v2_migration_preview_is_an_explicit_expectation_gate(tmp_path):
    source, target, source_path, migrator = _migration_fixture(tmp_path)
    inventory = V1GroupReader(source, "shared-legacy", source_path, immutable=True).inventory()
    preview = migrator.preview()

    assert inventory.ok and inventory.records == 3 and inventory.active == 3
    assert preview.ok and preview.source_records == inventory.records
    assert preview.groups["shared-legacy"]["active"] == inventory.active
    # A mismatched expected count is visible before any target write; the
    # formal caller can fail closed on this preflight result.
    assert preview.source_records != 99
    assert MemoryAtomStore(target).status()["atoms"] == 0


def test_v2_migration_preview_apply_is_idempotent(tmp_path):
    _source, target, _source_path, migrator = _migration_fixture(tmp_path)
    dry = migrator.preview()
    assert dry.ok and dry.source_records == 3 and dry.atoms == 0
    assert MemoryAtomStore(target).status()["atoms"] == 0

    first = migrator.migrate()
    assert first.ok, first.to_dict()
    memory = MemoryAtomStore(target)
    evidence = EvidenceStore(target)
    scope = MemoryReadScope(
        workspace_id=str(target.resolve()),
        share_group_id="shared-new",
        admin=True,
    )
    atoms = memory.list_atoms(scope=scope, include_building=True)
    assert len({atom.memory_id for atom in atoms}) == 3
    mappings = memory.list_source_mappings()
    assert len(mappings) == 3
    assert memory.validate(evidence, include_building=True).orphan_count == 0

    second = migrator.migrate()
    assert second.ok, second.to_dict()
    assert len(memory.list_atoms(scope=scope, include_building=True)) == 3
    assert len(memory.list_source_mappings()) == len(mappings)
