"""MCP feedback ownership and evidence-ledger fallback regressions."""

from __future__ import annotations

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.mcp_server import execute_tool
from memoryguard.rule_evidence_ledger import build_contribution, list_effective
from memoryguard.rule_merge import RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


GROUP_ID = "evidence-team"
OWNER_A = "owner-a"
OWNER_B = "owner-b"


def _setup_feedback(tmp_path, monkeypatch) -> tuple[SharedMemoryStore, str]:
    AgentBindingStore(tmp_path).bind_agent(OWNER_A, GROUP_ID)
    AgentBindingStore(tmp_path).bind_agent(OWNER_B, GROUP_ID)
    project_ref = str(tmp_path / "project")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", project_ref)
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", OWNER_B)
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", "session-b")

    legacy = SharedMemoryStore(tmp_path, GROUP_ID)
    legacy.append_record(
        SharedMemoryRecord(
            memory_id="rule-b",
            body="run tests before commit",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            agent_instance_id=OWNER_B,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        ),
        assignments=[{"target_type": "agent", "target_id": OWNER_B}],
    )
    legacy.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="receipt-b",
        memory_id="rule-b",
        share_group_id=GROUP_ID,
        agent_instance_id=OWNER_B,
        task_hash="task-b",
        task="feedback task",
        project_ref=project_ref,
        session_id="session-b",
        provider="codex",
        session_trusted=True,
        session_source="host",
        created_at=_now_iso(),
    ))
    return legacy, project_ref


def _submit_feedback(outcome: str) -> dict:
    return execute_tool("memoryguard_rule_feedback", {
        "receipt_id": "receipt-b",
        "outcome": outcome,
        "actor": OWNER_B,
        "evidence": "scope mismatch" if outcome == "not_applicable" else "",
    })


def _contribution_row(store: RuleMergeStore, receipt_id: str):
    with store._db() as conn:
        return conn.execute(
            "SELECT * FROM rule_evidence_contributions WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()


def test_owner_a_cannot_revoke_owner_b_contribution(tmp_path, monkeypatch):
    legacy, _project_ref = _setup_feedback(tmp_path, monkeypatch)
    created = _submit_feedback("followed")
    assert created.get("isError") is not True, created

    intel = RuleMergeStore(tmp_path)
    assert _contribution_row(intel, "receipt-b") is not None
    assert len(intel.list_evidence()) == 1
    before_feedback = legacy.list_rule_match_feedbacks(receipt_id="receipt-b")

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", OWNER_A)
    revoked = execute_tool("memoryguard_rule_feedback", {
        "receipt_id": "receipt-b",
        "outcome": "ignored",
        "actor": OWNER_B,
    })
    assert revoked["isError"] is True
    assert "feedback agent does not match receipt owner" in (
        revoked["content"][0]["text"]
    )
    assert legacy.list_rule_match_feedbacks(receipt_id="receipt-b") == before_feedback
    assert len(intel.list_evidence()) == 1


@pytest.mark.parametrize(
    ("winner_outcome", "winner_polarity", "runner_up_polarity"),
    [
        ("followed", "positive", "negative"),
        ("not_applicable", "negative", "positive"),
    ],
)
def test_feedback_outbox_winner_removal_restores_runner_up(
    tmp_path,
    monkeypatch,
    winner_outcome: str,
    winner_polarity: str,
    runner_up_polarity: str,
):
    legacy, project_ref = _setup_feedback(tmp_path, monkeypatch)
    created = _submit_feedback(winner_outcome)
    assert created.get("isError") is not True, created
    assert legacy.list_unconsumed_rule_events() == []

    intel = RuleMergeStore(tmp_path)
    winner = _contribution_row(intel, "receipt-b")
    assert winner is not None
    assert winner["polarity"] == winner_polarity
    runner_up = build_contribution(
        contribution_id="runner-up-contribution",
        definition_id=winner["definition_id"],
        independence_key=winner["independence_key"],
        kind=winner["kind"],
        polarity=runner_up_polarity,
        authority=int(winner["authority"]) - 1,
        confidence=float(winner["confidence"]),
        observed_at="2026-08-03T00:00:01+00:00",
        receipt_id="runner-up-receipt",
        feedback_id="runner-up-feedback",
        source_rule_id="rule-b",
        source_memory_id="rule-b",
        source_ids={"receipt_id": "runner-up-receipt"},
        agent_instance_id=OWNER_B,
        project_ref=project_ref,
        share_group_id=GROUP_ID,
        session_id="session-b",
        session_trusted=True,
    )
    intel.upsert_evidence_contribution(runner_up)

    with intel._db() as conn:
        assert list_effective(conn, definition_id=winner["definition_id"])[
            0
        ].winner_contribution_id == winner["contribution_id"]

    intel.deactivate_evidence_contributions_for_receipt("receipt-b")

    with intel._db() as conn:
        effective = list_effective(conn, definition_id=winner["definition_id"])
    assert len(effective) == 1
    assert effective[0].winner_contribution_id == runner_up.contribution_id
    assert effective[0].polarity == runner_up_polarity

    projection = intel.get_effective_feedback_projection("receipt-b")
    assert projection is not None
    assert projection["outcome"] == winner_outcome
