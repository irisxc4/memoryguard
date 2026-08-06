# -*- coding: utf-8 -*-
"""Part D: one-shot repair scripts (migrate_shared_group, repair_history).

Tests the script *logic* against synthetic real-shape databases without
invoking subprocesses: the merge/dedup/soft-delete rules (repair_history) and
the expectation gates + idempotency (migrate_shared_group) live in importable
functions of the scripts.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

from memoryguard.schema_v3 import SharedMemoryRecord
from memoryguard.shared_memory_store import SharedMemoryStore

SCRIPTS = Path(r"h:\ai\workspace\工具项目\memoryguard\scripts")


def _load(script: str, modname: str):
    """Load a scripts/ file as a uniquely-named module (so the same script
    can be imported multiple times across tests)."""
    spec = importlib.util.spec_from_file_location(modname, SCRIPTS / f"{script}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_history_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
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
    """)

    def sess(sid, ext, prov, agent, proj, imp):
        conn.execute(
            "INSERT INTO conversation_sessions(session_id,external_id,provider,agent_instance_id,"
            "project_ref,imported_at,created_at) VALUES(?,?,?,?,?,?,?)",
            (sid, ext, prov, agent, proj, imp, imp))

    def turn(tid, sid, ord_, role, content, created="2026-08-06T00:00:00Z"):
        conn.execute(
            "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, sid, ord_, role, content, created, _content_hash(content)))
        conn.execute(
            "INSERT INTO history_fts(session_id,turn_id,result_type,title,content) VALUES(?,?,'turn',?,?)",
            (sid, tid, "", content))

    # dual-write 1: identical turn under claude + cursor
    sess("s-claude-1", "ext-1", "claude", "a-claude", "", "2026-08-06T10:00:00Z")
    sess("s-cursor-1", "ext-1", "cursor", "a-cursor", "", "2026-08-06T10:00:00Z")
    turn("t-1a", "s-claude-1", 1, "user", "hello world")
    turn("t-1b", "s-cursor-1", 1, "user", "hello world")
    # dual-write 2: distinct content, both kept
    sess("s-claude-2", "ext-2", "claude", "a-claude", "", "2026-08-06T11:00:00Z")
    sess("s-cursor-2", "ext-2", "cursor", "a-cursor", "", "2026-08-06T11:00:00Z")
    turn("t-2a", "s-claude-2", 1, "user", "alpha")
    turn("t-2b", "s-cursor-2", 1, "assistant", "beta")
    # cross-agent claude duplicate
    sess("s-c1", "ext-3", "claude", "a-claude", "", "2026-08-06T12:00:00Z")
    sess("s-c2", "ext-3", "claude", "a-cursor", "", "2026-08-06T12:00:00Z")
    turn("t-3a", "s-c1", 1, "user", "gamma")
    turn("t-3b", "s-c2", 1, "user", "gamma")
    # legitimately distinct sessions: same external_id, different project -> must NOT merge
    sess("s-p1", "ext-4", "claude", "a-claude", "proj-A", "2026-08-06T13:00:00Z")
    sess("s-p2", "ext-4", "claude", "a-claude", "proj-B", "2026-08-06T14:00:00Z")
    turn("t-4a", "s-p1", 1, "user", "delta")
    turn("t-4b", "s-p2", 1, "assistant", "epsilon")
    conn.commit()
    return conn


def test_repair_history_merges_dual_writes_and_keeps_distinct_projects(tmp_path):
    mod = _load("repair_history", "repair_history_t1")
    db_path = tmp_path / "history.sqlite"
    conn = _build_history_db(db_path)

    groups = mod._find_merge_groups(conn)
    assert {g[0] for g in groups} == {"ext-1", "ext-2", "ext-3"}

    mod._phase_a(conn, dry_run=False)
    mod._rebuild_fts(conn)
    conn.commit()

    active = {row["external_id"]: row["n"] for row in conn.execute(
        "SELECT external_id, COUNT(*) n FROM conversation_sessions WHERE deleted_at='' "
        "GROUP BY external_id")}
    assert active == {"ext-1": 1, "ext-2": 1, "ext-3": 1, "ext-4": 2}

    orphans = conn.execute(
        "SELECT COUNT(*) n FROM conversation_turns t JOIN conversation_sessions s "
        "ON s.session_id=t.session_id WHERE s.deleted_at<>''").fetchone()["n"]
    assert orphans == 0
    conn.close()


def test_repair_history_phase_b_repairs_mojibake(tmp_path):
    mod = _load("repair_history", "repair_history_t2")
    db_path = tmp_path / "history.sqlite"
    conn = _build_history_db(db_path)
    moji = "cursor鍐欏叆鎬庝箞"
    conn.execute(
        "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        ("t-moji", "s-claude-1", 2, "user", moji, "2026-08-06T15:00:00Z", _content_hash(moji)))
    conn.commit()
    conn.row_factory = sqlite3.Row

    mod._phase_a(conn, dry_run=False)
    b = mod._phase_b(conn, dry_run=False)
    conn.commit()

    assert b["repaired"] == 1
    repaired = conn.execute(
        "SELECT content, content_hash FROM conversation_turns WHERE turn_id='t-moji'").fetchone()
    assert "cursor写入怎么" in repaired["content"]
    assert repaired["content_hash"] == _content_hash(repaired["content"])
    from memoryguard.encoding_guard import looks_like_mojibake
    assert not looks_like_mojibake(repaired["content"])
    conn.close()


def test_repair_history_phase_b_writes_partial_recovery_for_double_corrupt(tmp_path):
    # A double-corrupted turn (embedded PUA breaks byte alignment) cannot fully
    # recover, but the aligned prefix IS recovered and PUA is stripped: the
    # strictly-improved partial result is persisted and marked kind="partial".
    mod = _load("repair_history", "repair_history_t3")
    db_path = tmp_path / "history.sqlite"
    conn = _build_history_db(db_path)
    # The exact real-world double-corrupted turn (hist-1d49db): embedded PUA
    # codepoints break byte alignment, so only a prefix fully recovers.
    real = ("鎶婅閲忓尯鎹釜鏄剧ず鏂瑰紡锛屽悕瀛楄閲忔敾鍑诲姏闃插尽"
            "鍔涙樉绀轰竴涓嬨€傝澶囨爮鏈€濂芥槸鏈夊睘鎬у尯鏄剧ず瀹屾暣鎵€鏈夊睘鎬э紝"
            "浣犲啀濂藉ソ璁捐涓嬫€庝箞閲嶆瀯骞剁粺涓€ui")
    conn.execute(
        "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        ("t-partial", "s-claude-1", 2, "user", real, "2026-08-06T15:00:00Z", _content_hash(real)))
    conn.commit()
    conn.row_factory = sqlite3.Row

    b = mod._phase_b(conn, dry_run=False)
    conn.commit()

    assert b["partial"] == 1
    assert b["skipped"] == []
    assert any(e["kind"] == "partial" and e["turn_id"] == "t-partial" for e in b["manifest"])
    row = conn.execute(
        "SELECT content, content_hash FROM conversation_turns WHERE turn_id='t-partial'").fetchone()
    assert "把血量区换个显示方式" in row["content"]  # aligned prefix recovered
    assert row["content_hash"] == _content_hash(row["content"])
    from memoryguard.encoding_guard import strip_pua_residue
    _cleaned, lost = strip_pua_residue(row["content"])
    assert lost == 0  # PUA residue gone
    conn.close()


def test_repair_history_phase_b_skips_when_no_safe_change(tmp_path):
    # Garbage that passes no recovery gate and has no PUA to strip is left
    # byte-identical (bytes preserved for a future better recovery).
    mod = _load("repair_history", "repair_history_t4")
    db_path = tmp_path / "history.sqlite"
    conn = _build_history_db(db_path)
    irrecoverable = "锟斤拷" * 8  # mojibake of U+FFFD; cannot round-trip cleanly
    conn.execute(
        "INSERT INTO conversation_turns(turn_id,session_id,ordinal,role,content,created_at,content_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        ("t-skip", "s-claude-1", 2, "user", irrecoverable, "2026-08-06T15:00:00Z",
         _content_hash(irrecoverable)))
    conn.commit()
    conn.row_factory = sqlite3.Row

    b = mod._phase_b(conn, dry_run=False)
    conn.commit()

    assert b["partial"] == 0
    assert any(s["turn_id"] == "t-skip" for s in b["skipped"])
    row = conn.execute(
        "SELECT content FROM conversation_turns WHERE turn_id='t-skip'").fetchone()
    assert row["content"] == irrecoverable  # untouched
    conn.close()


def test_migrate_script_expectation_gate_aborts_on_mismatch(tmp_path):
    mod = _load("migrate_shared_group", "migrate_mod_t1")
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SharedMemoryStore(ws, "shared-legacy")
    for i in range(3):
        store.append_record(SharedMemoryRecord.from_dict({
            "memory_id": f"mig-{i}", "body": f"rule {i}", "kind": "fact", "status": "active",
            "confidence": 0.9, "injection_policy": "relevant", "priority": 0,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}))
    SharedMemoryStore(ws, "shared-new")

    rc = mod.main(["--source-workspace", str(ws), "--from", "shared-legacy",
                   "--workspace", str(ws), "--to", "shared-new",
                   "--expect-source", "99", "--expect-active", "3"])
    assert rc == 2


def test_migrate_script_dry_run_then_apply_is_idempotent(tmp_path, capsys):
    mod = _load("migrate_shared_group", "migrate_mod_t2")
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SharedMemoryStore(ws, "shared-legacy")
    for i in range(3):
        store.append_record(SharedMemoryRecord.from_dict({
            "memory_id": f"mig-{i}", "body": f"rule {i}", "kind": "fact", "status": "active",
            "confidence": 0.9, "injection_policy": "relevant", "priority": 0,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}))
    SharedMemoryStore(ws, "shared-new")

    args = ["--source-workspace", str(ws), "--from", "shared-legacy",
            "--workspace", str(ws), "--to", "shared-new",
            "--expect-source", "3", "--expect-active", "3"]
    assert mod.main(args) == 0
    capsys.readouterr()
    assert len(SharedMemoryStore(ws, "shared-new").list_records()) == 0

    assert mod.main(args + ["--apply"]) == 0
    assert len(SharedMemoryStore(ws, "shared-new").list_records()) == 3

    assert mod.main(args + ["--apply"]) == 0
    assert "already_migrated" in capsys.readouterr().out
    assert len(SharedMemoryStore(ws, "shared-new").list_records()) == 3
