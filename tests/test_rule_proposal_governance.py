"""Proposal identity, approval and TOCTOU tests (PR3).

Before this layer, a proposal id contained a timestamp so every scan created a
fresh row — cooldown restarted forever and the first-merge acknowledgment was
stranded on a dead row.  And a ``merge_proposal(actor='admin')`` string was the
only thing separating the human path from the automatic one, so a force-approved
proposal could bypass the polarity/parameter gates.  Now:

  * the proposal id is stable across scans (UPSERT preserves candidate_since /
    cooldown / first-merge acknowledgment);
  * approval is first-class data (``rule_merge_approvals``) and the actor string
    grants nothing by itself;
  * the merge transaction re-verifies the pair identity, the expected definition
    revisions, the evidence digest and the hard gates against current rows, so
    a definition edited between scan/approval and execution cannot merge.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from memoryguard.rule_definition import build_definition
from memoryguard.rule_binding import build_binding
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import _now_iso


def _seed(store: RuleMergeStore, definition, *, tag: str, count: int = 3):
    for i in range(count):
        store.upsert_evidence(build_evidence(
            definition_id=definition.definition_id,
            source_rule_id=f"{tag}-{i}", agent_instance_id=f"a{i}",
            project_ref=f"p{i}", session_id=f"s{i}", session_trusted=1,
            content=definition.canonical_text, observed_at=_now_iso(),
        ))


def _reps(store: RuleMergeStore) -> None:
    for i in range(3):
        store.upsert_agent_reputation(
            agent_id=f"a{i}", success_rate=0.98, rule_accuracy=0.98,
            sample_count=200, feedback_quality=0.95,
        )
        store.upsert_project_profile(
            project_ref=f"p{i}", production_level=1.0, criticality=0.8,
            owner_verified=True,
        )


def _candidate(store: RuleMergeStore, service: RuleMergeService):
    proposals = service.scan_and_propose()
    candidates = [p for p in proposals if p["status"] == "candidate"]
    assert candidates, "expected a merge candidate"
    return candidates[0]


def _approve(store: RuleMergeStore, proposal_id: str, **kwargs):
    return store.approve_proposal(
        proposal_id,
        approved_by="admin",
        capability_id="admin:test-suite",
        **kwargs,
    )


def _toctou_fixture(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)
    candidate = _candidate(store, service)
    proposal = store.get_proposal(candidate["proposal_id"])
    assert proposal is not None
    assert {
        "definition_revision_a", "definition_revision_b", "evidence_digest",
        "binding_digest", "runtime_digest", "assessment_revision",
        "policy_version",
    } <= proposal.keys()
    assert proposal["evidence_digest"]
    assert proposal["binding_digest"]
    assert proposal["runtime_digest"]
    assert proposal["policy_version"]
    _approve(store, candidate["proposal_id"])
    return store, service, a, b, candidate["proposal_id"]


def test_repeated_scan_reuses_proposal_and_preserves_cooldown(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)

    first = _candidate(store, service)
    p1 = store.get_proposal(first["proposal_id"])
    assert p1["cooldown_until"], "fresh candidate must enter the cooldown"
    assert p1["candidate_since"]

    # A second scan must reuse the same proposal id, not mint a new row.
    second = _candidate(store, service)
    assert second["proposal_id"] == first["proposal_id"]
    p2 = store.get_proposal(first["proposal_id"])
    assert p2["cooldown_until"] == p1["cooldown_until"], (
        "re-scan must not restart the cooldown"
    )
    assert p2["candidate_since"] == p1["candidate_since"]
    assert p2["assessment_revision"] == p1["assessment_revision"] + 1
    assert store.metrics()["proposal_count"] == 1


@pytest.mark.parametrize(
    ("drift", "error"),
    [
        ("definition", "rule_merge_definition_revision_drift"),
        ("evidence", "rule_merge_evidence_digest_drift"),
        ("binding", "rule_merge_binding_digest_drift"),
        ("runtime", "rule_merge_runtime_digest_drift"),
        ("assessment", "rule_merge_assessment_snapshot_mismatch"),
        ("policy", "rule_merge_policy_snapshot_mismatch"),
    ],
)
def test_human_merge_rechecks_complete_toctou_snapshot(
    tmp_path, drift, error,
):
    store, service, a, b, proposal_id = _toctou_fixture(tmp_path)
    snapshot = store.get_proposal(proposal_id)
    assert snapshot is not None
    if drift == "definition":
        store.bump_definition_revision(a.definition_id)
    elif drift == "evidence":
        store.upsert_evidence(build_evidence(
            definition_id=a.definition_id, source_rule_id="late-evidence",
            agent_instance_id="late-agent", project_ref="late-project",
            session_id="", content=a.canonical_text,
        ))
    elif drift == "binding":
        store.upsert_binding(build_binding(
            a.definition_id, share_group_id="g1", target_type="agent",
            target_id="late-agent", owner_agent_id="late-agent",
            created_by="test",
        ))
    elif drift == "runtime":
        store.upsert_runtime_feedback(
            feedback_id="late-runtime", definition_id=a.definition_id,
            outcome="followed", session_id="late-session",
            session_trusted=1,
        )
    else:
        column = "assessment_revision" if drift == "assessment" else "policy_version"
        value = "changed-policy" if drift == "policy" else None
        with store._db() as conn:
            if value is None:
                conn.execute(
                    "UPDATE rule_merge_proposals "
                    "SET assessment_revision=assessment_revision+1 "
                    "WHERE proposal_id=?",
                    (proposal_id,),
                )
            else:
                conn.execute(
                    f"UPDATE rule_merge_proposals SET {column}=? "
                    "WHERE proposal_id=?",
                    (value, proposal_id),
                )

    approval = store.get_valid_approval(proposal_id)
    assert approval is not None
    canonical, merged = service._pick_canonical(a, b)
    expected_revisions = {
        snapshot["definition_ids"][0]: snapshot["definition_revision_a"],
        snapshot["definition_ids"][1]: snapshot["definition_revision_b"],
    }
    with pytest.raises(RuntimeError, match=error):
        store.execute_merge(
            proposal_id=proposal_id,
            canonical_definition_id=canonical.definition_id,
            merged_definition_ids=[merged.definition_id], actor="admin",
            readiness_at_merge=snapshot["readiness_score"],
            strength_ok=True, negative_ok=True,
            first_merge_acknowledged=True,
            approval_id=approval["approval_id"],
            execution_mode="human-approved",
            expected_definition_revisions=expected_revisions,
            expected_evidence_digest=snapshot["evidence_digest"],
            expected_negative_digest=snapshot["negative_digest"],
            expected_binding_digest=snapshot["binding_digest"],
            expected_assessment_revision=snapshot["assessment_revision"],
            expected_policy_version=snapshot["policy_version"],
            expected_runtime_digest=snapshot["runtime_digest"],
        )


def test_repeated_scan_preserves_first_merge_acknowledgement(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)

    cand = _candidate(store, service)
    store.acknowledge_first_merge(cand["proposal_id"], actor="human")
    _candidate(store, service)  # rescan
    assert (
        store.get_proposal(cand["proposal_id"])["first_merge_acknowledged"] == 1
    ), "a rescan must not erase the acknowledgment"


def test_human_cannot_merge_polarity_conflict(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    pos = build_definition("必须运行测试")
    neg = build_definition("不要运行测试")
    store.upsert_definition(pos)
    store.upsert_definition(neg)
    _seed(store, pos, tag="pos")
    _seed(store, neg, tag="neg")

    # Polarity-conflicting rules are different semantic buckets, so the bounded
    # scan never proposes them together — which is the safe outcome.  The hard
    # gate must still block a merge if a proposal for the pair is forced.
    proposals = service.scan_and_propose()
    assert not any(
        {pos.definition_id, neg.definition_id} == set(p["definition_ids"])
        for p in proposals
    )
    pair = store.create_proposal(
        [pos.definition_id, neg.definition_id], 0.95,
        evidence=store.list_evidence(), definition_a=pos, definition_b=neg,
    )
    _approve(store, pair["proposal_id"])
    result = service.merge_proposal(pair["proposal_id"], actor="admin")
    assert result["ok"] is False
    assert result["conflict_type"] == "polarity"


def test_human_cannot_merge_parameter_conflict(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    pytest_def = build_definition("Python项目运行pytest")
    unittest_def = build_definition("Python项目运行unittest")
    store.upsert_definition(pytest_def)
    store.upsert_definition(unittest_def)
    _seed(store, pytest_def, tag="pytest")
    _seed(store, unittest_def, tag="unittest")

    proposals = service.scan_and_propose()
    assert not any(
        {pytest_def.definition_id, unittest_def.definition_id}
        == set(p["definition_ids"])
        for p in proposals
    )
    pair = store.create_proposal(
        [pytest_def.definition_id, unittest_def.definition_id], 0.95,
        evidence=store.list_evidence(),
        definition_a=pytest_def, definition_b=unittest_def,
    )
    _approve(store, pair["proposal_id"])
    result = service.merge_proposal(pair["proposal_id"], actor="admin")
    assert result["ok"] is False
    assert result["conflict_type"] == "parameter"


def test_store_rejects_proposal_definition_mismatch(tmp_path):
    store = RuleMergeStore(tmp_path)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    c = build_definition("使用 pnpm 安装依赖")
    for definition in (a, b, c):
        store.upsert_definition(definition)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _seed(store, c, tag="c")

    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.95,
        evidence=store.list_evidence(),
    )
    _approve(store, proposal["proposal_id"])
    # Merging a different pair than the evaluated one must be refused.
    with pytest.raises(RuntimeError, match="definition_mismatch"):
        store.execute_merge(
            proposal_id=proposal["proposal_id"],
            canonical_definition_id=c.definition_id,
            merged_definition_ids=[b.definition_id],
            actor="admin", strength_ok=True, negative_ok=True,
        )


def test_store_rejects_definition_revision_drift(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)

    cand = _candidate(store, service)
    _approve(store, cand["proposal_id"])
    # A definition edited after approval must block the merge.
    store.bump_definition_revision(a.definition_id)
    with pytest.raises(RuntimeError, match="revision_drift"):
        service.merge_proposal(cand["proposal_id"], actor="admin")


def test_actor_string_no_longer_grants_human_path(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)

    cand = _candidate(store, service)
    # actor="admin" on a plain candidate is not an approval: the automatic path
    # (soft gates) applies, and the fresh candidate is not auto-ready.
    result = service.merge_proposal(cand["proposal_id"], actor="admin")
    assert result["ok"] is False
    assert result["blocked_reason"] == "auto_merge_not_ready"


def test_approved_proposal_requires_valid_approval(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")
    _reps(store)

    cand = _candidate(store, service)
    expired = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    _approve(store, cand["proposal_id"], expires_at=expired)
    assert store.get_valid_approval(cand["proposal_id"]) is None
    result = service.merge_proposal(cand["proposal_id"], actor="admin")
    assert result["ok"] is False
    assert result["blocked_reason"] == "rule_merge_approval_required"
