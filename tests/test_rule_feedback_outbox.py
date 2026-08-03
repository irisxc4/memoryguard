"""P2 -> P3 transactional outbox tests (PR4).

Feedback was previously mirrored to the rule-intelligence layer only once, at
rule creation, so real production feedback (followed / violated /
not_applicable / exception / corrected) never reached the evidence, reputation
or maturity models.  Now every feedback event is written with its outbox row in
one legacy-store transaction, and ``consume_outbox`` projects it idempotently:

  * followed       -> positive runtime evidence
  * violated       -> adherence signal only (never negative scope)
  * not_applicable -> negative scope evidence
  * exception      -> negative/exception evidence
  * merged sources -> new evidence lands on the current canonical Definition
"""
from __future__ import annotations

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import normalize_rule_text
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.governance_engine import GovernanceEngine
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _seed_record(
    store: SharedMemoryStore,
    memory_id: str,
    body: str,
    agent="agent-1",
    *,
    dedup_domain: str = "",
):
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=[{"target_type": "agent", "target_id": agent}],
        dedup_domain=dedup_domain,
    )


def _seed_receipt(
    store: SharedMemoryStore,
    receipt_id: str,
    memory_id: str,
    *,
    session_id: str = "sess-1",
    session_trusted: bool = False,
    session_source: str = "absent",
) -> None:
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id, memory_id=memory_id, share_group_id=store.group_id,
        agent_instance_id="agent-1", task_hash="th", task="写测试",
        project_ref="/proj/x", session_id=session_id, provider="codex",
        session_trusted=session_trusted, session_source=session_source,
    ))


def _feedback(store: SharedMemoryStore, receipt_id: str, outcome: str,
              *, feedback_id: str = "", confidence: float = 1.0,
              source: str = "agent", authority: int = 3,
              created_at: str = "") -> None:
    store.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id=feedback_id or f"fb-{outcome}",
        receipt_id=receipt_id, outcome=outcome, actor="agent:agent-1",
        source=source, authority=authority, confidence=confidence,
        created_at=created_at or _now_iso(),
    ))


def _consume(tmp_path):
    intel = RuleMergeStore(tmp_path)
    summary = RuleMergeService(intel).consume_outbox(tmp_path)
    return intel, summary


def _setup_legacy(
    tmp_path,
    *,
    group="g1",
    body="提交代码前必须运行测试",
    session_id="sess-1",
    session_trusted=False,
    session_source="absent",
):
    store = SharedMemoryStore(tmp_path, group)
    _seed_record(store, "m1", body)
    _seed_receipt(
        store, "rcpt-1", "m1", session_id=session_id,
        session_trusted=session_trusted, session_source=session_source,
    )
    return store


