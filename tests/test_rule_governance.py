"""P3 Merge Intelligence Governance Layer tests (P3-001/002/003).

The upgrade adds four guarantees on top of the structural merge layer:

  * Rule Strength (P3-002): MUST vs SHOULD on the same intent is a governance
    conflict, never a duplicate — and even a human-approved merge refuses it.
  * Maturity / Readiness (P3-001): a freshly-created rule never auto-merges,
    no matter how similar or how well-evidenced; cooldown and first-merge
    acknowledgment are separate gates on top of the readiness score.
  * Negative Evidence (P3-001 §5): real projects contradicting the rule block
    the merge.
  * Evidence Weight (P3-003): Agent reputation and project profile weight the
    votes; one dominant Agent cannot carry a merge alone.

The tests assert *behaviour that matters* — that a bad merge is refused on
every path and that the acceptance metrics stay at their safe values.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import (
    RuleStrength,
    build_definition,
    detect_strength,
)
from memoryguard.rule_evidence import build_evidence, build_negative_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_merge_policy import (
    AUTO_MERGE_MIN_EVIDENCE,
    AUTO_READINESS_SCORE,
    TRUSTED_MIN_SUCCESS_SAMPLES,
    WEIGHTED_EVIDENCE_MIN,
    compute_layers,
    evaluate_candidate,
    evidence_weight,
    merge_readiness_score,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aged(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _store(tmp_path) -> RuleMergeStore:
    return RuleMergeStore(tmp_path)


def _svc(store: RuleMergeStore) -> RuleMergeService:
    return RuleMergeService(store)


def _approve(store: RuleMergeStore, proposal_id: str):
    context = AccessContext("test-admin", True, True, False)
    token = store.issue_merge_capability(proposal_id, context)
    return store.approve_proposal(
        proposal_id, approved_by=context.principal,
        capability_token=token, access_context=context,
    )


def _governance_token(store: RuleMergeStore, proposal_id: str) -> tuple[str, AccessContext]:
    context = AccessContext("test-admin", True, True, False)
    return store.issue_merge_capability(proposal_id, context), context


def _def(text: str, *, created_at: str = "") -> object:
    return build_definition(text, created_at=created_at or _now_iso())


def _evidence(store: RuleMergeStore, definition_id: str, *, agents, projects, content, observed_at=""):
    for i, (agent, project) in enumerate(zip(agents, projects)):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=f"r-{definition_id}-{i}",
            agent_instance_id=agent,
            project_ref=project,
            session_id=f"s{i}", session_trusted=1,
            content=content,
            observed_at=observed_at or _now_iso(),
        ))


def _strong_evidence(store: RuleMergeStore, definition_id: str, content: str):
    """3 agents / 3 projects, all production-rated, aged observations."""
    _evidence(
        store, definition_id,
        agents=["agent-a", "agent-b", "agent-c"],
        projects=["p1", "p2", "p3"], content=content, observed_at=_aged(60),
    )
    for agent in ("agent-a", "agent-b", "agent-c"):
        store.upsert_agent_reputation(
            agent_id=agent, success_rate=0.98, rule_accuracy=0.98,
            sample_count=200, feedback_quality=0.95,
        )
    for project in ("p1", "p2", "p3"):
        store.upsert_project_profile(
            project_ref=project, production_level=1.0, owner_verified=True,
        )


def _eligible_evidence(store: RuleMergeStore, definition_id: str, content: str):
    """Add legacy-compatible evidence whose empty session is not untrusted."""
    for i, (agent, project) in enumerate(
        zip(("agent-a", "agent-b", "agent-c"), ("p1", "p2", "p3"))
    ):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=f"eligible-{definition_id}-{i}",
            agent_instance_id=agent, project_ref=project, session_id="",
            content=content, observed_at=_aged(60),
        ))


def _project_runtime(store: RuleMergeStore, definition_id: str):
    for i in range(TRUSTED_MIN_SUCCESS_SAMPLES):
        feedback_id = f"runtime-{definition_id}-{i}"
        receipt_id = f"receipt-{definition_id}-{i}"
        store.upsert_runtime_feedback(
            feedback_id=feedback_id, receipt_id=receipt_id,
            definition_id=definition_id, outcome="followed",
            agent_instance_id=f"runtime-agent-{i % 3}",
            project_ref=f"runtime-project-{i % 3}",
            session_id=f"runtime-session-{i}", source="hook", authority=2,
            session_trusted=1, created_at=_aged(30),
        )
        store.upsert_effective_feedback_projection(
            receipt_id=receipt_id, effective_feedback_id=feedback_id,
            definition_id=definition_id, outcome="followed",
            session_trusted=1, session_source="host",
        )
    store.recompute_runtime_stats(definition_id)


# ---------------------------------------------------------------------------
# P3-002: Rule Strength
# ---------------------------------------------------------------------------


def test_detect_strength_from_markers():
    assert detect_strength("提交代码前必须运行测试") == RuleStrength.MUST
    assert detect_strength("禁止提交未测试代码") == RuleStrength.MUST
    assert detect_strength("建议运行测试") == RuleStrength.SHOULD
    assert detect_strength("推荐使用pnpm") == RuleStrength.RECOMMENDED
    assert detect_strength("可以运行测试") == RuleStrength.SUGGESTION
    assert detect_strength("运行测试") == RuleStrength.OBSERVATION


def test_strength_and_polarity_interplay():
    # 禁止/不得 is MUST *and* negative polarity — two orthogonal dimensions.
    d = build_definition("不得使用npm安装依赖")
    assert d.rule_strength == "must"
    assert d.polarity == "negative"
    pos = build_definition("必须运行测试")
    assert pos.rule_strength == "must"
    assert pos.polarity == "positive"


def test_strength_conflict_never_merges_and_is_never_forced(tmp_path):
    store = _store(tmp_path)
    must = _def("提交代码前必须运行测试")
    should = _def("提交代码前建议运行测试")
    assert must.rule_strength != should.rule_strength
    store.upsert_definition(must)
    store.upsert_definition(should)
    _strong_evidence(store, must.definition_id, must.canonical_text)
    _strong_evidence(store, should.definition_id, should.canonical_text)

    proposals = _svc(store).scan_and_propose()
    conflicted = [p for p in proposals if p["status"] == "conflicted"]
    assert conflicted, "MUST+SHOULD on the same intent must surface as a conflict"
    assert conflicted[0]["conflict_type"] == "strength"

    pid = conflicted[0]["proposal_id"]
    # Auto refuses.
    result = _svc(store).merge_proposal(pid)
    assert result["ok"] is False
    assert result["conflict_type"] == "strength"
    # Human approval refuses too: resolving a strength conflict is not a merge.
    with pytest.raises(ValueError, match="rule_merge_approval_capability_required"):
        store.approve_proposal(
            pid,
        )
    assert store.count_definitions() == 2


# ---------------------------------------------------------------------------
# P3-001 §5: Negative Evidence
# ---------------------------------------------------------------------------


def test_negative_evidence_blocks_merge(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _evidence(store, a.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content=a.canonical_text)
    _evidence(store, b.definition_id, agents=["b1", "b2", "b3"], projects=["q1", "q2", "q3"], content=b.canonical_text)
    store.upsert_negative_evidence(build_negative_evidence(
        definition_id=b.definition_id, source_rule_id="neg",
        agent_instance_id="b1", project_ref="q1",
        content="项目实际使用npm且运行正常，不遵循该规则",
    ))

    proposals = _svc(store).scan_and_propose()
    assert all(p["status"] != "candidate" for p in proposals)
    pair = next(p for p in proposals if {a.definition_id, b.definition_id} == set(p["definition_ids"]))
    assert "negative_evidence" in pair["explanation"]


def test_negative_evidence_score_ratio_is_weighted(tmp_path):
    store = _store(tmp_path)
    a = _def("必须使用pnpm安装依赖")
    store.upsert_definition(a)
    _strong_evidence(store, a.definition_id, a.canonical_text)
    # One contradicting project, weight 1.0, vs three positive (weight ~0.89
    # each): ratio 1.0/(1.0+2.67) ≈ 0.27 >= 0.05 -> blocks.
    store.upsert_negative_evidence(build_negative_evidence(
        definition_id=a.definition_id, source_rule_id="neg",
        agent_instance_id="agent-a", project_ref="p1",
        content="使用npm且运行正常",
    ))
    score = _svc(store)._negative_score(a, a)
    assert score >= 0.05


# ---------------------------------------------------------------------------
# P3-001 §1/§2: Maturity engine + Readiness Score
# ---------------------------------------------------------------------------


def test_maturity_engine_stages(tmp_path):
    store = _store(tmp_path)
    svc = _svc(store)
    fresh = _def("提交代码前必须运行测试", created_at=_now_iso())
    store.upsert_definition(fresh)
    assert svc._maturity_of(fresh) == "observing"

    aged = _def("提交代码前必须运行测试", created_at=_aged(90))
    store.upsert_definition(aged)
    _strong_evidence(store, aged.definition_id, aged.canonical_text)
    # Maturity is driven by *this definition's own* runtime feedback, never by
    # borrowing another rule's agent reputation.  Seed >= 20 followed events
    # across >= 3 projects -> trusted.
    for i in range(TRUSTED_MIN_SUCCESS_SAMPLES):
        store.upsert_runtime_feedback(
            feedback_id=f"rt-{i}", definition_id=aged.definition_id,
            outcome="followed", agent_instance_id=f"agent-{i % 3}",
            project_ref=f"p{i % 3}", session_id=f"sess-{i}",
            session_trusted=1,
            created_at=_aged(30), source="user", authority=4,
        )
    store.recompute_runtime_stats(aged.definition_id)
    assert svc._maturity_of(aged) == "trusted"

    # Negative evidence blocks validation: candidate at best.
    store.upsert_negative_evidence(build_negative_evidence(
        definition_id=aged.definition_id, source_rule_id="neg",
        agent_instance_id="agent-a", project_ref="p1", content="违背",
    ))
    assert svc._maturity_of(aged) == "candidate"


def test_merge_readiness_score_formula_is_explicit():
    score = merge_readiness_score(
        duplicate_score=0.98, evidence_confidence=0.95, maturity=0.2,
        execution_success=0.5, source_diversity=1.0, stability=0.03,
    )
    expected = (
        0.25 * 0.98 + 0.20 * 0.95 + 0.20 * 0.2
        + 0.15 * 0.5 + 0.10 * 1.0 + 0.10 * 0.03
    )
    assert score == pytest.approx(expected)
    fresh = merge_readiness_score(
        duplicate_score=0.98, evidence_confidence=0.95, maturity=0.2,
        execution_success=0.5, source_diversity=1.0, stability=0.03,
    )
    mature = merge_readiness_score(
        duplicate_score=0.98, evidence_confidence=1.0, maturity=1.0,
        execution_success=0.95, source_diversity=1.0, stability=1.0,
    )
    # The whole point of the readiness gate: a brand-new rule with great
    # evidence scores below the auto threshold; a proven rule clears it.
    assert fresh < AUTO_READINESS_SCORE
    assert mature > AUTO_READINESS_SCORE


# ---------------------------------------------------------------------------
# P3-001 §3/§4: first-merge acknowledgment + cooldown
# ---------------------------------------------------------------------------


def test_fresh_candidate_never_auto_merges_even_with_strong_evidence(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试", created_at=_aged(90))
    b = _def("提交前必须执行测试", created_at=_aged(90))
    store.upsert_definition(a)
    store.upsert_definition(b)
    _strong_evidence(store, a.definition_id, a.canonical_text)
    _strong_evidence(store, b.definition_id, b.canonical_text)

    _svc(store).scan_and_propose()
    candidates = store.list_proposals(status="candidate")
    assert candidates, "strong evidence + matched strength must be a candidate"

    result = _svc(store).merge_proposal(candidates[0]["proposal_id"])
    assert result["ok"] is False
    assert result["blocked_reason"] == "auto_merge_not_ready"
    # The hard cold-start gates are cooldown and first-merge acknowledgment.
    assert "cooldown_active" in result["governance_reasons"]
    assert "first_merge_requires_approval" in result["governance_reasons"]

    # Explicit review of cold-start gates is insufficient without projected
    # runtime feedback; auto merge must remain fail-closed.
    pid = candidates[0]["proposal_id"]
    token, context = _governance_token(store, pid)
    store.acknowledge_first_merge(
        pid, actor="human", capability_token=token, access_context=context,
    )
    token, context = _governance_token(store, pid)
    store.clear_proposal_cooldown(
        pid, capability_token=token, access_context=context,
    )
    result = _svc(store).merge_proposal(pid)
    assert result["ok"] is False
    assert "runtime_feedback_missing" in result["governance_reasons"]


def test_auto_merge_requires_runtime_and_ready_projection(tmp_path):
    store = _store(tmp_path)
    svc = _svc(store)
    a = _def("提交代码前必须运行测试", created_at=_aged(90))
    b = _def("提交前必须执行测试", created_at=_aged(90))
    store.upsert_definition(a)
    store.upsert_definition(b)
    for definition in (a, b):
        _strong_evidence(store, definition.definition_id, definition.canonical_text)
        _eligible_evidence(store, definition.definition_id, definition.canonical_text)

    proposals = svc.scan_and_propose()
    candidates = [p for p in proposals if p["status"] == "candidate"]
    assert candidates
    pid = candidates[0]["proposal_id"]
    token, context = _governance_token(store, pid)
    store.acknowledge_first_merge(
        pid, actor="human", capability_token=token, access_context=context,
    )
    token, context = _governance_token(store, pid)
    store.clear_proposal_cooldown(
        pid, capability_token=token, access_context=context,
    )

    blocked = svc.merge_proposal(pid)
    assert blocked["ok"] is False
    assert "runtime_feedback_missing" in blocked["governance_reasons"]

    for definition in (a, b):
        _project_runtime(store, definition.definition_id)
    store.set_projection_state(
        "rule-intelligence", last_projected_event_id="runtime-ready",
        projection_lag=1,
    )
    svc.scan_and_propose()
    token, context = _governance_token(store, pid)
    store.clear_proposal_cooldown(
        pid, capability_token=token, access_context=context,
    )

    blocked = svc.merge_proposal(pid)
    assert blocked["ok"] is False
    assert blocked["barrier"]["error"] == "projection_barrier_lag: 1"

    store.set_projection_state(
        "rule-intelligence", last_projected_event_id="runtime-ready",
        projection_lag=0,
    )
    result = svc.merge_proposal(pid)
    assert result["ok"] is True
    assert result["decision"]["execution_mode"] == "auto"


def test_readiness_blocks_auto_for_young_unproven_rules(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试", created_at=_now_iso())
    b = _def("提交前必须执行测试", created_at=_now_iso())
    store.upsert_definition(a)
    store.upsert_definition(b)
    # Evidence with NO reputation: cold start, neutral weights, observing.
    _evidence(store, a.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content=a.canonical_text)
    _evidence(store, b.definition_id, agents=["b1", "b2", "b3"], projects=["q1", "q2", "q3"], content=b.canonical_text)

    _svc(store).scan_and_propose()
    candidates = store.list_proposals(status="candidate")
    result = _svc(store).merge_proposal(candidates[0]["proposal_id"])
    assert result["ok"] is False
    assert "readiness_below_auto" in result["governance_reasons"]
    assert result["readiness_score"] < AUTO_READINESS_SCORE


# ---------------------------------------------------------------------------
# P3-003: evidence weight / single-agent dominance
# ---------------------------------------------------------------------------


def test_evidence_weight_unknown_is_neutral_not_full_credit():
    # PR5: an unknown source defaults to 0.5, never to full credit — an
    # unvetted Agent must not outrank a verified production one.  (Recency is
    # time-based, so a fresh unknown observation still scores its 0.10 slice.)
    unknown = evidence_weight()
    assert unknown == pytest.approx(0.55)
    verified = evidence_weight(
        agent_reliability=0.98, project_importance=1.0,
        rule_specific_success=0.98, feedback_authority=1.0,
        recency=1.0, evidence_confidence=1.0,
    )
    experimental = evidence_weight(
        agent_reliability=0.6, project_importance=0.5,
        rule_specific_success=0.5, feedback_authority=0.5,
        recency=0.5, evidence_confidence=0.5,
    )
    assert verified > unknown > experimental
    # Low-confidence evidence weighs less than high-confidence evidence.
    low_confidence = evidence_weight(
        agent_reliability=0.98, project_importance=1.0,
        rule_specific_success=0.98, feedback_authority=1.0,
        recency=1.0, evidence_confidence=0.2,
    )
    assert low_confidence < verified


def test_single_agent_dominance_blocks_auto(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试", created_at=_aged(90))
    b = _def("提交前必须执行测试", created_at=_aged(90))
    store.upsert_definition(a)
    store.upsert_definition(b)
    # 3 evidence from a production agent (weight ~0.98) + 1 weak agent.
    for i in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=a.definition_id, source_rule_id=f"a{i}",
            agent_instance_id="agent-a", project_ref=f"p{i}",
            session_id=f"s{i}", session_trusted=1,
            content=a.canonical_text, observed_at=_aged(60),
        ))
    store.upsert_evidence(build_evidence(
        definition_id=b.definition_id, source_rule_id="b0",
        agent_instance_id="agent-b", project_ref="q0",
        session_id="t0", session_trusted=1,
        content=b.canonical_text, observed_at=_aged(60),
    ))
    store.upsert_agent_reputation(
        agent_id="agent-a", success_rate=0.98, rule_accuracy=0.98,
        sample_count=200, feedback_quality=0.95,
    )
    store.upsert_agent_reputation(
        agent_id="agent-b", success_rate=0.3, rule_accuracy=0.3,
        sample_count=2, feedback_quality=0.1,
    )
    for p in ("p0", "p1", "p2", "q0"):
        store.upsert_project_profile(project_ref=p, production_level=1.0)

    _svc(store).scan_and_propose()
    candidates = store.list_proposals(status="candidate")
    assert candidates
    result = _svc(store).merge_proposal(candidates[0]["proposal_id"])
    assert result["ok"] is False
    assert "single_agent_dominance" in result["governance_reasons"]
    assert store.metrics()["single_agent_dominance"] >= 1


def test_weighted_evidence_satisfies_threshold_with_neutral_weights(tmp_path):
    store = _store(tmp_path)
    a = _def("提交代码前必须运行测试")
    b = _def("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    # 3 independent evidence (neutral weight 1.0 each, summed for the pair:
    # 6 * 1.0 = 6.0 >= 2.5).  The weighted model must not break the previous
    # "3 evidence" behavior in cold start.
    _evidence(store, a.definition_id, agents=["a1", "a2", "a3"], projects=["p1", "p2", "p3"], content=a.canonical_text)
    _evidence(store, b.definition_id, agents=["b1", "b2", "b3"], projects=["q1", "q2", "q3"], content=b.canonical_text)
    svc = _svc(store)
    evidence = svc._combined_evidence(a, b)
    weights = svc._evidence_weights(evidence)
    assert sum(weights) >= WEIGHTED_EVIDENCE_MIN
    assert evaluate_candidate(a, b, evidence=evidence).evidence_ok is True


# ---------------------------------------------------------------------------
# P3-002 §5: strength evolution never becomes a merge candidate
# ---------------------------------------------------------------------------


def test_evolve_strength_creates_version_and_supersedes(tmp_path):
    store = _store(tmp_path)
    svc = _svc(store)
    a = _def("提交代码前必须运行测试")  # MUST
    store.upsert_definition(a)
    result = svc.evolve_strength(
        a.definition_id, "suggestion", reason="治理放宽", actor="admin",
    )
    new_id = result["new_definition_id"]
    assert result["version"]["old_strength"] == "must"
    assert result["version"]["new_strength"] == "suggestion"

    old = store.get_definition(a.definition_id)
    assert old.status == "superseded"
    assert old.superseded_by == new_id
    assert store.get_definition(new_id).status == "active"
    assert store.count_definition_versions() == 1

    # The scanner never proposes a definition against its own history.
    proposals = svc.scan_and_propose()
    assert not any(
        set(p["definition_ids"]) == {a.definition_id, new_id}
        for p in proposals
    )


def test_evolve_strength_rejects_noop(tmp_path):
    store = _store(tmp_path)
    svc = _svc(store)
    a = _def("提交代码前必须运行测试")
    store.upsert_definition(a)
    with pytest.raises(ValueError):
        svc.evolve_strength(a.definition_id, "must")
    with pytest.raises(ValueError):
        svc.evolve_strength("missing", "suggestion")


# ---------------------------------------------------------------------------
# Acceptance metrics stay safe
# ---------------------------------------------------------------------------


def test_metrics_governance_family_stays_safe_after_clean_merge(tmp_path):
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
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        definition_a=a, definition_b=b,
    )
    _approve(store, proposal["proposal_id"])
    result = _svc(store).merge_proposal(proposal["proposal_id"])
    assert result["ok"] is True

    metrics = store.metrics()
    assert metrics["auto_merge_precision"] >= 0.995
    assert metrics["strength_conflict_merge"] == 0
    assert metrics["negative_evidence_leak"] == 0
    assert metrics["first_merge_human_approval"] == 0
