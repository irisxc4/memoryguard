"""Decisive P3 governance counterexamples.

These tests exercise public APIs and state the safety property at stake.  A
red test is intentional evidence that the current public contract is missing
the required guard; this file does not provide compatibility shims.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from memoryguard.merge_governance_coordinator import (
    MergeGovernanceCoordinator,
    ProjectionBarrierState,
)
from memoryguard.migrations.rule_intelligence_v2 import migrate
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_evidence_ledger import (
    build_contribution,
    deactivate_contribution,
    list_effective,
    rebuild_effective,
    upsert_contribution,
)
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_read_path import RuleReadPath
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _proposal(store: RuleMergeStore, *, cooldown_until: str = "") -> dict:
    left = build_definition("must run tests before commit")
    right = build_definition("must run tests before committing")
    store.upsert_definition(left)
    store.upsert_definition(right)
    return store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        cooldown_until=cooldown_until,
        definition_a=left,
        definition_b=right,
    )


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_distinct_evidence(
    store: RuleMergeStore,
    definition_id: str,
    content: str,
) -> None:
    for index in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=f"evidence-source-{index}",
            agent_instance_id=f"trusted-agent-{index}",
            project_ref=f"trusted-project-{index}",
            session_id="one-trusted-session",
            session_trusted=True,
            content=content,
            observed_at=_aged(10),
        ))


def _seed_single_session_runtime(
    store: RuleMergeStore,
    definition_id: str,
) -> None:
    for index in range(10):
        feedback_id = f"runtime-feedback-{index}"
        receipt_id = f"runtime-receipt-{index}"
        store.upsert_runtime_feedback(
            feedback_id=feedback_id,
            receipt_id=receipt_id,
            definition_id=definition_id,
            outcome="followed",
            agent_instance_id=f"trusted-agent-{index % 3}",
            project_ref=f"trusted-project-{index % 3}",
            session_id="one-trusted-session",
            source="hook",
            authority=2,
            session_trusted=True,
            created_at=_aged(10),
        )
        store.upsert_effective_feedback_projection(
            receipt_id=receipt_id,
            effective_feedback_id=feedback_id,
            definition_id=definition_id,
            outcome="followed",
            session_trusted=True,
            session_source="host",
        )
    store.recompute_runtime_stats(definition_id)


def _assert_active_bindings_have_active_contributions(
    store: RuleMergeStore,
) -> None:
    active_binding_ids = {
        binding.binding_id
        for binding in store.list_bindings(status="active")
    }
    contributed_binding_ids = {
        row["binding_id"]
        for row in store.list_binding_contributions(active=True)
    }
    assert active_binding_ids <= contributed_binding_ids


def _legacy_record(memory_id: str, group_id: str) -> SharedMemoryRecord:
    now = _timestamp()
    return SharedMemoryRecord(
        memory_id=memory_id,
        body="legacy feedback rule",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        agent_instance_id="legacy-agent",
        created_at=now,
        updated_at=now,
    )


def _legacy_receipt(group_id: str) -> RuleMatchReceipt:
    return RuleMatchReceipt(
        receipt_id="legacy-receipt",
        memory_id="legacy-rule",
        share_group_id=group_id,
        agent_instance_id="legacy-agent",
        task_hash="legacy-task",
        task="legacy feedback ordering",
        created_at=_timestamp(),
    )


def _drain_legacy_events(store: SharedMemoryStore) -> None:
    for event in store.list_unconsumed_rule_events():
        store.mark_rule_event_consumed(event["event_id"])


def test_forged_admin_prefix_cannot_approve(tmp_path):
    store = RuleMergeStore(tmp_path)
    proposal = _proposal(store)

    with pytest.raises((TypeError, ValueError)):
        store.approve_proposal(
            proposal["proposal_id"],
            approved_by="human",
            capability_id="admin:forged",
        )

    assert store.get_proposal(proposal["proposal_id"])["status"] == "candidate"
    assert store.get_valid_approval(proposal["proposal_id"]) is None


def test_first_merge_acknowledgment_requires_capability(tmp_path):
    store = RuleMergeStore(tmp_path)
    proposal = _proposal(store)

    with pytest.raises((TypeError, ValueError)):
        store.acknowledge_first_merge(proposal["proposal_id"], actor="admin")

    assert store.get_proposal(proposal["proposal_id"])[
        "first_merge_acknowledged"
    ] is False


def test_cooldown_clear_requires_capability(tmp_path):
    store = RuleMergeStore(tmp_path)
    proposal = _proposal(store, cooldown_until="2099-01-01T00:00:00+00:00")

    with pytest.raises((TypeError, ValueError)):
        store.clear_proposal_cooldown(proposal["proposal_id"])

    assert store.get_proposal(proposal["proposal_id"])["cooldown_until"] == (
        "2099-01-01T00:00:00+00:00"
    )


def test_definition_core_change_creates_new_definition(tmp_path):
    store = RuleMergeStore(tmp_path)
    must = build_definition("run tests before commit", rule_strength="must")
    should = build_definition("run tests before commit", rule_strength="should")

    assert must.definition_id != should.definition_id
    store.upsert_definition(must)
    store.upsert_definition(should)
    assert len(store.list_definitions(status="active")) == 2


def test_same_definition_id_immutable_core_change_is_rejected(tmp_path):
    store = RuleMergeStore(tmp_path)
    original = build_definition(
        "run tests before commit", definition_id="fixed-definition-id",
    )
    store.upsert_definition(original)

    forged = replace(
        original,
        canonical_text="run tests after commit",
        revision=99,
    )
    with pytest.raises(ValueError, match="definition_identity_mismatch"):
        store.upsert_definition(forged)

    persisted = store.get_definition(original.definition_id)
    assert persisted is not None
    assert persisted.to_dict() == original.to_dict()


def test_merge_transaction_recomputes_similarity_from_current_definitions(
    tmp_path,
):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit")
    right = build_definition("must rotate credentials daily")
    store.upsert_definition(left)
    store.upsert_definition(right)
    # A caller can persist a stale or dishonest proposal snapshot.  The
    # transaction must evaluate the current pair, not trust this score.
    proposal = store.create_proposal(
        [left.definition_id, right.definition_id],
        1.0,
        definition_a=left,
        definition_b=right,
    )
    definition_ids = proposal["definition_ids"]
    left_id, right_id = definition_ids

    with pytest.raises(RuntimeError):
        store.execute_merge(
            proposal_id=proposal["proposal_id"],
            canonical_definition_id=left_id,
            merged_definition_ids=[right_id],
            expected_definition_revisions={
                left_id: proposal["definition_revision_a"],
                right_id: proposal["definition_revision_b"],
            },
            expected_evidence_digest=proposal["evidence_digest"],
            expected_negative_digest=proposal["negative_digest"],
            expected_binding_digest=proposal["binding_digest"],
            expected_runtime_digest=proposal["runtime_digest"],
            expected_assessment_revision=proposal["assessment_revision"],
            expected_policy_version=proposal["policy_version"],
        )

    assert store.get_definition(left_id).status == "active"
    assert store.get_definition(right_id).status == "active"


def test_unrelated_projection_update_does_not_block_undo(tmp_path):
    store = RuleMergeStore(tmp_path)
    left = build_definition("must run tests before commit", definition_id="undo-left")
    right = build_definition(
        "must run tests before code commit", definition_id="undo-right",
    )
    unrelated = build_definition("use pnpm for dependencies")
    store.upsert_definition(left)
    store.upsert_definition(right)
    store.upsert_definition(unrelated)
    with store._write_conn() as conn:
        conn.execute(
            "UPDATE rule_definitions SET status='merged', superseded_by=? "
            "WHERE definition_id=?",
            (left.definition_id, right.definition_id),
        )
    with store._db() as conn:
        post_state = store._state_snapshot_conn(
            conn, [left.definition_id, right.definition_id],
        )
    with store._write_conn() as conn:
        conn.execute(
            "INSERT INTO rule_merge_proposals "
            "(proposal_id, definition_ids, created_at) VALUES (?, ?, ?)",
            (
                "undo-proposal",
                json.dumps([left.definition_id, right.definition_id]),
                _timestamp(),
            ),
        )
    decision = store.record_merge_decision(
        proposal_id="undo-proposal",
        canonical_definition_id=left.definition_id,
        merged_definition_ids=[right.definition_id],
        before_bindings=[],
        after_bindings=[],
        migration={
            "original_bindings": {},
            "original_evidence": {},
            "original_negative_evidence": {},
            "original_runtime_feedback": {},
            "original_contributions": {},
            "original_evidence_contributions": {},
            "original_effective_projection": {},
            "original_revisions": {
                left.definition_id: left.revision,
                right.definition_id: right.revision,
            },
            "post_state": post_state,
        },
    )

    store.upsert_effective_feedback_projection(
        receipt_id="unrelated-receipt",
        effective_feedback_id="unrelated-feedback",
        definition_id=unrelated.definition_id,
        outcome="followed",
    )
    assert store.get_effective_feedback_projection("unrelated-receipt")[
        "definition_id"
    ] == unrelated.definition_id

    undo = store.undo_merge(decision["decision_id"])
    assert undo["status"] == "undone"
    assert store.get_effective_feedback_projection("unrelated-receipt")[
        "effective_feedback_id"
    ] == "unrelated-feedback"


def test_validated_requires_distinct_trusted_sessions_agents_and_projects(
    tmp_path,
):
    store = RuleMergeStore(tmp_path)
    definition = build_definition(
        "must run tests before commit",
        created_at=_aged(10),
    )
    store.upsert_definition(definition)
    _seed_distinct_evidence(store, definition.definition_id, definition.canonical_text)
    _seed_single_session_runtime(store, definition.definition_id)

    RuleMergeService(store).scan_and_propose(
        definition_ids=[definition.definition_id],
    )

    refreshed = store.get_definition(definition.definition_id)
    assert refreshed is not None
    assert refreshed.maturity_state not in {"validated", "trusted"}


def test_concurrent_legacy_feedback_is_ordered_before_merge_commit(tmp_path):
    legacy = SharedMemoryStore(tmp_path, "legacy-ordering")
    legacy.append_record(_legacy_record("legacy-rule", legacy.group_id))
    legacy.append_rule_match_receipt(_legacy_receipt(legacy.group_id))

    feedback_committed = threading.Event()
    release_feedback = threading.Event()
    order: list[str] = []

    def write_feedback() -> None:
        with legacy.governance_lock(timeout=1.0, poll_interval=0.01):
            legacy.append_rule_match_feedback(RuleMatchFeedback(
                feedback_id="legacy-feedback",
                receipt_id="legacy-receipt",
                outcome="followed",
                actor="legacy-agent",
                source="agent",
                authority=2,
            ))
            order.append("feedback-committed")
            feedback_committed.set()
            release_feedback.wait(2.0)

    writer = threading.Thread(target=write_feedback)
    writer.start()
    assert feedback_committed.wait(1.0)

    coordinator = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[legacy],
        drain_callback=lambda: _drain_legacy_events(legacy),
        projection_status=lambda: {
            "projection_lag": 0,
            "projection_error": "",
        },
        timeout=1.0,
        poll_interval=0.01,
    )
    merge_committed = threading.Event()
    result_box: list = []

    def merge() -> dict[str, bool]:
        order.append("merge-committed")
        merge_committed.set()
        return {"ok": True}

    merger = threading.Thread(target=lambda: result_box.append(
        coordinator.run_merge(merge)
    ))
    merger.start()
    assert not merge_committed.wait(0.15)
    release_feedback.set()
    writer.join(2.0)
    merger.join(2.0)

    assert not writer.is_alive()
    assert not merger.is_alive()
    assert order == ["feedback-committed", "merge-committed"]
    assert result_box[0].state is ProjectionBarrierState.COMMITTED


def test_global_projection_barrier_serializes_merge_commits(tmp_path):
    status = {"projection_lag": 0, "projection_error": ""}
    first_entered = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []
    results: list = []

    first = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[],
        projection_status=status,
        timeout=1.0,
        poll_interval=0.01,
    )
    second = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[],
        projection_status=status,
        timeout=1.0,
        poll_interval=0.01,
    )

    def first_merge() -> dict[str, bool]:
        order.append("first-entered")
        first_entered.set()
        release_first.wait(2.0)
        order.append("first-exited")
        return {"ok": True}

    def second_merge() -> dict[str, bool]:
        order.append("second-entered")
        second_entered.set()
        order.append("second-exited")
        return {"ok": True}

    first_thread = threading.Thread(target=lambda: results.append(
        first.run_merge(first_merge)
    ))

    def run_second() -> None:
        second_attempted.set()
        results.append(second.run_merge(second_merge))

    second_thread = threading.Thread(target=run_second)
    first_thread.start()
    assert first_entered.wait(1.0)
    second_thread.start()
    assert second_attempted.wait(1.0)
    assert not second_entered.wait(0.15)
    release_first.set()
    first_thread.join(2.0)
    second_thread.join(2.0)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert order == [
        "first-entered",
        "first-exited",
        "second-entered",
        "second-exited",
    ]
    assert [result.state for result in results] == [
        ProjectionBarrierState.COMMITTED,
        ProjectionBarrierState.COMMITTED,
    ]


def test_canonical_read_falls_back_when_projection_lags(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("must run tests before commit")
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="read-group",
        target_type="agent",
        target_id="read-agent",
        owner_agent_id="read-agent",
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id,
        source_rule_id="legacy-memory",
        agent_instance_id="read-agent",
        project_ref="read-project",
        session_id="read-session",
        session_trusted=True,
        content=definition.canonical_text,
    ))
    store.set_projection_state(
        "rule-intelligence",
        last_outbox_event_id="event-2",
        last_projected_event_id="event-1",
        projection_lag=1,
    )

    read_path = RuleReadPath(tmp_path, "read-group")
    assert read_path.resolve_canonical_map(
        known_memory_ids={"legacy-memory"},
        shadow_summary={"missing": [], "extra": [], "permission_diff": 0},
    ) is None
    assert read_path.last_readiness["ready"] is False


def test_active_binding_always_has_active_contribution(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("must run tests before commit")
    store.upsert_definition(definition)
    binding = build_binding(
        definition.definition_id,
        share_group_id="binding-group",
        target_type="agent",
        target_id="binding-agent",
        owner_agent_id="binding-agent",
    )

    store.replace_source_contributions("binding-group", "source-a", [binding])
    store.replace_source_contributions("binding-group", "source-b", [binding])
    _assert_active_bindings_have_active_contributions(store)

    store.deactivate_source_contributions("binding-group", "source-a")
    _assert_active_bindings_have_active_contributions(store)
    assert store.list_bindings(
        definition_id=definition.definition_id,
        status="active",
    )

    store.deactivate_source_contributions("binding-group", "source-b")
    _assert_active_bindings_have_active_contributions(store)
    assert store.list_bindings(
        definition_id=definition.definition_id,
        status="active",
    ) == []


@pytest.mark.parametrize(
    ("winner_polarity", "runner_up_polarity"),
    [("positive", "negative"), ("negative", "positive")],
)
def test_runner_up_restores_for_positive_and_negative_polarity(
    tmp_path,
    winner_polarity: str,
    runner_up_polarity: str,
):
    db_path = tmp_path / "evidence-ledger.sqlite3"
    migrate(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        upsert_contribution(conn, build_contribution(
            contribution_id="winner",
            definition_id="definition-1",
            independence_key="fact-1",
            kind="receipt",
            polarity=winner_polarity,
            authority=20,
            confidence=1.0,
            observed_at="2026-08-03T00:00:00Z",
            receipt_id="winner-receipt",
            session_id="winner-session",
            session_trusted=True,
        ))
        upsert_contribution(conn, build_contribution(
            contribution_id="runner-up",
            definition_id="definition-1",
            independence_key="fact-1",
            kind="receipt",
            polarity=runner_up_polarity,
            authority=10,
            confidence=1.0,
            observed_at="2026-08-03T00:00:00Z",
            receipt_id="runner-up-receipt",
            session_id="runner-up-session",
            session_trusted=True,
        ))
        rebuild_effective(conn)
        assert deactivate_contribution(conn, "winner")
        rebuild_effective(conn)

        effective = list_effective(conn)
        assert len(effective) == 1
        assert effective[0].winner_contribution_id == "runner-up"
        assert effective[0].polarity == runner_up_polarity
    finally:
        conn.close()
