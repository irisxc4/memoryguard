import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.codex_subagent_reconcile import (
    CodexSubagentReconciler,
    dry_run_codex_subagents_json,
    dry_run_global_codex_subagents,
    reconcile_codex_subagents,
    reconcile_codex_subagents_json,
    reconcile_codex_subagent_stop,
    reconcile_global_codex_subagents,
)
from memoryguard.host_hooks import run_hook


SCHEMA = """
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    updated_at INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL DEFAULT 0,
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    rollout_path TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT ''
);
CREATE TABLE thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
"""


def _db(home: Path, threads, edges) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state_5.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO threads(id, archived, archived_at, updated_at) VALUES(?,?,?,?)",
        threads,
    )
    conn.executemany(
        "INSERT INTO thread_spawn_edges(parent_thread_id, child_thread_id, status) VALUES(?,?,?)",
        edges,
    )
    conn.commit()
    conn.close()
    return path


def _read(path: Path, sql: str, args=()):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _rollout(home: Path, db: Path, thread_id: str, event: str) -> Path:
    path = home / "sessions" / f"{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "event_msg", "payload": {"type": event}}) + "\n",
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE threads SET rollout_path=? WHERE id=?", (str(path), thread_id)
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_multi_root_nested_reconcile_isolated_and_bumps_root_recency(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [
            ("root-a", 0, None, 1),
            ("a-child", 0, None, 2),
            ("a-grandchild", 1, 10, 3),
            ("root-b", 0, None, 4),
            ("b-child", 0, None, 5),
        ],
        [
            ("root-a", "a-child", "open"),
            ("a-child", "a-grandchild", "open"),
            ("root-b", "b-child", "open"),
        ],
    )

    result = reconcile_codex_subagents("root-a", codex_home=home)

    assert result["ok"] is True
    assert result["changed"] is True
    assert result["closed_edge_ids"] == ["a-child", "a-grandchild"]
    assert result["archived_thread_ids"] == ["a-child"]
    assert _read(db, "SELECT status FROM thread_spawn_edges WHERE parent_thread_id='root-b'") == [("open",)]
    assert _read(db, "SELECT archived FROM threads WHERE id='b-child'") == [(0,)]
    root_times = _read(db, "SELECT updated_at,recency_at,recency_at_ms FROM threads WHERE id='root-a'")[0]
    assert root_times[0] > 1 and root_times[1] > 1 and root_times[2] > 0
    # Public JSON keeps recovery details, while the on-disk diagnostic is a
    # sanitized summary with no raw IDs or user filesystem paths.
    receipt_text = Path(result["diagnostic_receipt"]).read_text(encoding="utf-8")
    assert "root-a" not in receipt_text
    assert "a-child" not in receipt_text
    assert "a-grandchild" not in receipt_text
    assert str(home) not in receipt_text
    assert "root_thread_digest" in receipt_text
    assert "codex_home/state_5.sqlite" in receipt_text


def test_explicit_subagent_stop_closes_incoming_edge_and_archives_branch(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1), ("sub", 0, None, 2), ("grand", 0, None, 3)],
        [("root", "sub", "open"), ("sub", "grand", "open")],
    )

    result = reconcile_codex_subagent_stop(
        {"agent_id": "sub"},
        codex_home=home,
        trusted_parent_thread_id="root",
    )

    assert result["ok"] is True
    assert result["reason"] == "subagent_stopped"
    assert result["terminal_thread_ids"] == ["grand", "sub"]
    assert _read(
        db,
        "SELECT child_thread_id,status FROM thread_spawn_edges ORDER BY child_thread_id",
    ) == [("grand", "closed"), ("sub", "closed")]
    assert _read(
        db,
        "SELECT id,archived FROM threads WHERE id IN ('sub','grand') ORDER BY id",
    ) == [("grand", 1), ("sub", 1)]


