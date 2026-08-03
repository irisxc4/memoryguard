from __future__ import annotations

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


@pytest.mark.parametrize("source", ["generated", "manual", "client"])
def test_forged_session_trust_cannot_issue_merge_capability(tmp_path, source):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit", definition_id="left")
    right = build_definition("must run tests before commit", definition_id="right")
    store.upsert_definition(left)
    store.upsert_definition(right)
    proposal = store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        definition_a=left,
        definition_b=right,
    )

    context = AccessContext(
        "admin-agent", True, True, False,
        session_id="forged-session", session_source=source,
        session_trusted=True,
    )
    assert context.session_trusted is False
    with pytest.raises(ValueError, match="trusted session context"):
        store.issue_merge_capability(proposal["proposal_id"], context)


@pytest.mark.parametrize("source", ["host", "transport", "generated", "manual", "client"])
def test_feedback_trust_requires_host_or_transport(source):
    trusted = RuleMergeService._session_trusted_value(
        {
            "session_id": "session-1",
            "session_source": source,
            "session_trusted": True,
        },
        {},
    )
    assert trusted == int(source in {"host", "transport"})


@pytest.mark.parametrize("source", ["generated", "manual", "client"])
def test_feedback_projection_rejects_forged_session_trust(tmp_path, source):
    store = RuleMergeStore(tmp_path)
    store.upsert_effective_feedback_projection(
        receipt_id=f"forged-{source}",
        session_id="forged-session",
        session_trusted=1,
        session_source=source,
    )
    projection = store.get_effective_feedback_projection(f"forged-{source}")
    assert projection is not None
    assert projection["session_trusted"] == 0


def test_projection_barrier_only_checks_definition_groups(tmp_path):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit", definition_id="left")
    right = build_definition(
        "must run tests before committing", definition_id="right",
    )
    store.upsert_definition(left)
    store.upsert_definition(right)
    for definition in (left, right):
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="related-group",
            target_type="agent",
            target_id="agent-1",
            owner_agent_id="agent-1",
        ))

    store.set_projection_state("unrelated-group", projection_lag=3)
    with store._db() as conn:
        assert store._conn_projection_ready(conn) is False
        assert store._conn_projection_ready(
            conn, group_ids={"related-group"},
        ) is True

    status = store.projection_status(group_ids={"related-group"})
    assert status["projection_lag"] == 0
    assert status["projection_error"] == ""


def test_runtime_high_water_digest_ignores_unrelated_group(tmp_path):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit", definition_id="left")
    right = build_definition(
        "must run tests before committing", definition_id="right",
    )
    store.upsert_definition(left)
    store.upsert_definition(right)
    for definition in (left, right):
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="related-group",
            target_type="agent",
            target_id="agent-1",
            owner_agent_id="agent-1",
        ))

    proposal = store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        definition_a=left,
        definition_b=right,
    )
    before = proposal["runtime_digest"]
    store.set_projection_state("unrelated-group", projection_lag=1)
    refreshed = store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        definition_a=left,
        definition_b=right,
    )
    assert refreshed["runtime_digest"] == before


def test_outbox_drain_only_consumes_selected_groups(tmp_path):
    stores = {
        group_id: SharedMemoryStore(tmp_path, group_id)
        for group_id in ("related-group", "unrelated-group")
    }
    for group_id, legacy in stores.items():
        legacy.append_record(SharedMemoryRecord(
            memory_id=f"memory-{group_id}",
            body="must run tests before commit",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            agent_instance_id="agent-1",
            created_at=_now_iso(),
            updated_at=_now_iso(),
        ), assignments=[{"target_type": "agent", "target_id": "agent-1"}])
        legacy.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=f"receipt-{group_id}",
            memory_id=f"memory-{group_id}",
            share_group_id=group_id,
            agent_instance_id="agent-1",
            task_hash="task-hash",
            task="run tests",
        ))
        legacy.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"feedback-{group_id}",
            receipt_id=f"receipt-{group_id}",
            outcome="followed",
            actor="agent-1",
        ))

    RuleMergeService(RuleMergeStore(tmp_path)).consume_outbox(
        tmp_path, only_groups={"related-group"},
    )
    assert stores["related-group"].list_unconsumed_rule_events() == []
    assert len(stores["unrelated-group"].list_unconsumed_rule_events()) == 1


def test_merge_barrier_ignores_unrelated_group_lag(tmp_path):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit", definition_id="left")
    right = build_definition("must run tests before commit", definition_id="right")
    store.upsert_definition(left)
    store.upsert_definition(right)
    for definition in (left, right):
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="related-group",
            target_type="agent",
            target_id="agent-1",
            owner_agent_id="agent-1",
        ))
    SharedMemoryStore(tmp_path, "related-group")
    store.set_projection_state("unrelated-group", projection_lag=1)
    proposal = store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        definition_a=left,
        definition_b=right,
    )
    context = AccessContext("admin-agent", True, True, False)
    token = store.issue_merge_capability(proposal["proposal_id"], context)
    store.approve_proposal(
        proposal["proposal_id"],
        approved_by=context.principal,
        capability_token=token,
        access_context=context,
    )

    result = RuleMergeService(store).merge_proposal(
        proposal["proposal_id"], actor="human",
    )
    assert result["ok"] is True
