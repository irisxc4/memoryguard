"""P3 barrier, canonical-source and migration counterexamples."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memoryguard.access_context import AccessContext
from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence
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


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _record(memory_id: str, *, status: SharedMemoryStatus = SharedMemoryStatus.ACTIVE) -> SharedMemoryRecord:
    now = _now_iso()
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=f"P3 source {memory_id}",
        kind=MemoryKind.PROCEDURE,
        status=status,
        injection_policy="always",
        agent_instance_id="agent-a",
        created_at=now,
        updated_at=now,
    )


def _seed_ready_pair(tmp_path):
    intelligence = RuleMergeStore(tmp_path)
    left = build_definition(
        "提交代码前必须运行测试",
        kind=MemoryKind.PROCEDURE,
        created_at=_aged(90),
    )
    right = build_definition(
        "提交前必须执行测试",
        kind=MemoryKind.PROCEDURE,
        created_at=_aged(90),
    )
    for definition in (left, right):
        intelligence.upsert_definition(definition)
        intelligence.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id="merge-group",
            target_type="agent",
            target_id="merge-agent",
            owner_agent_id="merge-agent",
            created_by="test",
        ))
        for index in range(3):
            intelligence.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=f"{definition.definition_id}-evidence-{index}",
                agent_instance_id=f"merge-agent-{index}",
                project_ref=f"merge-project-{index}",
                session_id=f"{definition.definition_id}-session-{index}",
                session_trusted=True,
                content=definition.canonical_text,
                observed_at=_aged(60),
            ))
        for index in range(20):
            intelligence.upsert_runtime_feedback(
                feedback_id=f"{definition.definition_id}-runtime-{index}",
                definition_id=definition.definition_id,
                receipt_id=f"{definition.definition_id}-receipt-{index}",
                outcome="followed",
                agent_instance_id=f"merge-agent-{index % 3}",
                project_ref=f"merge-project-{index % 3}",
                session_id=f"{definition.definition_id}-runtime-session-{index}",
                source="user",
                authority=4,
                session_trusted=1,
                created_at=_aged(45),
            )
        intelligence.recompute_runtime_stats(definition.definition_id)
    for index in range(3):
        intelligence.upsert_agent_reputation(
            agent_id=f"merge-agent-{index}",
            success_rate=0.98,
            rule_accuracy=0.98,
            violation_rate=0.02,
            sample_count=200,
            feedback_quality=0.95,
        )
        intelligence.upsert_project_profile(
            project_ref=f"merge-project-{index}",
            production_level=1.0,
            criticality=0.8,
            owner_verified=True,
        )
    return intelligence, left, right


def _approve_cold_start_gates(store: RuleMergeStore, proposal_id: str) -> None:
    context = AccessContext("barrier-admin", True, True, False)
    token = store.issue_merge_capability(proposal_id, context)
    store.acknowledge_first_merge(
        proposal_id,
        actor=context.principal,
        capability_token=token,
        access_context=context,
    )
    token = store.issue_merge_capability(proposal_id, context)
    store.clear_proposal_cooldown(
        proposal_id,
        capability_token=token,
        access_context=context,
    )


def _approve_candidate_for_merge(
    store: RuleMergeStore, proposal_id: str,
) -> tuple[AccessContext, dict[str, object]]:
    """Walk the public approval path and assert its governed end state."""
    _approve_cold_start_gates(store, proposal_id)
    context = AccessContext("barrier-admin", True, True, False)
    token = store.issue_merge_capability(proposal_id, context)
    approval = store.approve_proposal(
        proposal_id,
        approved_by=context.principal,
        capability_token=token,
        access_context=context,
    )
    approved = store.get_proposal(proposal_id)
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["cooldown_until"] == ""
    assert approved["first_merge_acknowledged"] is True
    valid_approval = store.get_valid_approval(proposal_id)
    assert valid_approval is not None
    assert valid_approval["capability_id"] == approval["capability_id"]
    return context, approval


def test_public_merge_barrier_drains_all_groups_preserves_cleared_cooldown_and_reports_final_water(
    tmp_path,
):
    first = SharedMemoryStore(tmp_path, "group-a")
    second = SharedMemoryStore(tmp_path, "group-b")
    first.append_record(
        _record("source-a"),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
        emit_lifecycle_outbox=True,
    )
    second.append_record(
        _record("source-b"),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
        emit_lifecycle_outbox=True,
    )

    intelligence, left, right = _seed_ready_pair(tmp_path)
    service = RuleMergeService(intelligence)
    proposals = service.scan_and_propose(
        definition_ids=[left.definition_id, right.definition_id],
    )
    proposal = next(item for item in proposals if item["status"] == "candidate")
    _approve_cold_start_gates(intelligence, proposal["proposal_id"])

    result = service.merge_proposal(proposal["proposal_id"])

    assert result["ok"] is True
    barrier = result["barrier"]
    assert set(barrier["after"]["committed_high_water"]) == {
        "group-a", "group-b",
    }
    assert all(
        group.list_unconsumed_rule_events() == []
        for group in (first, second)
    )


def test_public_merge_fails_closed_when_approved_inputs_change(tmp_path):
    intelligence, left, right = _seed_ready_pair(tmp_path)
    service = RuleMergeService(intelligence)
    proposals = service.scan_and_propose(
        definition_ids=[left.definition_id, right.definition_id],
    )
    proposal = next(item for item in proposals if item["status"] == "candidate")
    context = AccessContext("barrier-admin", True, True, False)
    token = intelligence.issue_merge_capability(proposal["proposal_id"], context)
    intelligence.approve_proposal(
        proposal["proposal_id"],
        approved_by=context.principal,
        capability_token=token,
        access_context=context,
    )
    intelligence.upsert_evidence(build_evidence(
        definition_id=left.definition_id,
        source_rule_id="late-approved-input",
        agent_instance_id="late-agent",
        project_ref="late-project",
        session_id="late-session",
        session_trusted=True,
        content=left.canonical_text,
    ))

    result = service.merge_proposal(
        proposal["proposal_id"], actor=context.principal,
    )

    assert result["ok"] is False
    assert "rule_merge_evidence_digest_drift" in result["barrier"]["error"]
    assert intelligence.count_merge_decisions_for_definitions(
        [left.definition_id, right.definition_id],
    ) == 0


def test_public_merge_barrier_blocks_unlinked_negative_until_real_backfill(tmp_path):
    intelligence, left, right = _seed_ready_pair(tmp_path)
    service = RuleMergeService(intelligence)
    proposals = service.scan_and_propose(
        definition_ids=[left.definition_id, right.definition_id],
    )
    proposal = next(item for item in proposals if item["status"] == "candidate")
    _approve_candidate_for_merge(intelligence, proposal["proposal_id"])

    group_id = "unlinked-negative-group"
    agent_id = "unlinked-negative-agent"
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    legacy = SharedMemoryStore(tmp_path, group_id)
    source = _record("unlinked-negative-source")
    source.body = "提交代码前必须运行测试"
    source.agent_instance_id = agent_id
    legacy.append_record(
        source,
        assignments=[{"target_type": "agent", "target_id": agent_id}],
        dedup_domain="unlinked-negative-independent-domain",
        emit_lifecycle_outbox=False,
    )
    for index in range(3):
        receipt_id = f"unlinked-negative-receipt-{index}"
        legacy.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=receipt_id,
            memory_id=source.memory_id,
            share_group_id=group_id,
            agent_instance_id=agent_id,
            task_hash=f"unlinked-negative-task-{index}",
            task="unlinked negative barrier",
            project_ref=f"unlinked-negative-project-{index}",
            session_id=f"unlinked-negative-session-{index}",
            session_source="host",
            session_trusted=True,
            created_at=_now_iso(),
        ))
        legacy.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"unlinked-negative-feedback-{index}",
            receipt_id=receipt_id,
            outcome="not_applicable",
            actor=agent_id,
            source="agent",
            authority=3,
        ))

    # This is a real bound source with feedback outbox ownership still
    # unresolved.  A compatibility hint must not satisfy the merge barrier.
    assert intelligence.get_source_link(group_id, source.memory_id) is None
    pending_events = legacy.list_unconsumed_rule_events()
    assert len(pending_events) == 3
    assert {
        event["event_type"] for event in pending_events
    } == {"effective_rule_feedback_changed"}
    assert {
        event["memory_id"] for event in pending_events
    } == {source.memory_id}

    blocked = service.merge_proposal(proposal["proposal_id"])

    assert blocked["ok"] is False
    assert "projection_barrier_outbox_not_drained" in blocked["barrier"]["error"]
    assert blocked["barrier"]["state"] == "failed"
    assert blocked["barrier"]["after"] is None
    assert legacy.list_unconsumed_rule_events() == pending_events
    assert intelligence.get_source_link(group_id, source.memory_id) is None
    assert intelligence.list_negative_evidence(
        definition_id=left.definition_id,
    ) == []
    assert intelligence.list_bindings(
        share_group_id=group_id, status="active",
    ) == []
    assert intelligence.count_merge_decisions_for_definitions(
        [left.definition_id, right.definition_id],
    ) == 0

    service.backfill_group(legacy, group_id)
    service.consume_outbox(tmp_path, only_group=group_id)
    link = intelligence.get_source_link(group_id, source.memory_id)
    assert link is not None
    assert link["status"] == "active"
    assert link["original_definition_id"]
    assert link["original_definition_id"] != link["canonical_definition_id"]
    assert link["canonical_definition_id"] == left.definition_id
    assert intelligence.list_bindings(
        definition_id=link["canonical_definition_id"],
        share_group_id=group_id,
        status="active",
    )
    negative = intelligence.list_negative_evidence(
        definition_id=link["canonical_definition_id"],
    )
    assert len(negative) == 3
    assert {item.source_rule_id for item in negative} == {source.memory_id}
    safety_blocked = service.merge_proposal(
        proposal["proposal_id"], actor="admin",
    )
    assert safety_blocked["ok"] is False
    assert safety_blocked["blocked_reason"] == "merge_safety_evaluation_failed"
    assert safety_blocked["conflict_type"] == "negative_evidence"
    assert "negative_evidence" in safety_blocked["assessment"]["reasons"]
    assert intelligence.count_merge_decisions_for_definitions(
        [left.definition_id, right.definition_id],
    ) == 0


def test_new_source_resolves_inactive_definition_to_active_canonical(tmp_path):
    intelligence, left, right = _seed_ready_pair(tmp_path)
    service = RuleMergeService(intelligence)

    # Create the inactive -> canonical lifecycle through the public merge path.
    proposals = service.scan_and_propose(
        definition_ids=[left.definition_id, right.definition_id],
    )
    proposal = next(item for item in proposals if item["status"] == "candidate")
    _approve_candidate_for_merge(intelligence, proposal["proposal_id"])
    merged = service.merge_proposal(proposal["proposal_id"], actor="admin")
    assert merged["ok"] is True
    canonical_id = merged["canonical_definition_id"]
    inactive_id = merged["merged_definition_ids"][0]
    canonical = intelligence.get_definition(canonical_id)
    inactive = intelligence.get_definition(inactive_id)
    assert canonical is not None
    assert inactive is not None
    assert canonical.status == "active"
    assert inactive.status == "merged"
    assert inactive.superseded_by == canonical_id
    assert intelligence.resolve_canonical(inactive_id) == canonical_id

    group_id = "canonical-new-source-group"
    agent_id = "canonical-new-source-agent"
    legacy = SharedMemoryStore(tmp_path, group_id)
    AgentBindingStore(tmp_path).bind_agent(agent_id, group_id)
    record = _record("canonical-new-source-independent")
    record.agent_instance_id = agent_id
    record.body = inactive.canonical_text.replace(
        "运行", "必须运行",
    ).replace(
        "执行", "必须执行",
    ) + "。"
    legacy.append_record(
        record,
        assignments=[{"target_type": "agent", "target_id": agent_id}],
        dedup_domain="canonical-new-source-independent-domain",
        emit_lifecycle_outbox=False,
    )
    assert intelligence.get_source_link(group_id, record.memory_id) is None
    source_definition = build_definition(
        record.body, kind=record.kind, created_at=record.created_at,
    )
    assert source_definition.definition_id == inactive_id
    assert source_definition.canonical_text == inactive.canonical_text

    sync = service.sync_rule(
        legacy,
        group_id,
        record,
    )
    assert sync == {
        "definition_id": canonical_id,
        "bindings": 1,
        "evidence": 0,
    }
    link = intelligence.get_source_link(group_id, record.memory_id)
    assert link is not None
    assert link["status"] == "active"
    assert link["original_definition_id"] == inactive_id
    assert link["canonical_definition_id"] == canonical_id
    assert link["source_revision"] == record.updated_at
    active_bindings = intelligence.list_bindings(
        definition_id=canonical_id,
        share_group_id=group_id,
        status="active",
    )
    assert len(active_bindings) == 1
    assert active_bindings[0].target_id == agent_id
    assert active_bindings[0].owner_agent_id == agent_id
    assert intelligence.list_bindings(
        definition_id=inactive_id, status="active",
    ) == []
    inactive_evidence_before = intelligence.list_evidence(
        definition_id=inactive_id,
    )
    inactive_runtime_before = intelligence.get_runtime_stats(inactive_id)

    receipt_id = "canonical-new-source-receipt"
    feedback_id = "canonical-new-source-feedback"
    legacy.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id,
        memory_id=record.memory_id,
        share_group_id=group_id,
        agent_instance_id=agent_id,
        task_hash="canonical-new-source-task",
        task="canonical source feedback",
        project_ref="canonical-new-source-project",
        session_id="canonical-new-source-session",
        session_source="host",
        session_trusted=True,
        created_at=_now_iso(),
    ))
    legacy.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id=feedback_id,
        receipt_id=receipt_id,
        outcome="followed",
        actor=agent_id,
        source="user",
        authority=4,
    ))
    assert len(legacy.list_unconsumed_rule_events()) == 1
    service.consume_outbox(tmp_path, only_group=group_id)

    projected = intelligence.get_effective_feedback_projection(receipt_id)
    assert projected is not None
    assert projected["definition_id"] == canonical_id
    assert projected["effective_feedback_id"] == feedback_id
    assert projected["outcome"] == "followed"
    canonical_evidence = [
        item for item in intelligence.list_evidence(definition_id=canonical_id)
        if item.source_rule_id == record.memory_id
    ]
    assert len(canonical_evidence) == 1
    assert canonical_evidence[0].receipt_id == receipt_id
    assert canonical_evidence[0].feedback_id == feedback_id
    assert intelligence.list_evidence(
        definition_id=inactive_id,
    ) == inactive_evidence_before
    assert intelligence.list_bindings(
        definition_id=inactive_id, status="active",
    ) == []
    contributions = intelligence.list_binding_contributions(
        share_group_id=group_id,
        source_memory_id=record.memory_id,
        active=True,
    )
    assert len(contributions) == 1
    assert contributions[0]["definition_id"] == canonical_id
    runtime = intelligence.get_runtime_stats(canonical_id)
    assert runtime is not None
    assert runtime["followed"] >= 1
    assert intelligence.get_runtime_stats(inactive_id) == inactive_runtime_before


def test_inactive_lifecycle_outbox_does_not_create_active_definition(tmp_path):
    legacy = SharedMemoryStore(tmp_path, "inactive-group")
    record = _record("quarantined-source", status=SharedMemoryStatus.QUARANTINED)
    legacy.append_record(record, emit_lifecycle_outbox=True)
    intelligence = RuleMergeStore(tmp_path)
    service = RuleMergeService(intelligence)
    service.consume_outbox(tmp_path)

    definition_id = service._definition_from_record(record).definition_id
    assert intelligence.get_definition(definition_id) is None
    assert legacy.list_unconsumed_rule_events() == []


def test_production_feedback_contribution_identity_and_independence(tmp_path):
    legacy = SharedMemoryStore(tmp_path, "feedback-group")
    record = _record("feedback-source")
    legacy.append_record(record)
    intelligence = RuleMergeStore(tmp_path)
    service = RuleMergeService(intelligence)
    # Feedback projection is source-owned.  Establish source ownership before
    # producing feedback events for the source.
    service.backfill_group(legacy, legacy.group_id)
    for index in (1, 2):
        receipt_id = f"receipt-{index}"
        legacy.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=receipt_id,
            memory_id=record.memory_id,
            share_group_id=legacy.group_id,
            agent_instance_id="agent-a",
            task_hash="same-task",
            task="same task",
            project_ref="project-a",
            session_id="trusted-session",
            session_source="host",
            session_trusted=True,
            created_at=_now_iso(),
        ))
        legacy.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"feedback-{index}",
            receipt_id=receipt_id,
            outcome="followed",
            actor="agent-a",
            source="hook",
            authority=2,
        ))

    service.consume_outbox(tmp_path)

    definition_id = service._definition_from_record(record).definition_id
    with intelligence._db() as conn:
        rows = conn.execute(
            "SELECT contribution_id, independence_key, receipt_id, feedback_id "
            "FROM rule_evidence_contributions WHERE definition_id=? "
            "ORDER BY contribution_id",
            (definition_id,),
        ).fetchall()
    assert len(rows) == 2
    assert len({row["contribution_id"] for row in rows}) == 2
    assert len({row["independence_key"] for row in rows}) == 1
    assert {row["receipt_id"] for row in rows} == {"receipt-1", "receipt-2"}
    assert {row["feedback_id"] for row in rows} == {"feedback-1", "feedback-2"}
    assert len(intelligence.list_evidence(definition_id=definition_id)) == 1


def test_backfill_reports_real_partial_metrics(tmp_path):
    legacy = SharedMemoryStore(tmp_path, "partial-group")
    record = _record("unmigrated-source")
    legacy.append_record(record)
    intelligence = RuleMergeStore(tmp_path)
    definition = build_definition("partial migration definition")
    intelligence.upsert_definition(definition)
    intelligence.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=legacy.group_id,
        target_type="agent",
        target_id="agent-a",
        owner_agent_id="agent-a",
    ))
    with intelligence._write_conn() as conn:
        conn.execute("DELETE FROM rule_binding_contributions")
    evidence = build_evidence(
        definition_id=definition.definition_id,
        source_rule_id="unknown-source",
        agent_instance_id="agent-a",
        project_ref="project-a",
        session_id="trusted-session",
        session_trusted=True,
        content=definition.canonical_text,
    )
    intelligence.upsert_evidence(evidence)
    with intelligence._write_conn() as conn:
        conn.execute(
            "UPDATE rule_evidence SET source_root_id=?, active=0 "
            "WHERE evidence_id=?",
            ("ambiguous_migration_evidence", evidence.evidence_id),
        )
    orphan = SharedMemoryStore(tmp_path, "orphan-group")
    orphan_definition = build_definition("orphan migration target")
    intelligence.upsert_definition(orphan_definition)
    intelligence.set_definition_status(
        orphan_definition.definition_id, "merged",
    )
    intelligence.upsert_source_link(
        share_group_id=orphan.group_id,
        memory_id="orphan-source",
        original_definition_id=orphan_definition.definition_id,
        canonical_definition_id=orphan_definition.definition_id,
    )
    service = RuleMergeService(intelligence)
    result = service.backfill_legacy(tmp_path)

    assert result["migration_loss"] > 0
    assert result["binding_contribution_diff"] > 0
    assert result["ambiguous_count"] > 0
    assert result["status"] == "partial"
