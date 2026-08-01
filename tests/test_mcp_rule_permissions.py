import json

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import execute_tool
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _bind(tmp_path, monkeypatch, agent="a"):
    AgentBindingStore(tmp_path).bind_agent(agent, "team")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    return SharedMemoryStore(tmp_path, "team")


def _rule(memory_id, writer):
    return SharedMemoryRecord(
        memory_id=memory_id, body=f"rule {memory_id}",
        kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
        injection_policy="always", agent_instance_id=writer,
    )


def test_nonadmin_cannot_update_or_delete_cross_agent_or_group_rule(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    store.append_record(_rule("other", "b"))
    store.append_record(_rule("group", "admin"), assignments=[{
        "target_type": "group", "target_id": "team",
    }])
    before = {item.memory_id: item.to_dict() for item in store.list_records()}
    for memory_id in ("other", "group"):
        assert execute_tool("memoryguard_memory_update", {
            "memory_id": memory_id, "body": "poison",
        })["isError"] is True
        assert execute_tool("memoryguard_memory_delete", {
            "memory_id": memory_id,
        })["isError"] is True
    after = {item.memory_id: item.to_dict() for item in store.list_records()}
    assert after == before


def test_nonadmin_self_rule_and_owned_relevant_memory_are_mutable(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    store.append_record(_rule("self", "a"))
    relevant = _rule("relevant", "a")
    relevant.injection_policy = "relevant"
    store.append_record(relevant)
    assert execute_tool("memoryguard_memory_update", {
        "memory_id": "self", "priority": 7,
    }).get("isError") is not True
    transition = next(
        item for item in store.list_decisions()
        if item.action == "agent_rule_transition"
    )
    assert transition.actor == "agent:a"
    assert "rule self" not in transition.reason
    assert execute_tool("memoryguard_memory_update", {
        "memory_id": "relevant", "body": "owned update",
    }).get("isError") is not True


def test_invalid_audience_fails_before_record_or_event_write(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    before = store.status()
    result = execute_tool("memoryguard_memory_write", {
        "body": "unauthorized group rule", "kind": "procedure",
        "injection_policy": "always",
        "audience": [{"target_type": "group", "target_id": "team"}],
    })
    assert result["isError"] is True
    after = store.status()
    assert after["total_records"] == before["total_records"]
    assert after["total_events"] == before["total_events"]


def test_agent_audience_cannot_smuggle_project_scope(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    before = store.status()

    result = execute_tool("memoryguard_memory_write", {
        "body": "invalid agent plus project rule",
        "kind": "procedure",
        "injection_policy": "always",
        "audience": [{
            "target_type": "agent",
            "target_id": "a",
            "project_ref": r"C:\Work\Demo",
        }],
    })

    assert result["isError"] is True
    after = store.status()
    assert after["total_records"] == before["total_records"]
    assert after["total_events"] == before["total_events"]
    assert after["total_decisions"] == before["total_decisions"]


def test_self_agent_project_rule_create_update_delete_round_trip(
    tmp_path, monkeypatch,
):
    store = _bind(tmp_path, monkeypatch)
    created = execute_tool("memoryguard_memory_write", {
        "body": "self project rule",
        "kind": "procedure",
        "injection_policy": "always",
        "audience": [{
            "target_type": "agent_project",
            "target_id": "a",
            "project_ref": r"C:\Work\Demo",
        }],
    })
    assert created.get("isError") is not True
    payload = json.loads(created["content"][0]["text"])
    memory_id = payload["memory_id"]
    assignment = store.list_rule_assignments(memory_id)[0]
    assert assignment.target_type == "agent_project"
    assert assignment.project_ref == canonical_project_ref("c:/work/demo")

    updated = execute_tool("memoryguard_memory_update", {
        "memory_id": memory_id,
        "priority": 9,
    })
    assert updated.get("isError") is not True
    deleted = execute_tool("memoryguard_memory_delete", {
        "memory_id": memory_id,
    })
    assert deleted.get("isError") is not True


def test_rule_feedback_requires_receipt_actor_and_valid_outcome(tmp_path, monkeypatch):
    _bind(tmp_path, monkeypatch)
    response = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": "receipt-1",
        "outcome": "invalid",
        "actor": "hook",
    })
    assert response["isError"] is True
    assert "outcome must be one of" in response["content"][0]["text"]

    response = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": "receipt-1",
        "outcome": "followed",
        "actor": "",
    })
    assert response["isError"] is True
    assert "actor is required" in response["content"][0]["text"]


def test_rule_feedback_idempotent_by_receipt(tmp_path, monkeypatch):
    store = _bind(tmp_path, monkeypatch)
    receipt_id = stable_hash("rule-bootstrap-receipt", "team", "a", "rule-feedback-task", "mem")
    store.append_record(_rule("mem", "a"))
    store.set_rule_assignments("mem", [{
        "target_type": "agent", "target_id": "a",
    }])
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id,
        memory_id="mem",
        share_group_id="team",
        agent_instance_id="a",
        task_hash="task-hash",
        task="rule-feedback-task",
        assignment_ids=["agent:a"],
        created_at=_now_iso(),
    ))

    first = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": receipt_id,
        "outcome": "followed",
        "actor": "hook:codex:a",
        "confidence": 0.95,
        "evidence": "已执行",
    })
    assert first.get("isError") is not True
    second = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": receipt_id,
        "outcome": "followed",
        "actor": "hook:codex:a",
        "confidence": 0.95,
        "evidence": "已执行",
    })
    assert second.get("isError") is not True

    feedbacks = store.list_rule_match_feedbacks(receipt_id=receipt_id)
    assert len(feedbacks) == 1


def test_mcp_receipt_without_session_cannot_drive_narrowing(tmp_path, monkeypatch):
    """MCP feedback must not narrow a rule when receipts lack host sessions.

    Session identity is a trusted host fact.  Three otherwise-valid
    ``not_applicable`` events with an empty session must remain pending and
    leave the parent assignment unchanged; an actor cannot manufacture the
    cross-session evidence required for automatic narrowing.
    """
    store = _bind(tmp_path, monkeypatch)
    project_ref = str(tmp_path / "project")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", project_ref)
    monkeypatch.delenv("MEMORYGUARD_SESSION_ID", raising=False)
    store.append_record(_rule("parent", "a"), assignments=[{
        "target_type": "agent", "target_id": "a",
    }])
    before = [item.to_dict() for item in store.list_rule_assignments("parent")]

    for index in range(3):
        receipt_id = f"no-session-{index}"
        store.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=receipt_id,
            memory_id="parent",
            share_group_id="team",
            agent_instance_id="a",
            task_hash=receipt_id,
            task="task without host session",
            project_ref=project_ref,
            session_id="",
            created_at=_now_iso(),
        ))
        response = execute_tool("memoryguard_rule_feedback", {
            "receipt_id": receipt_id,
            "outcome": "not_applicable",
            "actor": "hook:codex:a",
            "confidence": 1.0,
        })
        assert response.get("isError") is not True, response
        payload = json.loads(response["content"][0]["text"])
        assert payload["status"] == "pending"
        assert payload["scope_reason"] in {
            "not_applicable_not_enough_evidence",
            "not_applicable_not_enough_sessions",
        }

    after = [item.to_dict() for item in store.list_rule_assignments("parent")]
    assert after == before
