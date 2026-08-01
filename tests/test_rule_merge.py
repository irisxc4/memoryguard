"""P3 Rule Intelligence: definition merging tests.

Covers the core merge invariants from the P3 design doc:
  * same definition across two Agents collapses to 1 definition / 2 bindings
  * three-layer duplicate detection composes a stable duplicate_score
  * conflict polarity and parameter conflicts never auto-merge
  * duplicate evidence counts once
  * merge is atomic, scoped (before_bindings == after_bindings), and undoable
  * a concurrent second merge fails closed
  * the rule-creation path dual-writes into the intelligence layer
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.rule_definition import (
    RuleDefinition,
    build_definition,
    extract_intent,
    normalize_rule_text,
    semantic_hash,
)
from memoryguard.rule_binding import build_binding
from memoryguard.rule_evidence import RuleEvidence, build_evidence, dedupe_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_merge_policy import (
    AUTO_MERGE_MIN_EVIDENCE,
    compute_layers,
    contradiction_score,
    evaluate_candidate,
)
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.schema_v3 import EffectiveAgentContext, _now_iso
from memoryguard.shared_memory_store import SharedMemoryStore


def _store(tmp_path) -> RuleMergeStore:
    return RuleMergeStore(tmp_path)


def _svc(store: RuleMergeStore) -> RuleMergeService:
    return RuleMergeService(store)


def _def(text: str, definition_id: str = "", kind="procedure") -> RuleDefinition:
    return build_definition(text, definition_id=definition_id, kind=kind)


def _evidence(store: RuleMergeStore, definition_id: str, *, agents, projects, content, sessions=None):
    for i, (agent, project) in enumerate(zip(agents, projects)):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=f"r-{definition_id}-{i}",
            agent_instance_id=agent,
            project_ref=project,
            session_id=sessions[i] if sessions else f"s{i}",
            content=content,
        ))


# ---------------------------------------------------------------------------
# 1. Same definition, two Agents -> 1 definition / 2 bindings
# ---------------------------------------------------------------------------


def test_same_definition_two_agents_collapses_to_one(tmp_path):
    store = _store(tmp_path)
    text = "提交代码前必须运行测试"
    for agent in ("agent-a", "agent-b"):
        d = _def(text)
        store.upsert_definition(d)
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id=agent, owner_agent_id=agent, created_by="backfill",
        ))
    assert store.count_definitions() == 1
    assert store.count_bindings() == 2
    binding_agents = {b.target_id for b in store.list_bindings()}
    assert binding_agents == {"agent-a", "agent-b"}


def test_synonym_surface_wording_collapses_to_same_definition(tmp_path):
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    assert a.semantic_hash == b.semantic_hash
    assert extract_intent(a.canonical_text).action == extract_intent(b.canonical_text).action


# ---------------------------------------------------------------------------
# 2. Three-layer duplicate detection
# ---------------------------------------------------------------------------


def test_three_layer_duplicate_detection_weights(tmp_path):
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    layers = compute_layers(a, b)
    # Exact semantic hash matches (synonyms normalised), intent matches fully,
    # and the semantic layer over the synonym-collapsed surface also matches.
    assert layers.hash_score == 1.0
    assert layers.intent_score == 1.0
    assert layers.semantic_score > 0.9
    assert layers.duplicate_score > 0.9


def test_duplicate_score_formula_is_explicit(tmp_path):
    a = _def("必须运行测试")
    b = _def("必须执行测试")
    layers = compute_layers(a, b)
    expected = (
        0.3 * layers.hash_score
        + 0.4 * layers.intent_score
        + 0.3 * layers.semantic_score
    )
    assert layers.duplicate_score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 3. Never auto-merge conflict / parameter clashes
# ---------------------------------------------------------------------------


def test_polarity_conflict_never_auto_merges(tmp_path):
    pos = _def("必须运行测试")
    neg = _def("不要运行测试")
    assert pos.polarity != neg.polarity
    assert contradiction_score(pos, neg) == 1.0
    evs = [build_evidence(agent_instance_id=f"a{i}", project_ref=f"p{i}", session_id=f"s{i}", content="x") for i in range(3)]
    assessment = evaluate_candidate(pos, neg, evidence=evs)
    assert assessment.can_auto_merge is False
    assert "polarity_conflict" in assessment.reasons


def test_parameter_conflict_never_auto_merges(tmp_path):
    pytest_def = _def("Python项目运行pytest")
    unittest_def = _def("Python项目运行unittest")
    assert pytest_def.semantic_hash != unittest_def.semantic_hash
    evs = [build_evidence(agent_instance_id=f"a{i}", project_ref=f"p{i}", session_id=f"s{i}", content="x") for i in range(3)]
    assessment = evaluate_candidate(pytest_def, unittest_def, evidence=evs)
    assert assessment.can_auto_merge is False
    assert "parameter_conflict" in assessment.reasons


def test_duplicate_evidence_counts_once(tmp_path):
    evidence = [
        build_evidence(
            agent_instance_id="a1", project_ref="p1",
            session_id="same-session", content="same content",
        )
        for _ in range(100)
    ]
    unique = dedupe_evidence(evidence)
    assert len(unique) == 1


# ---------------------------------------------------------------------------
# 4. Proposal lifecycle
# ---------------------------------------------------------------------------


def test_auto_merge_requires_independent_evidence(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    # Fewer than AUTO_MERGE_MIN_EVIDENCE distinct observations: never auto.
    _evidence(store, a.definition_id, agents=["a1"], projects=["p1"], content="提交代码前必须运行测试")
    _evidence(store, b.definition_id, agents=["a2"], projects=["p2"], content="提交前必须执行测试")
    proposals = _svc(store).scan_and_propose()
    assert proposals
    assert all(p["status"] == "rejected" for p in proposals)


def test_scan_proposes_candidate_when_all_conditions_hold(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _evidence(store, a.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content="提交代码前必须运行测试")
    _evidence(store, b.definition_id, agents=["b1", "b2", "b3"], projects=["q1", "q2", "q3"], content="提交前必须执行测试")
    proposals = _svc(store).scan_and_propose()
    assert any(p["status"] == "candidate" for p in proposals)


# ---------------------------------------------------------------------------
# 5. Atomic merge + scope invariance + undo
# ---------------------------------------------------------------------------


def test_merge_is_atomic_and_scope_invariant(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d, src in ((a, "rA"), (b, "rB")):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-2", owner_agent_id="agent-2", created_by="backfill",
        ))
        _evidence(store, d.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content="提交代码前必须运行测试")
    before = {b.audience_identity() for b in store.list_bindings()}

    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    result = _svc(store).merge_proposal(proposal["proposal_id"])
    assert result["ok"] is True

    after = {b.audience_identity() for b in store.list_bindings()}
    assert before == after, "merge must never expand permission"
    assert store.count_definitions() == 1
    active = store.list_definitions(status="active")
    assert len(active) == 1
    canonical_id = active[0].definition_id
    assert canonical_id in {a.definition_id, b.definition_id}
    assert {b.definition_id for b in store.list_bindings()} == {canonical_id}


def test_merge_undo_restores_original_state(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d, src in ((a, "rA"), (b, "rB")):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))
        store.upsert_evidence(build_evidence(
            definition_id=d.definition_id, source_rule_id=src,
            agent_instance_id="a1", project_ref="p1", session_id="s1",
            content="提交代码前必须运行测试",
        ))
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    decision = _svc(store).merge_proposal(proposal["proposal_id"])["decision"]
    assert store.count_definitions() == 1

    undo = _svc(store).undo_decision(decision["decision_id"])
    assert undo["status"] == "undone"
    assert store.count_definitions() == 2
    active = {d.definition_id for d in store.list_definitions(status="active")}
    assert active == {a.definition_id, b.definition_id}
    binding_defs = {b.definition_id for b in store.list_bindings()}
    assert binding_defs == {a.definition_id, b.definition_id}
    evidence_defs = {e.definition_id for e in store.list_evidence()}
    assert evidence_defs == {a.definition_id, b.definition_id}


# ---------------------------------------------------------------------------
# 6. Concurrency fails closed
# ---------------------------------------------------------------------------


def test_concurrent_second_merge_fails_closed(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d in (a, b):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))
        _evidence(store, d.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content="x")
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    svc = _svc(store)
    first = svc.merge_proposal(proposal["proposal_id"])
    assert first["ok"] is True
    # A racing second merge on the already-merged proposal must fail.
    with pytest.raises((ValueError, RuntimeError)):
        store.set_proposal_status(proposal["proposal_id"], "approved")
        svc.merge_proposal(proposal["proposal_id"])
    decisions = store.list_merge_decisions()
    assert len(decisions) == 1


# ---------------------------------------------------------------------------
# 7. Dual-write from rule creation
# ---------------------------------------------------------------------------


def test_rule_creation_dual_writes_definition(tmp_path):
    from memoryguard.rule_creation import RuleCreationService

    group = "g1"
    AgentBindingStore(tmp_path).bind_agent("agent-a", group)
    store = SharedMemoryStore(tmp_path, group)
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id=group,
        project_ref=str(tmp_path / "project"), session_id="s1",
    )
    service = RuleCreationService(
        tmp_path, group, store=store,
        merge_service=_svc(_store(tmp_path)),
    )
    decision = service.create_rule_from_text("提交代码前必须运行测试", context)
    assert decision.status == "created"
    intelligence = _store(tmp_path)
    definitions = intelligence.list_definitions()
    assert len(definitions) == 1
    bindings = intelligence.list_bindings()
    assert len(bindings) >= 1
    assert all(b.definition_id == definitions[0].definition_id for b in bindings)


def test_shadow_verify_reports_permission_diff(tmp_path):
    store = _store(tmp_path)
    d = _def("必须运行测试")
    store.upsert_definition(d)
    store.upsert_binding(build_binding(
        d.definition_id, share_group_id="g1", target_type="system",
        target_id="", owner_agent_id="admin", created_by="manual",
        authorization="admin",
    ))
    store.upsert_evidence(build_evidence(
        definition_id=d.definition_id, source_rule_id="r1",
        agent_instance_id="a1", project_ref="p1", session_id="s1",
        content="必须运行测试",
    ))
    context = EffectiveAgentContext(
        agent_instance_id="a1", share_group_id="g1",
        project_ref=canonical_project_ref(str(Path("p1"))),
    )
    result = store.shadow_verify(context, legacy_records=[("r1", [])])
    assert result["permission_diff"] >= 1
