from __future__ import annotations

import json
import sqlite3

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import handle_request
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.shared_memory_store import SharedMemoryStore


def _mandatory_record(memory_id: str, agent_id: str) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body="必须先运行测试再提交代码",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id=agent_id,
    )


def _bootstrap_fixture(tmp_path, monkeypatch, *, session_source: str | None):
    agent_id, group_id = "mcp-agent", "mcp-group"
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    SharedMemoryStore(tmp_path, group_id).append_record(
        _mandatory_record("mandatory-rule", agent_id),
        assignments=[{"target_type": "agent", "target_id": agent_id}],
    )
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent_id)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "host-session-1")
    if session_source is None:
        monkeypatch.delenv("MEMORYGUARD_SESSION_SOURCE", raising=False)
    else:
        monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", session_source)
    return group_id


def _packet(result: dict) -> dict:
    assert result.get("isError") is not True, result
    return json.loads(result["content"][0]["text"])


def _mcp_call(name: str, arguments: dict) -> dict:
    response = handle_request({
        "jsonrpc": "2.0",
        "id": name,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response is not None
    assert response["id"] == name
    return response["result"]


def test_mcp_bootstrap_persists_host_session_provenance_and_feedback(tmp_path, monkeypatch):
    group_id = _bootstrap_fixture(tmp_path, monkeypatch, session_source="host")

    packet = _packet(_mcp_call(
        "memoryguard_context_bootstrap", {"task": "提交代码前运行测试"},
    ))
    receipt_data = packet["mandatory_match_receipts"][0]
    assert receipt_data["session_id"] == "host-session-1"
    assert receipt_data["session_source"] == "host"
    assert receipt_data["session_trusted"] is True

    store = SharedMemoryStore(tmp_path, group_id, read_only=True)
    receipt = store.get_rule_match_receipt(receipt_data["receipt_id"])
    assert receipt is not None
    assert receipt.session_trusted is True
    assert receipt.session_source == "host"

    feedback = _mcp_call("memoryguard_rule_feedback", {
        "receipt_id": receipt.receipt_id,
        "outcome": "followed",
        "actor": "ignored-client-actor",
    })
    assert feedback.get("isError") is not True, feedback
    with sqlite3.connect(store.db_path) as conn:
        event = conn.execute(
            "SELECT session_id, session_source, session_trusted "
            "FROM rule_event_outbox WHERE receipt_id=?",
            (receipt.receipt_id,),
        ).fetchone()
    assert event == ("host-session-1", "host", 1)


def test_session_id_without_trusted_source_remains_untrusted(tmp_path, monkeypatch):
    group_id = _bootstrap_fixture(tmp_path, monkeypatch, session_source=None)

    packet = _packet(_mcp_call(
        "memoryguard_context_bootstrap", {"task": "提交代码前运行测试"},
    ))
    receipt_data = packet["mandatory_match_receipts"][0]
    assert receipt_data["session_id"] == "host-session-1"
    assert receipt_data["session_source"] == "absent"
    assert receipt_data["session_trusted"] is False
    receipt = SharedMemoryStore(tmp_path, group_id, read_only=True).get_rule_match_receipt(
        receipt_data["receipt_id"],
    )
    assert receipt is not None
    assert receipt.session_trusted is False


def _candidate(store: RuleMergeStore, suffix: str, *, cooldown_until: str = "") -> dict:
    left = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-left",
    )
    right = build_definition(
        f"must run tests before commit {suffix}",
        definition_id=f"{suffix}-right",
    )
    store.upsert_definition(left)
    store.upsert_definition(right)
    return store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        cooldown_until=cooldown_until,
        readiness_digest="fixture-readiness",
        definition_a=left,
        definition_b=right,
    )


def _issued_token(tmp_path, monkeypatch, proposal_id: str) -> str:
    response = _mcp_call(
        "memoryguard_rule_merge_capability_issue", {"proposal_id": proposal_id},
    )
    payload = _packet(response)
    token = payload["capability_token"]
    assert token and not token.startswith("admin:")
    return token


def test_mcp_governance_handlers_issue_approve_ack_and_clear_cooldown(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-admin")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    store = RuleMergeStore(tmp_path)

    approval = _candidate(store, "approval")
    token = _issued_token(tmp_path, monkeypatch, approval["proposal_id"])
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT token_hash FROM governance_capabilities WHERE proposal_id=?",
            (approval["proposal_id"],),
        ).fetchone()
    assert row is not None
    assert token not in row[0]

    approved = _packet(_mcp_call("memoryguard_rule_merge_approve", {
        "proposal_id": approval["proposal_id"],
        "capability_token": token,
    }))
    assert approved["ok"] is True
    assert store.get_proposal(approval["proposal_id"])["status"] == "approved"

    acknowledged = _candidate(store, "acknowledge")
    ack_token = _issued_token(tmp_path, monkeypatch, acknowledged["proposal_id"])
    ack_result = _packet(_mcp_call("memoryguard_rule_merge_acknowledge", {
        "proposal_id": acknowledged["proposal_id"],
        "capability_token": ack_token,
    }))
    assert ack_result["ok"] is True
    assert store.get_proposal(acknowledged["proposal_id"])["first_merge_acknowledged"] is True

    cooldown = _candidate(
        store, "cooldown", cooldown_until="2099-01-01T00:00:00+00:00",
    )
    cooldown_token = _issued_token(tmp_path, monkeypatch, cooldown["proposal_id"])
    clear_result = _packet(_mcp_call("memoryguard_rule_merge_cooldown_clear", {
        "proposal_id": cooldown["proposal_id"],
        "capability_token": cooldown_token,
    }))
    assert clear_result["ok"] is True
    assert store.get_proposal(cooldown["proposal_id"])["cooldown_until"] == ""
