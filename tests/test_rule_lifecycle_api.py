import json
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import TOOLS, execute_tool
from memoryguard.rule_creation import RuleCreationService
from memoryguard.schema_v3 import EffectiveAgentContext, RuleMatchReceipt, _now_iso, stable_hash
from memoryguard.shared_memory_store import SharedMemoryStore


def _bind(tmp_path, monkeypatch, agent="a"):
    AgentBindingStore(tmp_path).bind_agent(agent, "team")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    return SharedMemoryStore(tmp_path, "team")


def _payload(response):
    assert response.get("isError") is not True, response
    return json.loads(response["content"][0]["text"])


def test_mcp_auto_rule_tool_and_scope_stats(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    names = {item["name"] for item in TOOLS}
    assert "memoryguard_rule_create_auto" in names
    created = _payload(execute_tool("memoryguard_rule_create_auto", {
        "text": "不要跳过测试",
    }))
    assert created["status"] == "created"
    assert created["target_type"] == "agent_project"

    stats = _payload(execute_tool("memoryguard_rule_scope_stats", {}))
    assert stats["by_target_type"]["agent_project"] == 1

    decision = _payload(execute_tool("memoryguard_rule_decision_read", {
        "decision_id": created["decision_id"],
    }))
    assert decision["decision_id"] == created["decision_id"]


def test_exception_feedback_excludes_parent_and_corrected_is_only_recorded(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    context = EffectiveAgentContext(
        agent_instance_id="a", share_group_id="team",
        project_ref=str(tmp_path / "project"),
    )
    service = RuleCreationService(tmp_path, "team", store=store)
    parent = service.create_rule_from_text(
        "始终先运行测试", context,
        requested_scope={"target_type": "agent", "target_id": "a"},
    )
    parent_before = store.list_rule_assignments(parent.memory_id)
    assert any(item.target_type == "agent" and item.effect == "include" for item in parent_before)
    assert not any(item.effect == "exclude" for item in parent_before)

    # "corrected" is a content-level correction, not a scope-error signal: it is recorded
    # as an event (P0-2) and must NOT trigger narrowing (P0-3 narrows only on not_applicable).
    receipt_id = stable_hash("receipt", parent.memory_id)
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id, memory_id=parent.memory_id,
        share_group_id="team", agent_instance_id="a", task_hash="t",
        task="x", assignment_ids=[item.assignment_id for item in parent_before],
        created_at=_now_iso(),
    ))
    corrected = service.submit_feedback(
        receipt_id, "corrected", "agent:a", evidence="当前项目仍需先运行测试", effective_context=context,
    )
    assert corrected.status == "recorded"
    assert store.get_rule_match_feedback_by_receipt(receipt_id).outcome == "corrected"
    # corrected does not narrow: the parent rule's assignments are unchanged.
    assert [item.to_dict() for item in store.list_rule_assignments(parent.memory_id)] == [
        item.to_dict() for item in parent_before
    ]

    # "exception" creates a child exception rule and adds a parent exclude for that project
    # in the same transaction, so the original stops applying there (P0-5).
    exception_receipt_id = stable_hash("receipt", parent.memory_id, "exception")
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=exception_receipt_id, memory_id=parent.memory_id,
        share_group_id="team", agent_instance_id="a", task_hash="t2",
        task="x", assignment_ids=[item.assignment_id for item in store.list_rule_assignments(parent.memory_id)],
        created_at=_now_iso(),
    ))
    exception = service.submit_feedback(
        exception_receipt_id, "exception", "agent:a", evidence="当前项目允许跳过测试", effective_context=context,
    )
    assert exception.status == "created"
    assert exception.parent_rule_id == parent.memory_id
    assert all(item.effect == "include" for item in store.list_rule_assignments(exception.memory_id))
    parent_after = store.list_rule_assignments(parent.memory_id)
    # The store normalizes project paths (lowercase, forward slashes), so compare normalized.
    def _norm_project(value: str) -> str:
        return str(Path(value or "")).replace("\\", "/").lower()
    assert any(
        item.effect == "exclude" and item.target_type == "agent_project"
        and item.target_id == "a"
        and _norm_project(item.project_ref) == _norm_project(context.project_ref)
        for item in parent_after
    ), "exception must exclude the parent rule from the offending project (P0-5)"


def test_atomic_rule_create_dedup_undo_preserves_original_rule(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    context = EffectiveAgentContext(
        agent_instance_id="a", share_group_id="team",
        project_ref=str(tmp_path / "project"), session_id="s-1",
    )
    service = RuleCreationService(tmp_path, "team", store=store)
    first = service.create_rule_from_text("始终先运行定向测试", context)
    second = service.create_rule_from_text("始终先运行定向测试", context)

    assert first.status == "created"
    assert second.status == "created"
    assert second.memory_id == first.memory_id
    assert second.metadata["mutation_kind"] == "deduplicated"
    undone = service.undo_rule_decision(second.decision_id, context)
    assert undone.status == "undone"
    original = store.get_record(first.memory_id)
    assert original is not None
    assert original.status.value == "active"


def test_non_created_lifecycle_decisions_are_atomic_and_reversible(
    tmp_path, monkeypatch,
):
    store = _bind(tmp_path, monkeypatch)
    context = EffectiveAgentContext(
        agent_instance_id="a", share_group_id="team",
        project_ref=str(tmp_path / "project"), session_id="lifecycle-session",
    )
    service = RuleCreationService(tmp_path, "team", store=store)

    # A correction supersedes the old record in the same mutation bundle.
    original = service.create_rule_from_text("始终先运行定向测试", context)
    superseded = service.create_rule_from_text("纠正：始终先运行定向测试", context)
    assert superseded.action == "rule_superseded"
    assert superseded.metadata["mutation_kind"] == "superseded"
    assert store.get_record(original.memory_id).status.value == "shadowed"
    undone = service.undo_rule_decision(superseded.decision_id, context)
    assert undone.status == "undone"
    assert store.get_record(superseded.memory_id).status.value == "deleted"
    assert store.get_record(original.memory_id).status.value == "active"

    # A conflict records the group and can be undone without touching unrelated
    # records; the structured decision carries the fixed revision hash.
    first_preference = service.create_rule_from_text(
        "偏好：项目统一使用 pnpm 进行依赖安装", context,
    )
    conflict = service.create_rule_from_text(
        "偏好：项目统一使用 npm 进行依赖安装", context,
    )
    assert conflict.action == "rule_conflicted"
    assert conflict.metadata["mutation_kind"] == "conflicted"
    assert store.get_record(conflict.memory_id).status.value == "conflicted"
    conflict_undo = service.undo_rule_decision(conflict.decision_id, context)
    assert conflict_undo.status == "undone"
    assert store.get_record(conflict.memory_id).status.value == "deleted"
    assert store.get_record(first_preference.memory_id).status.value in {
        "active", "shadowed", "conflicted",
    }

    # A secret is quarantined without sending raw content through enrichment;
    # undo creates a released tombstone rather than reactivating the secret.
    quarantined = service.create_rule_from_text(
        "临时 api_key=do-not-leak", context,
    )
    assert quarantined.action == "rule_quarantined"
    assert quarantined.metadata["mutation_kind"] == "quarantined"
    assert store.get_record(quarantined.memory_id).status.value == "quarantined"
    quarantine_undo = service.undo_rule_decision(quarantined.decision_id, context)
    assert quarantine_undo.status == "undone"
    assert store.get_record(quarantined.memory_id).status.value == "deleted"
    assert any(
        item.memory_id == quarantined.memory_id and item.released
        for item in store.list_quarantine()
    )