def test_explicit_subagent_stop_stays_open_with_active_descendant(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1), ("sub", 0, None, 2), ("grand", 0, None, 3)],
        [("root", "sub", "open"), ("sub", "grand", "open")],
    )

    result = reconcile_codex_subagent_stop(
        {"subagent_id": "sub"},
        codex_home=home,
        trusted_parent_thread_id="root",
        active_thread_ids={"grand"},
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "subagent_stop_active_descendant"
    assert _read(
        db,
        "SELECT child_thread_id,status FROM thread_spawn_edges ORDER BY child_thread_id",
    ) == [("grand", "open"), ("sub", "open")]


def test_subagent_stop_only_closes_its_descendants_not_parent_edge(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1), ("sub", 0, None, 2), ("grand", 0, None, 3)],
        [("root", "sub", "open"), ("sub", "grand", "open")],
    )

    result = reconcile_codex_subagents("sub", codex_home=home)

    assert result["closed_edge_ids"] == ["grand"]
    assert _read(db, "SELECT status FROM thread_spawn_edges WHERE child_thread_id='sub'") == [("open",)]
    assert _read(db, "SELECT status FROM thread_spawn_edges WHERE child_thread_id='grand'") == [("closed",)]


def test_active_whitelist_is_a_hard_branch_boundary(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1), ("active", 0, None, 2), ("nested", 0, None, 3), ("stale", 0, None, 4)],
        [("root", "active", "open"), ("active", "nested", "open"), ("root", "stale", "open")],
    )

    result = reconcile_codex_subagents("root", codex_home=home, active_thread_ids={"active"})

    assert result["skipped_active_thread_ids"] == ["active"]
    assert result["closed_edge_ids"] == ["stale"]
    assert _read(db, "SELECT status FROM thread_spawn_edges WHERE child_thread_id='nested'") == [("open",)]
    assert _read(db, "SELECT archived FROM threads WHERE id='active'") == [(0,)]


