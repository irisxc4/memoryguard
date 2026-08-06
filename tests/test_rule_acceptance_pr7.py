"""Acceptance & bounded-scan tests (PR7).

The old acceptance values were self-certifying: ``merge_undo_success`` and
``migration_loss`` were constants the code reported to itself, precision was
computed from booleans the merge service wrote into its own decision, and a
canonical read that silently fell back to legacy still "passed".  Now every
value is derived from persisted state and the canonical read must actually
engage.
"""
from __future__ import annotations

import json as _json

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.governance_scope import (
    GovernanceScope,
    build_shared_memory_graph,
    share_group_projection_path,
)
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_read_path import RuleReadPath
from memoryguard.rule_reconciliation import RuleReconciliationStore
from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _seed_record(store, memory_id, body, *, agent="agent-1"):
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=[{"target_type": "agent", "target_id": agent}])


def _backfill(tmp_path, group="g1", bodies=("提交代码前必须运行测试",)):
    legacy = SharedMemoryStore(tmp_path, group)
    for i, body in enumerate(bodies):
        _seed_record(legacy, f"m{i}", body)
    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    return legacy, intel, service


def _activate_canonical(tmp_path, group, intel, legacy):
    """Persist group-level canonical activation + full readiness (Req8 gate).

    The Req8 gate only switches ``effective_read_path`` to
    ``rule-intelligence`` when ``rule_canonical_state`` activation is active
    *and* ``canonical_reconciliation_status`` reports ``canonical_ready``.
    This writes the projection graph and the activation row so the canonical
    read can actually engage (its source links are already backfilled).
    """
    scope = GovernanceScope(mode="share_group", share_group_id=group)
    out_path = share_group_projection_path(tmp_path, scope)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _json.dumps(
            build_shared_memory_graph(tmp_path, group), ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    RuleReconciliationStore(intel).set_canonical_activation(
        group, activation_status="active",
        canonical_digest="test-digest", read_path="rule-intelligence",
    )


def test_governance_acceptance_zero_on_clean_state(tmp_path):
    _, intel, _ = _backfill(tmp_path)
    acceptance = intel.governance_acceptance()
    for key in (
        "definition_strength_identity_collision", "canonical_read_context_diff",
        "backfill_resurrection_count", "proposal_duplicate_count",
        "human_hard_gate_bypass_count", "evidence_independence_violation",
        "migration_binding_multiset_diff", "undo_state_digest_diff",
        "rule_intelligence_event_lag",
    ):
        assert acceptance[key] == 0, key
    # A clean store has no observed merge decision.  Precision is therefore
    # unobserved, not a vacuous pass; acceptance must remain fail-closed.
    assert acceptance["passed"] is False
    assert acceptance["auto_merge_precision"] == 0.0
    assert acceptance["auto_merge_precision_status"] == "unobserved"
    assert acceptance["merge_decision_count"] == 0


def test_scan_is_bounded_not_quadratic(tmp_path):
    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store)
    n_distinct = 200
    bodies = [
        f"规则{i}: 必须使用tool{i}完成构建" for i in range(n_distinct)
    ]
    # Five synonym pairs: "使用" vs "采用" collapse to the same intent hash but
    # are distinct Definitions, so only these pairs share a semantic bucket.
    for i in range(5):
        bodies.append(f"规则{i}: 必须采用tool{i}完成构建")
    for body in bodies:
        store.upsert_definition(build_definition(body))
    total_pairs = len(bodies) * (len(bodies) - 1) // 2

    svc.scan_and_propose()
    summary = svc.last_scan_summary
    # The full O(N²) sweep would evaluate every pair; the bounded scan only
    # evaluates pairs inside the same semantic bucket (the five synonyms).
    assert summary["pairs_evaluated"] == 5
    assert summary["pairs_evaluated"] < total_pairs // 100


