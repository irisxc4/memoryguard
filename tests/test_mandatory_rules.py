import pytest

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.agent_binding import AgentBindingStore
from memoryguard.host_hooks import _read_heartbeat, run_hook, set_hook_mode
from memoryguard.governance_engine import GovernanceEngine
import json
import sqlite3
from pathlib import Path

from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.shared_memory_store import MANDATORY_MAX_ITEMS, SharedMemoryStore


def _record(memory_id, body, *, policy="relevant", priority=0):
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=body,
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy=policy,
        priority=priority,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id="test-agent",
    )


def test_always_is_task_independent_and_does_not_consume_recall_budget(tmp_path):
    store = SharedMemoryStore(tmp_path, "mandatory-group")
    store.append_record(_record("always", "永远先运行隔离测试", policy="always", priority=4))
    store.append_record(_record("relevant", "database migration must run tests first"))
    store.append_record(_record("ordinary", "release procedure for unrelated deployment"))

    packet = build_context_packet(
        store, task="database migration", max_items=1, max_chars=256,
    )

    assert packet["mandatory_rule_ids"] == ["always"]
    assert packet["recalled_memory_ids"] == ["relevant"]
    assert packet["budget"]["used_items"] == 1
    assert packet["budget"]["mandatory_used_items"] == 1
    assert "ordinary" not in packet["recalled_memory_ids"]


def test_old_records_default_relevant_and_sensitive_always_is_not_leaked(tmp_path):
    store = SharedMemoryStore(tmp_path, "compat-group")
    old = SharedMemoryRecord.from_dict({
        "memory_id": "old", "body": "legacy migration procedure",
        "kind": "procedure", "status": "active",
    })
    assert old.injection_policy == "relevant"
    assert old.priority == 0
    store.append_record(old)
    store.append_record(_record("secret", "api_key=not-for-context", policy="always"))

    packet = build_context_packet(store, task="legacy migration")

    rendered = str(packet["context_packet"])
    assert "not-for-context" not in rendered
    assert "secret" not in packet["mandatory_rule_ids"]
    assert packet["selection"]["omitted"]["sensitive"] == 1
    assert packet["recalled_memory_ids"] == ["old"]


def test_sensitive_historical_mandatory_fails_closed_without_leaking_body(tmp_path):
    store = SharedMemoryStore(tmp_path, "sensitive-mandatory")
    store.append_record(_record("secret", "api_key=never-expose", policy="always"))
    packet = build_context_packet(store, task="anything")
    assert packet["mandatory_overflow"] is True
    assert packet["mandatory_invalid_reason"]
    assert "never-expose" not in str(packet)


def test_mandatory_limit_rejects_writes_and_legacy_overflow_fails_closed(tmp_path):
    store = SharedMemoryStore(tmp_path, "limit-group")
    for index in range(MANDATORY_MAX_ITEMS):
        store.append_record(_record(f"m{index}", f"mandatory rule {index}", policy="always"))
    with pytest.raises(ValueError, match="mandatory_rule_budget_exceeded"):
        store.append_record(_record("too-many", "one more mandatory rule", policy="always"))

    # Simulate pre-limit historical data.  Bootstrap must report the damage,
    # not silently truncate a rule package it cannot faithfully inject.
    with store._tx() as conn:
        store._insert_record(conn, _record("legacy-overflow", "old mandatory rule", policy="always"))
    packet = build_context_packet(store, task="anything")
    assert packet["mandatory_overflow"] is True
    assert packet["error"]
    assert packet["mandatory_rule_ids"] == []