def test_dry_run_is_read_only_and_json_api_is_serializable(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(home, [("root", 0, None, 1), ("child", 0, None, 2)], [("root", "child", "open")])

    result = CodexSubagentReconciler(codex_home=home).dry_run("root")

    assert result["status"] == "dry_run"
    assert result["changed"] is False
    assert _read(db, "SELECT status,archived FROM thread_spawn_edges JOIN threads ON child_thread_id=id") == [("open", 0)]
    assert not list((home / "memoryguard-codex-backups").glob("*.bak"))
    assert json.loads(reconcile_codex_subagents_json("root", codex_home=home, dry_run=True))["status"] == "dry_run"
    assert json.loads(dry_run_codex_subagents_json("root", codex_home=home))["status"] == "dry_run"


def test_missing_or_corrupt_db_is_degraded_without_creating_state_db(tmp_path: Path):
    home = tmp_path / "codex"
    missing = reconcile_codex_subagents("root", codex_home=home)
    assert missing["degraded"] is True
    assert missing["status"] == "missing"
    assert not (home / "state_5.sqlite").exists()
    assert Path(missing["diagnostic_receipt"]).exists()

    home.mkdir(parents=True, exist_ok=True)
    (home / "state_5.sqlite").write_bytes(b"not sqlite")
    corrupt = reconcile_codex_subagents("root", codex_home=home)
    assert corrupt["degraded"] is True
    assert corrupt["status"] in {"corrupt", "degraded"}
    assert Path(corrupt["diagnostic_receipt"]).exists()
    corrupt_receipt = Path(corrupt["diagnostic_receipt"]).read_text(encoding="utf-8")
    assert "state_db_path" not in corrupt_receipt
    assert str(home) not in corrupt_receipt
    assert "root_thread_id" not in corrupt_receipt
    assert '"root"' not in corrupt_receipt


def test_path_containment_rejects_state_db_outside_codex_home(tmp_path: Path):
    home = tmp_path / "codex"
    outside = tmp_path / "outside.sqlite"
    result = reconcile_codex_subagents(
        "root", state_db_path=outside, codex_home=home
    )
    assert result["degraded"] is True
    assert result["status"] == "unsafe_path"


def test_busy_database_is_degraded_and_not_partially_mutated(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(home, [("root", 0, None, 1), ("child", 0, None, 2)], [("root", "child", "open")])
    lock = sqlite3.connect(db, timeout=0.01)
    lock.execute("BEGIN IMMEDIATE")
    try:
        result = reconcile_codex_subagents("root", codex_home=home, busy_timeout_ms=10)
    finally:
        lock.rollback()
        lock.close()
    assert result["degraded"] is True
    assert result["status"] in {"locked", "degraded"}
    assert _read(db, "SELECT status,archived FROM thread_spawn_edges JOIN threads ON child_thread_id=id") == [("open", 0)]


def test_backup_retention_is_bounded_and_restore_path_is_explicit(tmp_path: Path, monkeypatch):
    home = tmp_path / "codex"
    db = _db(home, [("root", 0, None, 1), ("child", 0, None, 2)], [("root", "child", "open")])
    now = 1_800_000_000_000
    import memoryguard.codex_subagent_reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module, "_now_ms", lambda: now)
    for index in range(5):
        (home / "memoryguard-codex-backups").mkdir(parents=True, exist_ok=True)
        stale = home / "memoryguard-codex-backups" / f"state_5.sqlite.mg-20260101T00000{index}.000000Z.bak"
        stale.write_bytes(b"placeholder")
        stale.touch()
    result = reconcile_codex_subagents("root", codex_home=home)
    backups = list((home / "memoryguard-codex-backups").glob("*.bak"))
    assert len(backups) <= 3
    assert result["restore_path"] == result["backup_path"]
    assert Path(result["restore_path"]).exists()


def test_global_reconcile_requires_terminal_rollout_and_preserves_live_branches(
    tmp_path: Path,
):
    home = tmp_path / "codex"
    db = _db(
        home,
        [
            ("root-a", 0, None, 1),
            ("done-a", 0, None, 2),
            ("root-b", 0, None, 3),
            ("aborted-b", 0, None, 4),
            ("root-c", 0, None, 5),
            ("terminal-parent", 0, None, 6),
            ("live-grand", 0, None, 7),
            ("root-d", 0, None, 8),
            ("active-terminal", 0, None, 9),
            ("root-e", 0, None, 10),
            ("no-rollout", 0, None, 11),
        ],
        [
            ("root-a", "done-a", "open"),
            ("root-b", "aborted-b", "open"),
            ("root-c", "terminal-parent", "open"),
            ("terminal-parent", "live-grand", "open"),
            ("root-d", "active-terminal", "open"),
            ("root-e", "no-rollout", "open"),
        ],
    )
    _rollout(home, db, "done-a", "task_complete")
    _rollout(home, db, "aborted-b", "turn_aborted")
    _rollout(home, db, "terminal-parent", "task_complete")
    _rollout(home, db, "live-grand", "agent_message")
    _rollout(home, db, "active-terminal", "task_complete")

    dry = dry_run_global_codex_subagents(
        codex_home=home, active_thread_ids={"active-terminal"}
    )
    assert dry["status"] == "dry_run"
    assert dry["closed_edge_count"] == 2
    assert dry["terminal_event_counts"] == {
        "task_complete": 1,
        "turn_aborted": 1,
    }
    assert _read(db, "SELECT COUNT(*) FROM thread_spawn_edges WHERE status='open'") == [(6,)]

    result = reconcile_global_codex_subagents(
        codex_home=home, active_thread_ids={"active-terminal"}
    )
    assert result["closed_edge_count"] == result["archived_thread_count"] == 2
    assert Path(result["restore_path"]).is_file()
    assert _read(
        db,
        "SELECT child_thread_id,status FROM thread_spawn_edges ORDER BY child_thread_id",
    ) == [
        ("aborted-b", "closed"),
        ("active-terminal", "open"),
        ("done-a", "closed"),
        ("live-grand", "open"),
        ("no-rollout", "open"),
        ("terminal-parent", "open"),
    ]
    assert _read(
        db,
        "SELECT id,archived FROM threads WHERE id IN ('done-a','aborted-b','live-grand','terminal-parent') ORDER BY id",
    ) == [
        ("aborted-b", 1),
        ("done-a", 1),
        ("live-grand", 0),
        ("terminal-parent", 0),
    ]
    replay = dry_run_global_codex_subagents(
        codex_home=home, active_thread_ids={"active-terminal"}
    )
    assert replay["reason"] == "already_reconciled"


def test_global_reconcile_closes_missing_child_edge_after_history_delete(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1)],
        [("root", "deleted-child", "open")],
    )

    dry = dry_run_global_codex_subagents(codex_home=home)
    assert dry["closed_edge_count"] == 1
    assert dry["archived_thread_count"] == 0
    assert dry["missing_thread_count"] == 1
    assert dry["missing_thread_ids"] == ["deleted-child"]
    assert dry["terminal_event_counts"] == {"missing_thread": 1}

    result = reconcile_global_codex_subagents(codex_home=home)
    assert result["closed_edge_count"] == 1
    assert result["archived_thread_count"] == 0
    assert _read(
        db,
        "SELECT child_thread_id,status FROM thread_spawn_edges",
    ) == [("deleted-child", "closed")]
    assert _read(db, "SELECT id FROM threads ORDER BY id") == [("root",)]


