"""P3 Rule Intelligence: merge undo precision tests (P3 §9).

Undo must restore the exact pre-merge state — original definitions, evidence
ownership and binding pointers — and must never degrade into a whole-database
rollback.  A merge already undone returns the original state and a second undo
is a no-op; unrelated definitions and bindings are untouched.
"""
from __future__ import annotations

import pytest

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore


def _store(tmp_path) -> RuleMergeStore:
    return RuleMergeStore(tmp_path)


def _setup(tmp_path, *, third_definition=False):
    store = _store(tmp_path)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    c = build_definition("使用 pnpm 安装依赖") if third_definition else None
    store.upsert_definition(a)
    store.upsert_definition(b)
    if c is not None:
        store.upsert_definition(c)
    for d, src in ((a, "rA"), (b, "rB")):
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
        ))
        store.upsert_evidence(build_evidence(
            definition_id=d.definition_id, source_rule_id=src,
            agent_instance_id="a1", project_ref="p1", session_id="s1",
            content=d.canonical_text,
        ))
    if c is not None:
        store.upsert_binding(build_binding(
            c.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-3", owner_agent_id="agent-3", created_by="backfill",
        ))
        store.upsert_evidence(build_evidence(
            definition_id=c.definition_id, source_rule_id="rC",
            agent_instance_id="c1", project_ref="p9", session_id="s9",
            content=c.canonical_text,
        ))
    return store, a, b, c


def _merge(store, a, b):
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    return RuleMergeService(store).merge_proposal(proposal["proposal_id"])


def test_undo_restores_definitions_bindings_and_evidence(tmp_path):
    store, a, b, _c = _setup(tmp_path)
    result = _merge(store, a, b)
    assert result["ok"] is True
    decision_id = result["decision"]["decision_id"]
    assert store.count_definitions() == 1

    undo = RuleMergeService(store).undo_decision(decision_id)
    assert undo["status"] == "undone"
    assert store.count_definitions() == 2
    active = {d.definition_id for d in store.list_definitions(status="active")}
    assert active == {a.definition_id, b.definition_id}
    binding_defs = {b.definition_id for b in store.list_bindings()}
    assert binding_defs == {a.definition_id, b.definition_id}
    evidence_defs = {e.definition_id for e in store.list_evidence()}
    assert evidence_defs == {a.definition_id, b.definition_id}


def test_undo_is_noop_on_already_undone_merge(tmp_path):
    store, a, b, _c = _setup(tmp_path)
    result = _merge(store, a, b)
    decision_id = result["decision"]["decision_id"]
    svc = RuleMergeService(store)
    first = svc.undo_decision(decision_id)
    assert first["status"] == "undone"
    second = svc.undo_decision(decision_id)
    assert second["status"] == "undone"
    assert second.get("already_undone") is True


def test_undo_never_touches_unrelated_definition(tmp_path):
    store, a, b, c = _setup(tmp_path, third_definition=True)
    result = _merge(store, a, b)
    decision_id = result["decision"]["decision_id"]
    # unrelated definition keeps its binding and evidence throughout
    c_bindings_before = {x.definition_id for x in store.list_bindings(definition_id=c.definition_id)}
    c_evidence_before = {x.definition_id for x in store.list_evidence(c.definition_id)}
    RuleMergeService(store).undo_decision(decision_id)
    assert {x.definition_id for x in store.list_bindings(definition_id=c.definition_id)} == c_bindings_before
    assert {x.definition_id for x in store.list_evidence(c.definition_id)} == c_evidence_before
    assert store.get_definition(c.definition_id) is not None
    assert store.get_definition(c.definition_id).status == "active"


def test_undo_requires_real_decision_id(tmp_path):
    store, _a, _b, _c = _setup(tmp_path)
    with pytest.raises(ValueError):
        RuleMergeService(store).undo_decision("missing-decision")


def test_merge_decision_is_auditable(tmp_path):
    store, a, b, _c = _setup(tmp_path)
    result = _merge(store, a, b)
    decision = store.get_merge_decision(result["decision"]["decision_id"])
    assert decision is not None
    assert decision["status"] == "merged"
    assert decision["canonical_definition_id"] in {a.definition_id, b.definition_id}
    assert set(decision["merged_definition_ids"]) == {
        x for x in (a.definition_id, b.definition_id)
        if x != decision["canonical_definition_id"]
    }
    assert len(decision["before_bindings"]) == len(decision["after_bindings"])
