import json

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import execute_tool
from memoryguard.rule_creation import RuleCreationService
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _setup(tmp_path, monkeypatch, agent="a"):
    AgentBindingStore(tmp_path).bind_agent(agent, "team")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    # MCP feedback scope is the trusted host project.  It must match the
    # receipt's original project or the feedback is rejected (cross-project
    # evidence must never mutate another project's rule).
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path / "project"))
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(SharedMemoryRecord(
        memory_id="rule-1", body="始终先运行测试", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        agent_instance_id="a",
    ), assignments=[{"target_type": "agent", "target_id": "a"}])
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="receipt-1", memory_id="rule-1", share_group_id="team",
        agent_instance_id="a", task_hash="task", task="task",
        assignment_ids=[], project_ref=str(tmp_path / "project"),
        session_id="session-a", created_at=_now_iso(),
    ))
    return store


def _context(tmp_path, agent="a"):
    return EffectiveAgentContext(
        agent_instance_id=agent, share_group_id="team",
        project_ref=str(tmp_path / "project"), session_id="session-a",
    )


def test_mcp_actor_cannot_upgrade_authority(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch)
    result = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": "receipt-1", "outcome": "not_applicable",
        "actor": "user", "evidence": "agent supplied display text",
    })
    assert result.get("isError") is not True
    feedback = store.list_rule_match_feedbacks(receipt_id="receipt-1")[0]
    assert feedback.source == "agent"
    assert feedback.authority == 3


def test_feedback_owner_is_bound_to_receipt_agent(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch)
    service = RuleCreationService(tmp_path, "team", store=store)
    decision = service.submit_feedback(
        "receipt-1", "followed", "user", effective_context=_context(tmp_path, "b"),
        producer="user",
    )
    assert decision.status == "blocked"
    assert "receipt owner" in decision.blocked_reason
    assert store.list_rule_match_feedbacks(receipt_id="receipt-1") == []


def test_lower_authority_feedback_is_recorded_but_not_effective(tmp_path, monkeypatch):
    store = _setup(tmp_path, monkeypatch)
    service = RuleCreationService(tmp_path, "team", store=store)
    context = _context(tmp_path)
    first = service.submit_feedback(
        "receipt-1", "followed", "human", effective_context=context,
        producer="user",
    )
    assert first.status == "recorded"
    second = service.submit_feedback(
        "receipt-1", "not_applicable", "agent says user", effective_context=context,
        producer="agent",
    )
    assert second.status == "recorded"
    assert second.after["metadata"]["effective"] is False
    effective = store.get_effective_rule_match_feedback("receipt-1")
    assert effective is not None
    assert effective.outcome == "followed"
    assert len(store.list_rule_match_feedbacks(receipt_id="receipt-1")) == 2