def test_update_to_always_is_rejected_until_delete_releases_capacity(tmp_path):
    store = SharedMemoryStore(tmp_path, "update-limit-group")
    for index in range(MANDATORY_MAX_ITEMS):
        store.append_record(_record(f"full-{index}", f"rule {index}", policy="always"))
    store.append_record(_record("candidate", "candidate rule"))
    engine = GovernanceEngine(tmp_path, "update-limit-group", store=store)

    rejected = engine.agent_update(
        "candidate", actor="agent:test", injection_policy="always",
    )
    assert rejected["ok"] is False
    assert rejected["blocked_reason"].startswith("mandatory_rule_budget_exceeded")
    assert engine.agent_delete("full-0", actor="agent:test")["ok"] is True
    accepted = engine.agent_update(
        "candidate", actor="agent:test", injection_policy="always",
    )
    assert accepted["ok"] is True


def test_duplicate_body_different_injection_semantics_stays_distinct(tmp_path):
    store = SharedMemoryStore(tmp_path, "duplicate-policy")
    store.append_record(_record("first", "same durable rule", policy="always", priority=9))
    duplicate = _record("second", "same durable rule", policy="relevant", priority=-3)
    store.append_record(duplicate)
    assert duplicate.memory_id == "second"
    assert {item.memory_id for item in store.list_records()} == {
        "first", "second",
    }


def test_interactive_memory_records_offer_visible_injection_toggle():
    html = Path("src/memoryguard/interactive.py").read_text(encoding="utf-8")
    assert "强制规则每任务注入；按需记忆按相关性召回" in html
    assert "injection_policy === 'always'" in html
    assert "set_memory_injection_policy" in html
    assert "result.ok === false" in html
    assert "result.blocked_reason || '切换被拒绝'" in html
    assert "renderMemoryRecords()" in html


