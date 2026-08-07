"""Rule Intelligence read APIs must be physically read-only.

Bootstrap, canonical status, projection status and diagnostics are public
"read-only" surfaces.  Opening them must never create the rule-intelligence
database, initialize its schema, or mutate an existing database.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard import mcp_server as mcp_server_module
from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import execute_tool
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _rule_dir(workspace: Path) -> Path:
    return workspace / ".memoryguard" / "rule-intelligence"


def _rule_hashes(workspace: Path) -> dict[str, str]:
    root = _rule_dir(workspace)
    if not root.exists():
        return {}
    return {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _shared_hashes(workspace: Path) -> dict[str, str]:
    root = workspace / ".memoryguard" / "shared-memory"
    if not root.exists():
        return {}
    return {
        str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_read_only_store_never_initializes_rule_db(tmp_path):
    ws = tmp_path / "ws"
    store = RuleMergeStore(ws, read_only=True)

    assert not store.db_path.exists()
    assert not store.root.exists()
    with pytest.raises(PermissionError, match="rule_intelligence_store_read_only"):
        with store._write_conn():
            pass


def test_context_bootstrap_does_not_create_rule_store(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    legacy = SharedMemoryStore(ws, "default")
    legacy.append_record(SharedMemoryRecord(
        memory_id="src-folded",
        body="legacy folded rule",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        priority=10,
        agent_instance_id="agent-1",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    ), assignments=[{"target_type": "agent", "target_id": "agent-1"}])
    legacy.shadow_record("src-folded", reason="folded_into_canonical")

    AgentBindingStore(ws).bind_agent("agent-1", "default")

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(ws / "project"))
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_RUNTIME_ROLE", "subagent")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "session-1")
    monkeypatch.setenv("MEMORYGUARD_CONTEXT_HASH", "ctx-1")

    result = execute_tool("memoryguard_context_bootstrap", {
        "task": "read-only bootstrap test",
        "read_path": "auto",
    })
    assert result.get("isError") is not True, result
    assert not _rule_dir(ws).exists()


def test_rule_intelligence_read_apis_do_not_mutate_db(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    AgentBindingStore(ws).bind_agent("diag-agent", "default")
    SharedMemoryStore(ws, "default")
    RuleMergeStore(ws)

    before = _rule_hashes(ws)
    before_shared = _shared_hashes(ws)
    assert before
    assert before_shared

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(ws))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "diag-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(ws / "project"))
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_RUNTIME_ROLE", "subagent")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "session-1")
    monkeypatch.setenv("MEMORYGUARD_CONTEXT_HASH", "ctx-1")

    calls = [
        (
            "memoryguard_canonical_status",
            {"workspace": str(ws), "share_group_id": "default"},
        ),
        (
            "memoryguard_projection_status",
            {"workspace": str(ws), "share_group_id": "default"},
        ),
        (
            "memoryguard_diagnostics_snapshot",
            {"workspace": str(ws), "share_group_id": "default"},
        ),
        (
            "memoryguard_context_bootstrap",
            {"workspace": str(ws), "task": "read-only api test"},
        ),
    ]
    for name, args in calls:
        result = execute_tool(name, args)
        assert result.get("isError") is not True, (name, result)

    after = _rule_hashes(ws)
    after_shared = _shared_hashes(ws)
    assert after == before
    assert after_shared == before_shared


def test_rule_read_only_reader_observes_committed_concurrent_write(tmp_path):
    ws = tmp_path / "ws"
    writer = RuleMergeStore(ws)
    reader = RuleMergeStore(ws, read_only=True)
    conn = reader._db()
    try:
        conn.execute("SELECT definition_id FROM rule_definitions").fetchall()
        writer.upsert_definition(build_definition(
            "必须运行测试后再提交",
            definition_id="concurrent-rule",
        ))
        rows = conn.execute(
            "SELECT definition_id FROM rule_definitions",
        ).fetchall()
        assert "concurrent-rule" in {
            str(row["definition_id"]) for row in rows
        }
    finally:
        conn.close()


def test_shared_memory_read_only_old_schema_fails_without_mutation(tmp_path):
    ws = tmp_path / "ws"
    group = "legacy-read-only"
    db_path = (
        ws / ".memoryguard" / "shared-memory" / group / "memory.db"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, "
            "kind TEXT NOT NULL, status TEXT NOT NULL)"
        )

    with pytest.raises(RuntimeError, match="schema_upgrade_required"):
        SharedMemoryStore(ws, group, read_only=True)

    files = [
        path for path in db_path.parent.iterdir()
        if path.name != "memory.db"
    ]
    assert files == []


def test_context_bootstrap_write_classification():
    assert "memoryguard_context_bootstrap" in mcp_server_module._DB_WRITING_TOOLS
    assert "memoryguard_context_bootstrap" not in mcp_server_module._MUTATING_TOOLS


def test_context_bootstrap_runtime_lease_guard(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    healthy = {
        "lock": {"acquirable": True, "error": ""},
        "canonical": None,
    }
    monkeypatch.setattr(
        mcp_server_module,
        "_governance_diagnostics_state",
        lambda *args, **kwargs: healthy,
    )
    blocked = {
        "ok": False,
        "error": "runtime_split_brain",
        "restart_required": True,
    }
    monkeypatch.setattr(
        mcp_server_module,
        "_runtime_lease_guard",
        lambda *args, **kwargs: blocked,
    )
    result = execute_tool(
        "memoryguard_context_bootstrap",
        {"task": "lease check", "workspace": str(ws)},
    )
    assert result.get("error") == "runtime_split_brain"
