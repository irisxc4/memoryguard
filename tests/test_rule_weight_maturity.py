"""Evidence weighting and maturity tests (PR5).

Before this layer an unknown evidence source defaulted to full weight (1.0) so
an unvetted Agent outranked a verified production one, evidence confidence did
not participate in the weight, duplicate receipts could inflate the counts, and
maturity borrowed another rule's agent reputation instead of the definition's
own execution history.  Now:

  * unknown sources shrink to a 0.5 neutral prior (never full credit);
  * evidence confidence, project criticality/owner and feedback authority all
    participate in the weight;
  * independent evidence is keyed on share group + Agent + project + source
    root/object + session + content, so repeated receipts collapse;
  * maturity is driven by the definition's own runtime feedback.
"""
from __future__ import annotations

from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import (
    NegativeEvidence,
    build_evidence,
    build_negative_evidence,
    dedupe_evidence,
)
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.rule_merge_policy import evidence_weight
from memoryguard.schema_v3 import _now_iso


def _definition(store: RuleMergeStore, text="提交代码前必须运行测试"):
    definition = build_definition(text)
    store.upsert_definition(definition)
    return definition


def test_unknown_evidence_does_not_outweigh_verified_production(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition(store)
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id, source_rule_id="m1",
        agent_instance_id="unknown-agent", project_ref="exp-project",
        session_id="s1", content=definition.canonical_text,
    ))
    store.upsert_agent_reputation(
        agent_id="prod-agent", success_rate=0.98, rule_accuracy=0.98,
        sample_count=200, feedback_quality=0.95,
    )
    store.upsert_project_profile(
        project_ref="prod-project", production_level=1.0, criticality=0.8,
        owner_verified=True,
    )
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id, source_rule_id="m2",
        agent_instance_id="prod-agent", project_ref="prod-project",
        session_id="s2", content=definition.canonical_text,
    ))

    svc = RuleMergeService(store)
    weights = svc._evidence_weights(store.list_evidence())
    unknown_w = weights[0]
    verified_w = weights[1]
    assert verified_w > unknown_w, (
        "a verified production Agent must outweigh an unknown one"
    )
    assert unknown_w < 1.0, "unknown source must not get full credit"


def test_low_confidence_evidence_has_lower_weight(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition(store)
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id, source_rule_id="high",
        agent_instance_id="a", project_ref="p", session_id="s1",
        content=definition.canonical_text, confidence=1.0,
    ))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id, source_rule_id="low",
        agent_instance_id="a", project_ref="p", session_id="s2",
        content=definition.canonical_text, confidence=0.2,
    ))
    svc = RuleMergeService(store)
    high_w, low_w = svc._evidence_weights(store.list_evidence())
    assert high_w > low_w


def test_duplicate_receipts_count_as_one_independent_evidence(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition(store)
    # Same fact, two receipts, same Agent/project/session/content.
    first = build_evidence(
        definition_id=definition.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s",
        receipt_id="r1", content=definition.canonical_text,
    )
    second = build_evidence(
        definition_id=definition.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s",
        receipt_id="r2", content=definition.canonical_text,
    )
    assert first.evidence_id != second.evidence_id
    assert first.independence_key == second.independence_key
    assert len(dedupe_evidence([first, second])) == 1


def test_distinct_sessions_are_independent_evidence(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition(store)
    a = build_evidence(
        definition_id=definition.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s1",
        content=definition.canonical_text,
    )
    b = build_evidence(
        definition_id=definition.definition_id, source_rule_id="m1",
        agent_instance_id="a", project_ref="p", session_id="s2",
        content=definition.canonical_text,
    )
    assert a.independence_key != b.independence_key
    assert len(dedupe_evidence([a, b])) == 2


def test_negative_evidence_requires_distinct_trusted_sessions(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = _definition(store)
    same_session = [
        build_negative_evidence(
            definition_id=definition.definition_id, source_rule_id="m1",
            agent_instance_id="a", project_ref="p", session_id="s",
            content="违背", session_trusted=1,
        ),
        build_negative_evidence(
            definition_id=definition.definition_id, source_rule_id="m1",
            agent_instance_id="a", project_ref="p", session_id="s",
            content="违背", session_trusted=1,
        ),
    ]
    distinct_sessions = [
        build_negative_evidence(
            definition_id=definition.definition_id, source_rule_id="m1",
            agent_instance_id="a", project_ref="p", session_id="s1",
            content="违背", session_trusted=1,
        ),
        build_negative_evidence(
            definition_id=definition.definition_id, source_rule_id="m1",
            agent_instance_id="a", project_ref="p", session_id="s2",
            content="违背", session_trusted=1,
        ),
    ]
    assert len(dedupe_evidence(same_session)) == 1
    assert len(dedupe_evidence(distinct_sessions)) == 2


def test_definition_maturity_uses_rule_specific_feedback(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store)
    aged = (
        datetime.now(timezone.utc) - timedelta(days=90)
    ).isoformat()
    definition = build_definition("提交代码前必须运行测试", created_at=aged)
    store.upsert_definition(definition)
    for i in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=definition.definition_id, source_rule_id=f"e{i}",
            agent_instance_id=f"a{i}", project_ref=f"p{i}",
            session_id=f"s{i}", content=definition.canonical_text,
        ))
    # Even with an impeccable agent reputation, NO rule-specific feedback means
    # the definition cannot claim validated — it must not borrow the agent's
    # track record from other rules.
    store.upsert_agent_reputation(
        agent_id="a0", success_rate=0.98, rule_accuracy=0.98,
        sample_count=500, feedback_quality=0.98,
    )
    assert svc._maturity_of(definition) == "candidate"

    # 12 rule-specific followed events across 3 projects -> validated.
    for i in range(12):
        store.upsert_runtime_feedback(
            feedback_id=f"rt-{i}", definition_id=definition.definition_id,
            outcome="followed", agent_instance_id=f"a{i % 3}",
            project_ref=f"p{i % 3}", session_id=f"sess-{i}",
            created_at=_now_iso(), source="user", authority=4,
        )
    store.recompute_runtime_stats(definition.definition_id)
    assert svc._maturity_of(definition) == "validated"


def test_proposal_persists_weight_breakdown(tmp_path):
    store = RuleMergeStore(tmp_path)
    svc = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    for d in (a, b):
        for i in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=d.definition_id, source_rule_id=f"{d.definition_id}-{i}",
                agent_instance_id=f"a{i}", project_ref=f"p{i}",
                session_id=f"s{i}", content=d.canonical_text,
            ))
        store.upsert_agent_reputation(
            agent_id="a0", success_rate=0.98, rule_accuracy=0.98,
            sample_count=200, feedback_quality=0.95,
        )
        store.upsert_project_profile(project_ref="p0", production_level=1.0)

    candidates = svc.scan_and_propose()
    cand = [p for p in candidates if p["status"] == "candidate"]
    assert cand
    import json
    breakdown = json.loads(cand[0].get("weight_breakdown") or "{}")
    assert breakdown.get("per_agent"), "proposal must persist a weight breakdown"
    assert breakdown.get("total_weight", 0) > 0