def test_readonly_open_old_schema_fails_closed_until_writable_migration(
    tmp_path, monkeypatch,
):
    group_id, agent_id = "legacy-sqlite", "legacy-agent"
    root = tmp_path / ".memoryguard" / "shared-memory" / group_id
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "memory.db"
    # A pre-feature production database has a records table but none of the
    # injection columns.  Remaining tables may be initialized on next writer.
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("""
            CREATE TABLE records (
              memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
              status TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
              conflict_group_id TEXT DEFAULT '', locked INTEGER NOT NULL DEFAULT 0,
              supersedes TEXT DEFAULT '[]', provenance TEXT DEFAULT '[]',
              agent_instance_id TEXT DEFAULT '', created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("legacy", "old persisted procedure", "procedure", "active", .8,
             "", 0, "[]", "[]", agent_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    # A read-only open must not take a write transaction to upgrade the DB.
    with pytest.raises(RuntimeError, match="schema_upgrade_required"):
        SharedMemoryStore(tmp_path, group_id, read_only=True).get_record("legacy")

    # The explicit writable open performs the migration, after which the same
    # database is safe to open read-only.
    migrated = SharedMemoryStore(tmp_path, group_id)
    assert migrated.get_record("legacy").injection_policy == "relevant"
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    readonly = SharedMemoryStore(tmp_path, group_id, read_only=True)
    assert readonly.get_record("legacy").injection_policy == "relevant"
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent_id)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    updated = execute_tool("memoryguard_memory_update", {
        "memory_id": "legacy", "injection_policy": "always", "priority": 100,
    })
    assert updated.get("isError") is not True, updated
    packet = build_context_packet(
        SharedMemoryStore(tmp_path, group_id, read_only=True), task="unrelated words",
    )
    assert packet["mandatory_rule_ids"] == ["legacy"]


def test_mcp_write_update_schema_exposes_persisted_injection_fields():
    tools = {item["name"]: item for item in TOOLS}
    for name in ("memoryguard_memory_write", "memoryguard_memory_update"):
        props = tools[name]["inputSchema"]["properties"]
        assert props["injection_policy"]["enum"] == ["relevant", "always"]
        assert props["priority"]["minimum"] == -100


def test_mcp_write_and_update_persist_injection_settings(tmp_path, monkeypatch):
    AgentBindingStore(tmp_path).bind_agent("trusted-agent", "trusted-group")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    written = execute_tool("memoryguard_memory_write", {
        "body": "mandatory mcp procedure for release verification",
        "kind": "procedure",
        "injection_policy": "always",
        "priority": 7,
    })
    assert written.get("isError") is not True, written
    write_result = json.loads(written["content"][0]["text"])
    memory_id = write_result["memory_id"]
    assert write_result["record"]["injection_policy"] == "always"

    updated = execute_tool("memoryguard_memory_update", {
        "memory_id": memory_id,
        "injection_policy": "relevant",
        "priority": -2,
    })
    assert updated.get("isError") is not True, updated
    update_result = json.loads(updated["content"][0]["text"])
    assert update_result["record"]["injection_policy"] == "relevant"
    assert update_result["record"]["priority"] == -2


def test_cursor_session_start_injects_mandatory_rules_and_receipt_ids(tmp_path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    agent_id, group_id = "cursor-agent", "cursor-rules"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(workspace, group_id)
    store.append_record(_record("cursor-always", "Cursor 固定会话也必须执行此规则", policy="always"))

    store.set_rule_assignments("cursor-always", [{
        "target_type": "agent", "target_id": agent_id,
    }])

    result = run_hook(
        provider="cursor", event="session_start", workspace=workspace,
        agent_instance_id=agent_id, share_group_id=group_id,
        payload={"session_id": "cursor-session"},
    )

    assert "MemoryGuard 强制规则（必须遵循）" in result["additional_context"]
    assert "Cursor 固定会话也必须执行此规则" in result["additional_context"]
    receipt = _read_heartbeat(workspace, "cursor", agent_id)
    assert receipt["mandatory_rule_ids"] == ["cursor-always"]
    assert receipt["mandatory_overflow"] is False


def test_hook_stops_on_historical_mandatory_overflow(tmp_path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    agent_id, group_id = "codex-agent", "overflow-rules"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(workspace, group_id)
    with store._tx() as conn:
        for index in range(MANDATORY_MAX_ITEMS + 1):
            record = _record(
                f"overflow-{index}", f"legacy rule {index}",
                policy="always",
            )
            record.agent_instance_id = agent_id
            store._insert_record(conn, record)
            store._insert_assignments(
                conn, record.memory_id,
                store._default_assignments(record),
            )
    set_hook_mode(workspace, "codex", agent_id, "enforce")

    prompt_result = run_hook(
        provider="codex", event="user_prompt", workspace=workspace,
        agent_instance_id=agent_id, share_group_id=group_id,
        payload={"session_id": "overflow-session", "prompt": "implement feature"},
    )
    context = prompt_result["hookSpecificOutput"]["additionalContext"]
    assert "强制规则包异常，停止继续执行" in context
    denied = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id=agent_id, share_group_id=group_id,
        payload={"session_id": "overflow-session", "tool_name": "shell_command"},
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_subagent_overflow_is_persisted_and_denies_next_tool(tmp_path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    agent_id, group_id = "codex-agent", "subagent-overflow"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(workspace, group_id)
    with store._tx() as conn:
        for index in range(MANDATORY_MAX_ITEMS + 1):
            record = _record(
                f"subagent-overflow-{index}",
                f"legacy subagent rule {index}",
                policy="always",
            )
            record.agent_instance_id = agent_id
            store._insert_record(conn, record)
            store._insert_assignments(
                conn, record.memory_id,
                store._default_assignments(record),
            )
    set_hook_mode(workspace, "codex", agent_id, "enforce")

    run_hook(
        provider="codex", event="subagent_start", workspace=workspace,
        agent_instance_id=agent_id, share_group_id=group_id,
        payload={
            "session_id": "subagent-overflow-session",
            "task": "implement feature",
        },
    )
    denied = run_hook(
        provider="codex", event="pre_tool", workspace=workspace,
        agent_instance_id=agent_id, share_group_id=group_id,
        payload={
            "session_id": "subagent-overflow-session",
            "tool_name": "shell_command",
        },
    )

    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
