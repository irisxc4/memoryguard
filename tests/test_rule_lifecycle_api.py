import json

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


def test_feedback_corrected_creates_narrow_child_and_exception_has_parent_link(tmp_path, monkeypatch):
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
    receipt_id = stable_hash("receipt", parent.memory_id)
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id, memory_id=parent.memory_id,
        share_group_id="team", agent_instance_id="a", task_hash="t",
        task="x", assignment_ids=[item.assignment_id for item in store.list_rule_assignments(parent.memory_id)],
        created_at=_now_iso(),
    ))

    corrected = service.submit_feedback(
        receipt_id, "corrected", "agent:a", evidence="当前项目仍需先运行测试", effective_context=context,
    )
    assert corrected.status == "created"
    assert corrected.parent_rule_id == parent.memory_id
    assert store.list_rule_assignments(corrected.memory_id)[0].target_type == "agent_project"

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
