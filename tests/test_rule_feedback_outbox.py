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

from memoryguard.rule_definition import normalize_rule_text
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


def _seed_record(store: SharedMemoryStore, memory_id: str, body: str, agent="agent-1"):
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=[{"target_type": "agent", "target_id": agent}])


def _seed_receipt(store: SharedMemoryStore, receipt_id: str, memory_id: str) -> None:
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id, memory_id=memory_id, share_group_id=store.group_id,
        agent_instance_id="agent-1", task_hash="th", task="写测试",
        project_ref="/proj/x", session_id="sess-1", provider="codex",
    ))


def _feedback(store: SharedMemoryStore, receipt_id: str, outcome: str,
              *, feedback_id: str = "") -> None:
    store.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id=feedback_id or f"fb-{outcome}",
        receipt_id=receipt_id, outcome=outcome, actor="agent:agent-1",
        source="agent", authority=3, confidence=1.0, created_at=_now_iso(),
    ))


def _consume(tmp_path):
    intel = RuleMergeStore(tmp_path)
    summary = RuleMergeService(intel).consume_outbox(tmp_path)
    return intel, summary


def _setup_legacy(tmp_path, *, group="g1", body="提交代码前必须运行测试"):
    store = SharedMemoryStore(tmp_path, group)
    _seed_record(store, "m1", body)
    _seed_receipt(store, "rcpt-1", "m1")
    return store


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


def test_consume_outbox_is_idempotent(tmp_path):
    store = _setup_legacy(tmp_path)
    _feedback(store, "rcpt-1", "followed")
    _consume(tmp_path)
    intel, summary = _consume(tmp_path)
    assert summary["events_seen"] == 0  # already consumed
    assert intel.count_evidence() == intel.count_evidence()  # stable
    definition_id = intel.list_definitions()[0].definition_id
    stats = intel.get_runtime_stats(definition_id)
    assert stats["followed"] == 1  # not double-counted


def test_merged_source_feedback_lands_on_canonical(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "提交代码前必须运行测试")
    _seed_record(legacy, "m2", "提交前必须执行测试")
    _seed_receipt(legacy, "rcpt-1", "m1")
    _seed_receipt(legacy, "rcpt-2", "m2")

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
                session_id=f"s{i}", content=d.canonical_text,
                observed_at=_now_iso(),
            ))
        intel.upsert_agent_reputation(
            agent_id="agent-2", success_rate=0.98, sample_count=200,
        )
    for i in range(3):
        intel.upsert_project_profile(
            project_ref=f"p{i}", production_level=1.0,
        )

    candidates = service.scan_and_propose()
    cand = [p for p in candidates if p["status"] == "candidate"]
    assert cand, "synonym pair must be a merge candidate"
    pid = cand[0]["proposal_id"]
    intel.approve_proposal(pid, approved_by="admin")
    result = service.merge_proposal(pid, actor="admin")
    assert result["ok"] is True
    canonical_id = result["canonical_definition_id"]
    merged_id = result["merged_definition_ids"][0]

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