def _setup_same_definition_sources(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    body = "run tests before submit"
    _seed_record(
        legacy, "m1", body, agent="owner", dedup_domain="source-m1",
    )
    _seed_record(
        legacy, "m2", body, agent="owner", dedup_domain="source-m2",
    )
    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    definition_id = service._definition_from_record(
        legacy.get_record("m1"),
    ).definition_id
    return legacy, intel, service, group, definition_id


def test_feedback_writes_outbox_event_atomically(tmp_path):
    store = _setup_legacy(tmp_path)
    assert store.list_unconsumed_rule_events() == []
    _feedback(store, "rcpt-1", "followed")
    events = store.list_unconsumed_rule_events()
    assert len(events) == 1
    assert events[0]["outcome"] == "followed"
    assert events[0]["memory_id"] == "m1"


def test_consume_outbox_projects_followed_to_evidence(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(store, "rcpt-1", "followed")
    intel, summary = _consume(tmp_path)
    assert summary["events_consumed"] >= 1
    assert store.list_unconsumed_rule_events() == []  # checkpointed
    evidence = intel.list_evidence()
    assert any(e.source_rule_id == "m1" for e in evidence)
    stats = intel.get_runtime_stats(next(
        d.definition_id for d in intel.list_definitions()
    ))
    assert stats is not None
    assert stats["followed"] == 1
    with intel._db() as conn:
        contributions = conn.execute(
            "SELECT contribution_id, receipt_id, active "
            "FROM rule_evidence_contributions WHERE receipt_id=?",
            ("rcpt-1",),
        ).fetchall()
    assert len(contributions) == 1
    assert contributions[0]["active"] == 1


def test_consume_outbox_violated_is_adherence_not_negative(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(store, "rcpt-1", "violated")
    intel, _ = _consume(tmp_path)
    assert intel.count_negative_evidence() == 0
    stats = intel.get_runtime_stats(next(
        d.definition_id for d in intel.list_definitions()
    ))
    assert stats["violated"] == 1


def test_consume_outbox_not_applicable_adds_negative_evidence(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(store, "rcpt-1", "not_applicable")
    intel, _ = _consume(tmp_path)
    assert intel.count_negative_evidence() == 1
    stats = intel.get_runtime_stats(next(
        d.definition_id for d in intel.list_definitions()
    ))
    assert stats["not_applicable"] == 1


def test_effective_feedback_replace_and_clear_retracts_only_current_receipt(
    tmp_path,
):
    legacy = SharedMemoryStore(tmp_path, "g1")
    _seed_record(legacy, "m1", "Submit code only after running tests")
    _seed_record(legacy, "m2", "Run tests before submitting code")
    _seed_receipt(
        legacy, "rcpt-1", "m1", session_id="trusted-1",
        session_trusted=True, session_source="host",
    )
    _seed_receipt(
        legacy, "rcpt-2", "m2", session_id="trusted-2",
        session_trusted=True, session_source="host",
    )

    # Hook feedback has stable authority, so a later event can replace and
    # then clear the effective state for exactly one receipt.
    _feedback(
        legacy, "rcpt-1", "followed", feedback_id="fb-1-followed",
        source="hook", authority=2, created_at="2026-01-01T00:00:01+00:00",
    )
    _feedback(
        legacy, "rcpt-2", "followed", feedback_id="fb-2-followed",
        source="hook", authority=2, created_at="2026-01-01T00:00:02+00:00",
    )
    intel, _ = _consume(tmp_path)
    assert intel.count_evidence() == 2
    projection_1 = intel.get_effective_feedback_projection("rcpt-1")
    projection_2 = intel.get_effective_feedback_projection("rcpt-2")
    definition_1 = projection_1["definition_id"]
    definition_2 = projection_2["definition_id"]

    _feedback(
        legacy, "rcpt-1", "not_applicable", feedback_id="fb-1-replaced",
        source="hook", authority=2, created_at="2026-01-01T00:00:03+00:00",
    )
    _consume(tmp_path)
    assert intel.count_evidence() == 1
    assert intel.count_negative_evidence() == 1
    assert intel.get_effective_feedback_projection("rcpt-1")[
        "effective_feedback_id"
    ] == "fb-1-replaced"

    _feedback(
        legacy, "rcpt-1", "unobserved", feedback_id="fb-1-cleared",
        source="hook", authority=2, created_at="2026-01-01T00:00:04+00:00",
    )
    _consume(tmp_path)
    projection = intel.get_effective_feedback_projection("rcpt-1")
    assert projection["effective_feedback_id"] == ""
    assert projection["outcome"] == "tombstone"
    assert intel.count_evidence() == 1  # rcpt-2 remains effective
    assert intel.count_negative_evidence() == 0
    remaining = intel.list_evidence()
    assert {item.receipt_id for item in remaining} == {"rcpt-2"}
    assert intel.get_runtime_stats(definition_2)["followed"] == 1
    with intel._db() as conn:
        runtime_ids = {
            row["feedback_id"]
            for row in conn.execute(
                "SELECT feedback_id FROM rule_runtime_feedback"
            ).fetchall()
        }
    assert runtime_ids == {"fb-2-followed"}


def test_confidence_zero_is_projected_without_becoming_unknown(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(
        store, "rcpt-1", "followed", feedback_id="fb-zero",
        confidence=0.0,
    )
    event = store.list_unconsumed_rule_events()[0]
    assert event["confidence"] == 0.0

    intel, _ = _consume(tmp_path)
    evidence = intel.list_evidence()
    assert len(evidence) == 1
    assert evidence[0].confidence == 0.0
    definition_id = intel.list_definitions()[0].definition_id
    assert intel.get_runtime_stats(definition_id)["followed"] == 1


def test_session_trusted_fails_closed_without_provenance(tmp_path):
    store = _setup_legacy(
        tmp_path, session_id="session-present", session_trusted=True,
        session_source="absent",
    )
    receipt = store.get_rule_match_receipt("rcpt-1")
    assert receipt is not None
    assert receipt.session_trusted is False

    _feedback(store, "rcpt-1", "followed", feedback_id="fb-untrusted")
    event = store.list_unconsumed_rule_events()[0]
    assert event["session_trusted"] == 0

    intel, _ = _consume(tmp_path)
    evidence = intel.list_evidence()
    assert len(evidence) == 1
    assert evidence[0].session_trusted == 0
    definition_id = intel.list_definitions()[0].definition_id
    assert intel.get_runtime_stats(definition_id)["distinct_sessions"] == 0


def test_consume_outbox_is_idempotent(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(store, "rcpt-1", "followed")
    first_intel, _ = _consume(tmp_path)
    evidence_count = first_intel.count_evidence()
    intel, summary = _consume(tmp_path)
    assert summary["events_seen"] == 0  # already consumed
    assert intel.count_evidence() == evidence_count
    definition_id = intel.list_definitions()[0].definition_id
    stats = intel.get_runtime_stats(definition_id)
    assert stats["followed"] == 1  # not double-counted


def test_assignment_change_projects_to_p3(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "rule-1", "run tests")
    record = legacy.get_record("rule-1")
    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    definition_id = service._definition_from_record(record).definition_id

    legacy.set_rule_assignments(
        record.memory_id,
        [{"target_type": "agent", "target_id": "agent-2"}],
    )
    service.consume_outbox(tmp_path, only_group=group)

    bindings = intel.list_bindings(definition_id=definition_id)
    assert any(item.target_id == "agent-2" for item in bindings)
    assert not any(
        item.target_id == "agent-1" and item.status == "active"
        for item in bindings
    )


def test_two_sources_same_definition_same_owner_update_isolated(tmp_path):
    legacy, intel, service, group, definition_id = (
        _setup_same_definition_sources(tmp_path)
    )

    legacy.set_rule_assignments(
        "m1", [{"target_type": "agent", "target_id": "agent-2"}],
    )
    service.consume_outbox(tmp_path, only_group=group)

    source_a = intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m1", active=True,
    )
    source_b = intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m2", active=True,
    )
    assert {row["target_id"] for row in source_a} == {"agent-2"}
    assert {row["target_id"] for row in source_b} == {"owner"}
    assert {row["definition_id"] for row in source_a + source_b} == {
        definition_id,
    }


def test_delete_one_source_preserves_other_source_binding(tmp_path):
    legacy, intel, service, group, definition_id = (
        _setup_same_definition_sources(tmp_path)
    )

    legacy.delete("m1")
    service.consume_outbox(tmp_path, only_group=group)

    assert intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m1", active=True,
    ) == []
    remaining = intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m2", active=True,
    )
    assert len(remaining) == 1
    assert intel.list_bindings(definition_id=definition_id, status="active")
    assert intel.get_source_link(group, "m1")["status"] == "deleted"
    assert intel.get_source_link(group, "m2")["status"] == "active"


def test_delete_source_deactivates_evidence_and_runtime(tmp_path):
    legacy = _setup_legacy(tmp_path)
    _feedback(legacy, "rcpt-1", "followed", feedback_id="fb-source-delete")
    intel, _ = _consume(tmp_path)
    definition_id = intel.list_definitions()[0].definition_id
    assert intel.count_evidence() == 1
    assert intel.get_runtime_stats(definition_id)["followed"] == 1

    legacy.delete("m1")
    RuleMergeService(intel).consume_outbox(tmp_path, only_group="g1")

    assert intel.count_evidence() == 0
    assert intel.get_runtime_stats(definition_id)["followed"] == 0
    with intel._db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_evidence_contributions "
            "WHERE source_rule_id=? AND active=1",
            ("m1",),
        ).fetchone()[0] == 0


def test_delete_last_source_revokes_binding(tmp_path):
    legacy, intel, service, group, definition_id = (
        _setup_same_definition_sources(tmp_path)
    )

    legacy.delete("m1")
    service.consume_outbox(tmp_path, only_group=group)
    assert intel.list_bindings(definition_id=definition_id, status="active")

    legacy.delete("m2")
    service.consume_outbox(tmp_path, only_group=group)
    assert intel.list_bindings(definition_id=definition_id, status="active") == []
    assert intel.list_bindings(definition_id=definition_id, status="revoked")


def test_rule_restore_reactivates_only_its_own_contributions(tmp_path):
    legacy, intel, service, group, _definition_id = (
        _setup_same_definition_sources(tmp_path)
    )
    legacy.set_rule_assignments(
        "m1", [{"target_type": "agent", "target_id": "agent-2"}],
    )
    service.consume_outbox(tmp_path, only_group=group)

    legacy.delete("m1")
    service.consume_outbox(tmp_path, only_group=group)
    GovernanceEngine(tmp_path, group, store=legacy).human_restore("m1")
    service.consume_outbox(tmp_path, only_group=group)

    source_a = intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m1", active=True,
    )
    source_b = intel.list_binding_contributions(
        share_group_id=group, source_memory_id="m2", active=True,
    )
    assert {row["target_id"] for row in source_a} == {"agent-2"}
    assert {row["target_id"] for row in source_b} == {"owner"}
    assert intel.get_source_link(group, "m1")["status"] == "active"


