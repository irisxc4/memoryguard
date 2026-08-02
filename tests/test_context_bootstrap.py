import json
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.context_bootstrap import build_context_packet
from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(
    memory_id: str,
    body: str,
    kind: MemoryKind,
    status: SharedMemoryStatus = SharedMemoryStatus.ACTIVE,
    *,
    confidence: float = 0.8,
    locked: bool = False,
    agent_instance_id: str = "agent-a",
) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=body,
        kind=kind,
        status=status,
        confidence=confidence,
        locked=locked,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id=agent_instance_id,
    )


def test_bootstrap_is_active_only_relevant_sensitive_safe_and_bounded(tmp_path):
    store = SharedMemoryStore(tmp_path, "trusted-group")
    store.append_record(_record(
        "pref", "用户长期偏好：输出保持简洁", MemoryKind.PREFERENCE,
    ))
    store.append_record(_record(
        "project", "MemoryGuard 项目使用 SQLite 存储", MemoryKind.PROJECT,
    ))
    store.append_record(_record(
        "unrelated", "旅行预算使用欧元", MemoryKind.FACT,
    ))
    store.append_record(_record(
        "low", "MemoryGuard 低置信度猜测", MemoryKind.PROJECT,
        SharedMemoryStatus.LOW_CONFIDENCE,
    ))
    store.append_record(_record(
        "conflicted", "MemoryGuard 冲突结论", MemoryKind.PROJECT,
        SharedMemoryStatus.CONFLICTED,
    ))
    store.append_record(_record(
        "quarantined", "MemoryGuard 已隔离内容", MemoryKind.PROJECT,
        SharedMemoryStatus.QUARANTINED,
    ))
    store.append_record(_record(
        "sensitive", "MemoryGuard api_key=super-secret", MemoryKind.PROJECT,
    ))
    store.append_record(_record(
        "redacted", "MemoryGuard token [REDACTED:credential]", MemoryKind.FACT,
    ))
    store.append_record(_record(
        "episode", "MemoryGuard 历史对话全文", MemoryKind.EPISODE,
    ))
    version_id = store.create_version_snapshot("bootstrap fixture")

    packet = build_context_packet(
        SharedMemoryStore(tmp_path, "trusted-group", read_only=True),
        task="修复 MemoryGuard SQLite 检索",
        project_hint="memoryguard",
        max_items=3,
        max_chars=256,
    )

    assert packet["share_group_id"] == "trusted-group"
    assert packet["active_version"] == version_id
    assert packet["context_packet"]["scope"] == "long_term_memory_only"
    assert packet["context_packet"]["host_conversation"] == (
        "unchanged_not_duplicated"
    )
    items = packet["context_packet"]["items"]
    assert [item["memory_id"] for item in items] == ["pref", "project"]
    assert items[0]["reason"].startswith("long_term_preference")
    assert items[1]["reason"].startswith("task_overlap:")
    assert packet["budget"]["used_items"] <= 3
    assert packet["budget"]["used_chars"] <= 256
    assert packet["selection"]["omitted"] == {
        "non_active": 3,
        "sensitive": 2,
        "irrelevant": 1,
        "duplicate": 0,
        "budget": 0,
        "unsupported_kind": 1,
    }