def test_repeated_scan_has_no_proposal_duplicates(tmp_path):
    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d in (a, b):
        for i in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=d.definition_id,
                source_rule_id=f"{d.definition_id}-{i}",
                agent_instance_id=f"a{i}", project_ref=f"p{i}",
                session_id=f"s{i}", content=d.canonical_text,
            ))
        store.upsert_agent_reputation(
            agent_id="a0", success_rate=0.98, sample_count=200,
        )
    for _ in range(3):
        svc.scan_and_propose()
    assert store.governance_acceptance()["proposal_duplicate_count"] == 0


def test_migration_loss_is_computed_not_reported(tmp_path):
    legacy, intel, _ = _backfill(tmp_path)
    assert intel.metrics()["migration_loss"] == 0
    # A NEW governed legacy rule that was never backfilled is real loss: it is
    # not covered by any source link, so the canonical read would miss it.
    _seed_record(legacy, "m2", "必须使用pnpm安装依赖")
    assert intel.metrics()["migration_loss"] >= 1


def test_canonical_read_engages_when_intelligence_exists(tmp_path):
    legacy, intel, _ = _backfill(tmp_path)
    for d in intel.list_definitions():
        intel.upsert_evidence(build_evidence(
            definition_id=d.definition_id, source_rule_id="m0",
            agent_instance_id="a0", project_ref="p0", session_id="s0",
            content=d.canonical_text,
        ))
    _activate_canonical(tmp_path, "g1", intel, legacy)
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=EffectiveAgentContext("agent-1", "g1"),
        read_path="rule-intelligence",
    )
    assert packet["read_path"]["mode"] == "rule-intelligence"


def test_real_store_readiness_is_complete_without_monkeypatch(tmp_path):
    legacy, intel, _ = _backfill(tmp_path)
    for definition in intel.list_definitions():
        intel.upsert_evidence(build_evidence(
            definition_id=definition.definition_id, source_rule_id="m0",
            agent_instance_id="a0", project_ref="p0", session_id="s0",
            content=definition.canonical_text,
        ))
    context = EffectiveAgentContext("agent-1", "g1")
    read = RuleReadPath(tmp_path, "g1")
    readiness = read.canonical_readiness(
        legacy_store=legacy, context=context,
    )
    assert readiness["ready"] is True
    assert readiness["checks"]["binding_contribution_diff"] == 0
    assert readiness["checks"]["shadow"] == {
        "missing": [], "extra": [], "permission_diff": 0,
    }


def test_acceptance_fails_when_canonical_read_falls_back(tmp_path):
    legacy, intel, _ = _backfill(tmp_path)
    # The group is canonical-activated (Req8 gate passes), but no evidence is
    # anchored to real memory ids: the canonical read cannot resolve and must
    # fall back to legacy — an acceptance gate that requires canonical
    # engagement must fail here, not silently pass.
    _activate_canonical(tmp_path, "g1", intel, legacy)
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=EffectiveAgentContext("agent-1", "g1"),
        read_path="rule-intelligence",
    )
    assert packet["read_path"]["mode"] != "rule-intelligence"
    # Once evidence carries the real memory id, the canonical read engages.
    for d in intel.list_definitions():
        intel.upsert_evidence(build_evidence(
            definition_id=d.definition_id, source_rule_id="m0",
            agent_instance_id="a0", project_ref="p0", session_id="s0",
            content=d.canonical_text,
        ))
    packet = build_context_packet(
        legacy, task="写测试",
        effective_context=EffectiveAgentContext("agent-1", "g1"),
        read_path="rule-intelligence",
    )
    assert packet["read_path"]["mode"] == "rule-intelligence"


def test_evidence_independence_violation_metric(tmp_path):
    store = RuleMergeStore(tmp_path)
    d = build_definition("提交代码前必须运行测试")
    store.upsert_definition(d)
    # The DB-level independence invariant keeps one strongest row for the
    # same fact; governance must report no residual duplicate rows.
    store.upsert_evidence(build_evidence(
        definition_id=d.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s",
        receipt_id="r1", content=d.canonical_text,
    ))
    store.upsert_evidence(build_evidence(
        definition_id=d.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s",
        receipt_id="r2", content=d.canonical_text,
    ))
    assert store.count_evidence() == 1
    assert store.governance_acceptance()["evidence_independence_violation"] == 0