def test_merged_source_feedback_lands_on_canonical(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "提交代码前必须运行测试")
    _seed_record(legacy, "m2", "提交前必须执行测试")
    _seed_receipt(
        legacy, "rcpt-1", "m1", session_id="trusted-1",
        session_trusted=True, session_source="host",
    )
    _seed_receipt(
        legacy, "rcpt-2", "m2", session_id="trusted-2",
        session_trusted=True, session_source="host",
    )

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    for d in intel.list_definitions():
        for i in range(3):
            intel.upsert_evidence(build_evidence(
                definition_id=d.definition_id,
                source_rule_id=next(
                    mid for mid in ("m1", "m2")
                    if normalize_rule_text(
                        "提交代码前必须运行测试" if mid == "m1"
                        else "提交前必须执行测试",
                    ) == d.canonical_text
                ),
                agent_instance_id=f"a{i}", project_ref=f"p{i}",
                session_id=f"s{i}", session_trusted=1,
                content=d.canonical_text,
                observed_at=_now_iso(),
            ))
        intel.upsert_agent_reputation(
            agent_id="agent-2", success_rate=0.98, sample_count=200,
        )
    for i in range(3):
        intel.upsert_project_profile(
            project_ref=f"p{i}", production_level=1.0,
        )

    before_contributions = sorted(
        (row["source_memory_id"], row["legacy_assignment_hash"])
        for row in intel.list_binding_contributions(active=True)
    )
    candidates = service.scan_and_propose()
    cand = [p for p in candidates if p["status"] == "candidate"]
    assert cand, "synonym pair must be a merge candidate"
    pid = cand[0]["proposal_id"]
    context = AccessContext("test-admin", True, True, False)
    token = intel.issue_merge_capability(pid, context)
    intel.approve_proposal(
        pid, approved_by=context.principal,
        capability_token=token, access_context=context,
    )
    result = service.merge_proposal(pid, actor="admin")
    assert result["ok"] is True
    canonical_id = result["canonical_definition_id"]
    merged_id = result["merged_definition_ids"][0]

    contributions = intel.list_binding_contributions(active=True)
    assert sorted(
        (row["source_memory_id"], row["legacy_assignment_hash"])
        for row in contributions
    ) == before_contributions
    assert {row["definition_id"] for row in contributions} == {canonical_id}
    with intel._db() as conn:
        binding_definition_ids = {
            row["definition_id"]
            for row in conn.execute(
                "SELECT b.definition_id FROM rule_bindings b "
                "JOIN rule_binding_contributions c ON c.binding_id=b.binding_id "
                "WHERE c.active=1"
            ).fetchall()
        }
    assert binding_definition_ids == {canonical_id}

    # Feedback arrives for the *merged* source m2 after the merge.
    _feedback(legacy, "rcpt-2", "followed", feedback_id="fb-after-merge")
    service.consume_outbox(tmp_path)

    merged = intel.get_definition(merged_id)
    assert merged.status == "merged"  # not resurrected
    # The new evidence lands on the canonical definition.
    canonical_evidence = [
        e for e in intel.list_evidence(definition_id=canonical_id)
        if e.source_rule_id == "m2"
    ]
    assert canonical_evidence, "merged source feedback must project onto canonical"
    stats = intel.get_runtime_stats(canonical_id)
    assert stats is not None
    assert stats["followed"] >= 1