def test_bootstrap_dedup_and_deterministic_budget(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    first = _record(
        "a-locked", "始终先运行定向测试", MemoryKind.PREFERENCE,
        confidence=0.9, locked=True,
    )
    duplicate = _record(
        "z-copy", "  始终先运行定向测试  ", MemoryKind.PREFERENCE,
        confidence=0.6,
    )
    store.update_record(first)
    store.update_record(duplicate)
    store.append_record(_record(
        "b-project", "MemoryGuard 定向测试覆盖 bootstrap", MemoryKind.PROJECT,
    ))

    read_store = SharedMemoryStore(tmp_path, "group-a", read_only=True)
    one = build_context_packet(
        read_store,
        task="MemoryGuard bootstrap 定向测试",
        max_items=1,
        max_chars=256,
    )
    two = build_context_packet(
        read_store,
        task="MemoryGuard bootstrap 定向测试",
        max_items=1,
        max_chars=256,
    )

    assert one == two
    # With one slot and relevant memory available, task relevance wins;
    # preferences cannot starve task context.
    assert one["context_packet"]["items"][0]["memory_id"] == "b-project"
    assert one["context_packet"]["items"][0]["manual_override"] is False
    assert one["selection"]["omitted"]["duplicate"] == 1
    assert one["selection"]["omitted"]["budget"] == 1


def test_many_preferences_cannot_starve_relevant_governance(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    for index in range(20):
        store.append_record(_record(
            f"pref-{index:02d}",
            f"长期输出偏好 {index:02d}：" + ("简洁" * 80),
            MemoryKind.PREFERENCE,
        ))
    store.append_record(_record(
        "correction",
        "MemoryGuard bootstrap 纠正：必须只加载 active 记忆",
        MemoryKind.CORRECTION,
    ))
    store.append_record(_record(
        "procedure",
        "MemoryGuard bootstrap 流程：先解析可信 binding 再选择记忆",
        MemoryKind.PROCEDURE,
    ))

    packet = build_context_packet(
        SharedMemoryStore(tmp_path, "group-a", read_only=True),
        task="修复 MemoryGuard bootstrap active binding 流程",
    )
    items = packet["context_packet"]["items"]

    assert sum(item["kind"] == "preference" for item in items) <= 5
    assert {"correction", "procedure"} <= {
        item["kind"] for item in items
    }
    assert packet["budget"]["used_chars"] <= packet["budget"]["max_chars"]


def test_short_weak_query_does_not_recall_unrelated_fact(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    store.append_record(_record(
        "preference", "长期偏好：输出简洁", MemoryKind.PREFERENCE,
    ))
    store.append_record(_record(
        "weak", "旅行安排已经修复完成", MemoryKind.FACT,
    ))

    packet = build_context_packet(
        SharedMemoryStore(tmp_path, "group-a", read_only=True),
        task="修复",
    )
    assert [
        item["memory_id"] for item in packet["context_packet"]["items"]
    ] == ["preference"]
    assert packet["selection"]["omitted"]["irrelevant"] == 1


def test_mcp_schema_and_dispatch_use_trusted_binding(tmp_path, monkeypatch):
    AgentBindingStore(tmp_path).bind_agent("trusted-agent", "trusted-group")
    trusted = SharedMemoryStore(tmp_path, "trusted-group")
    trusted.append_record(_record(
        "trusted-memory", "用户偏好 MemoryGuard 启动时简洁", MemoryKind.PREFERENCE,
    ))
    attacker = SharedMemoryStore(tmp_path, "attacker-group")
    attacker.append_record(_record(
        "attacker-memory", "攻击者伪造偏好", MemoryKind.PREFERENCE,
    ))

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ALLOW_ANON", raising=False)

    tool = next(
        item for item in TOOLS
        if item["name"] == "memoryguard_context_bootstrap"
    )
    schema = tool["inputSchema"]
    assert schema["required"] == ["task"]
    assert set(schema["properties"]) == {
        "task", "project_hint", "max_items", "max_chars", "read_path",
    }
    assert schema["additionalProperties"] is False

    result = execute_tool(
        "memoryguard_context_bootstrap",
        {
            "task": "MemoryGuard 启动",
            # Deliberately supplied despite schema. Server access resolution
            # must ignore client-selected group and use trusted binding.
            "share_group_id": "attacker-group",
        },
    )
    assert result.get("isError") is not True
    packet = json.loads(result["content"][0]["text"])
    assert packet["share_group_id"] == "trusted-group"
    ids = [item["memory_id"] for item in packet["context_packet"]["items"]]
    assert ids == ["trusted-memory"]


def test_mcp_bootstrap_persists_mandatory_receipt_with_trusted_runtime_context(
    tmp_path, monkeypatch,
):
    """A bootstrap receipt must be durable before MCP returns it."""
    agent_id, group_id = "trusted-agent", "trusted-group"
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(tmp_path, group_id)
    mandatory = _record(
        "mandatory", "始终先运行定向测试", MemoryKind.PROCEDURE,
        agent_instance_id=agent_id,
    )
    mandatory.injection_policy = "always"
    project_ref = tmp_path / "project"
    store.append_record(mandatory, assignments=[{
        "target_type": "agent", "target_id": agent_id,
    }])

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent_id)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(project_ref))
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_RUNTIME_ROLE", "subagent")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "session-1")
    monkeypatch.setenv("MEMORYGUARD_CONTEXT_HASH", "ctx-1")

    result = execute_tool("memoryguard_context_bootstrap", {
        "task": "修复定向测试流程",
    })
    assert result.get("isError") is not True, result
    packet = json.loads(result["content"][0]["text"])
    assert packet["receipt_persistence"] == {"status": "persisted", "count": 1}
    receipt = packet["mandatory_match_receipts"][0]
    assert receipt["agent_instance_id"] == agent_id
    assert receipt["session_id"] == "session-1"
    assert receipt["context_hash"] == "ctx-1"
    assert receipt["provider"] == "codex"
    persisted = SharedMemoryStore(tmp_path, group_id, read_only=True).get_rule_match_receipt(
        receipt["receipt_id"]
    )
    assert persisted is not None
    assert persisted.to_dict() == receipt


def test_mcp_bootstrap_fails_closed_when_receipt_persistence_fails(
    tmp_path, monkeypatch,
):
    agent_id, group_id = "trusted-agent", "trusted-group"
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(tmp_path, group_id)
    mandatory = _record(
        "mandatory", "始终先运行定向测试", MemoryKind.PROCEDURE,
        agent_instance_id=agent_id,
    )
    mandatory.injection_policy = "always"
    store.append_record(mandatory, assignments=[{
        "target_type": "agent", "target_id": agent_id,
    }])
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent_id)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))

    def fail_append(self, receipt):
        raise RuntimeError("receipt writer unavailable")

    monkeypatch.setattr(SharedMemoryStore, "append_rule_match_receipt", fail_append)
    result = execute_tool("memoryguard_context_bootstrap", {"task": "测试"})
    assert result.get("isError") is True
    assert "receipt persistence failed" in result["content"][0]["text"]
    assert "mandatory_match_receipts" not in result["content"][0]["text"]