def test_missing_parent_branch_stays_open_when_live_descendant_exists(tmp_path: Path):
    home = tmp_path / "codex"
    db = _db(
        home,
        [("root", 0, None, 1), ("live-grand", 0, None, 2)],
        [
            ("root", "deleted-child", "open"),
            ("deleted-child", "live-grand", "open"),
        ],
    )
    _rollout(home, db, "live-grand", "agent_message")

    dry = dry_run_global_codex_subagents(codex_home=home)
    assert dry["closed_edge_count"] == 0
    assert dry["missing_thread_count"] == 1
    assert dry["skipped_nonterminal_count"] == 1
    assert _read(
        db,
        "SELECT child_thread_id,status FROM thread_spawn_edges ORDER BY child_thread_id",
    ) == [("deleted-child", "open"), ("live-grand", "open")]


def test_host_session_start_globally_recovers_terminal_stale_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _db(
        codex_home,
        [("old-root", 0, None, 1), ("old-child", 0, None, 2), ("current", 0, None, 3)],
        [("old-root", "old-child", "open")],
    )
    _rollout(codex_home, db, "old-child", "task_complete")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE threads SET cwd=? WHERE id='current'", (str(workspace),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_THREAD_ID", "current")

    run_hook(
        provider="codex",
        event="session_start",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "host-start"},
    )

    assert _read(
        db,
        "SELECT status,archived FROM thread_spawn_edges JOIN threads ON child_thread_id=id",
    ) == [("closed", 1)]


def test_host_user_prompt_repairs_terminal_stale_edge_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = _db(
        codex_home,
        [("old-root", 0, None, 1), ("old-child", 0, None, 2), ("current", 0, None, 3)],
        [("old-root", "old-child", "open")],
    )
    _rollout(codex_home, db, "old-child", "turn_aborted")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE threads SET cwd=? WHERE id='current'", (str(workspace),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_THREAD_ID", "current")

    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "current", "prompt": "continue"},
    )

    assert _read(
        db,
        "SELECT status,archived FROM thread_spawn_edges JOIN threads ON child_thread_id=id",
    ) == [("closed", 1)]


def test_host_session_start_workspace_mismatch_never_touches_global_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    codex_home = home / ".codex"
    workspace = tmp_path / "workspace"
    other_workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    other_workspace.mkdir()
    db = _db(
        codex_home,
        [("old-root", 0, None, 1), ("old-child", 0, None, 2), ("current", 0, None, 3)],
        [("old-root", "old-child", "open")],
    )
    _rollout(codex_home, db, "old-child", "task_complete")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE threads SET cwd=? WHERE id='current'", (str(other_workspace),))
    conn.commit()
    conn.close()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_THREAD_ID", "current")

    run_hook(
        provider="codex",
        event="session_start",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "host-start-mismatch"},
    )

    assert _read(
        db,
        "SELECT status,archived FROM thread_spawn_edges JOIN threads ON child_thread_id=id",
    ) == [("open", 0)]


def test_host_stop_reconcile_is_best_effort_when_db_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".codex").mkdir(parents=True)
    workspace.mkdir()
    (home / ".codex" / "state_5.sqlite").write_bytes(b"broken")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_THREAD_ID", "root")

    result = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": "host-stop"},
    )

    assert result == {}
    heartbeat = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    receipt = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert receipt["codex_subagent_reconcile"]["degraded"] is True
    persisted = json.dumps(receipt["codex_subagent_reconcile"], ensure_ascii=True)
    assert "root_thread_id" not in persisted
    assert '"root"' not in persisted
    assert str(home) not in persisted
    assert "state_db_path" not in persisted