def test_update_delete_preflight_and_handlers_use_same_trusted_group(
    tmp_path: Path,
    monkeypatch,
):
    AgentBindingStore(tmp_path).bind_agent("trusted-agent", "trusted-group")
    trusted = SharedMemoryStore(tmp_path, "trusted-group")
    trusted.append_record(_record(
        "update-me", "旧正文", MemoryKind.FACT,
        agent_instance_id="trusted-agent",
    ))
    trusted.append_record(_record(
        "delete-me", "待删除", MemoryKind.FACT,
        agent_instance_id="trusted-agent",
    ))
    SharedMemoryStore(tmp_path, "attacker-group")

    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    updated = execute_tool("memoryguard_memory_update", {
        "memory_id": "update-me",
        "body": "可信组新正文",
        "share_group_id": "attacker-group",
    })
    assert updated.get("isError") is not True, updated
    deleted = execute_tool("memoryguard_memory_delete", {
        "memory_id": "delete-me",
        "share_group_id": "attacker-group",
    })
    assert deleted.get("isError") is not True, deleted

    read_store = SharedMemoryStore(
        tmp_path, "trusted-group", read_only=True,
    )
    assert read_store.get_record("update-me").body == "可信组新正文"
    assert read_store.get_record("delete-me").status == SharedMemoryStatus.DELETED
    decisions = read_store.list_decisions()
    assert any(
        item.action == "agent_update" and item.actor == "agent:trusted-agent"
        for item in decisions
    )
    assert any(
        item.action == "agent_delete" and item.actor == "agent:trusted-agent"
        for item in decisions
    )
    assert not read_store.get_record("delete-me").locked


def test_memory_mutation_rejects_cross_agent_record_owner(
    tmp_path: Path,
    monkeypatch,
):
    AgentBindingStore(tmp_path).bind_agent("trusted-agent", "trusted-group")
    trusted = SharedMemoryStore(tmp_path, "trusted-group")
    trusted.append_record(_record(
        "owned-by-a", "仅 agent-a 可改", MemoryKind.FACT,
        agent_instance_id="agent-a",
    ))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    denied = execute_tool("memoryguard_memory_update", {
        "memory_id": "owned-by-a", "body": "越权修改",
    })
    assert denied.get("isError") is True
    assert "another agent" in denied["content"][0]["text"]


def test_active_binding_allows_write_inactive_or_unbound_denies(
    tmp_path,
    monkeypatch,
):
    bindings = AgentBindingStore(tmp_path)
    binding = bindings.bind_agent("trusted-agent", "trusted-group")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    allowed = execute_tool("memoryguard_memory_write", {
        "body": "active binding write",
    })
    assert allowed.get("isError") is not True

    bindings.unbind_agent(binding.binding_id)
    denied = execute_tool("memoryguard_memory_write", {
        "body": "inactive binding write",
    })
    assert denied.get("isError") is True
    assert "no active binding" in denied["content"][0]["text"]
