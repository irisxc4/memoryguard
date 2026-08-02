"""Definition identity v2 tests (PR2).

The pre-v2 definition id only covered the canonical surface wording, so
``提交代码前必须运行测试`` and ``提交代码前应该运行测试`` collapsed onto one
id and the later ingest silently overwrote the earlier strength — the merge
layer never saw the MUST/SHOULD pair and could not report the strength
conflict.  v2 includes polarity / strength / parameters / kind in the id, and
pre-v2 definitions are migrated atomically (alias row + evidence moved +
bindings recreated + scope verified).  Orphans with an unrecoverable body are
marked ``unknown`` strength and excluded from automatic merging.
"""
from __future__ import annotations

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import (
    STRENGTH_UNKNOWN,
    build_definition,
    normalize_rule_text,
)
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _seed(store: RuleMergeStore, definition, *, tag: str, count: int = 3):
    for i in range(count):
        store.upsert_evidence(build_evidence(
            definition_id=definition.definition_id,
            source_rule_id=f"{tag}-{i}", agent_instance_id=f"a{i}",
            project_ref=f"p{i}", session_id=f"s{i}",
            content=definition.canonical_text, observed_at=_now_iso(),
        ))


def _record(memory_id: str, body: str, *, agent: str = "agent-1"):
    return SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    )


def test_must_and_should_never_share_definition_id():
    must = build_definition("提交代码前必须运行测试")
    should = build_definition("提交代码前应该运行测试")
    assert must.definition_id != should.definition_id
    # identical wording still collapses onto one id.
    assert build_definition("提交代码前必须运行测试").definition_id == must.definition_id


def test_english_must_not_has_negative_polarity():
    d = build_definition("must not commit untested code")
    assert d.polarity == "negative"
    assert d.rule_strength == "must"
    assert build_definition("do not run tests").polarity == "negative"
    assert build_definition("never push without review").polarity == "negative"
    # positive control
    assert build_definition("must run tests").polarity == "positive"


def test_strength_conflict_surfaces_after_v2_identity(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    must = build_definition("提交代码前必须运行测试")
    should = build_definition("提交代码前应该运行测试")
    store.upsert_definition(must)
    store.upsert_definition(should)
    _seed(store, must, tag="m")
    _seed(store, should, tag="r")

    proposals = service.scan_and_propose()
    # The MUST/SHOULD pair must surface as a governance conflict, not silently
    # collapse at ingest, and must never become an auto-merge candidate.
    assert any(p["conflict_type"] == "strength" for p in proposals)
    assert all(p["status"] != "candidate" for p in proposals)


def test_backfill_migrates_v1_definition_to_v2(tmp_path):
    legacy = SharedMemoryStore(tmp_path, "g1")
    legacy.append_record(
        _record("m1", "提交代码前必须运行测试"),
        assignments=[{"target_type": "agent", "target_id": "agent-1"}],
    )
    intel = RuleMergeStore(tmp_path)
    # Pre-seed a v1-format definition for the same body (a store built before
    # the v2 identity change) with a binding and evidence of its own.
    v1_id = stable_hash(
        "rule-definition", "canonical",
        normalize_rule_text("提交代码前必须运行测试"),
    )
    v1 = build_definition("提交代码前必须运行测试", definition_id=v1_id)
    intel.upsert_definition(v1)
    intel.upsert_binding(build_binding(
        v1_id, share_group_id="g1", target_type="agent", target_id="agent-1",
        created_by="backfill", authorization="backfill",
    ))
    intel.upsert_evidence(build_evidence(
        definition_id=v1_id, source_rule_id="m1", agent_instance_id="a0",
        project_ref="p0", session_id="s0", content="提交代码前必须运行测试",
    ))

    service = RuleMergeService(intel)
    service.backfill_group(legacy, "g1")

    active = intel.list_definitions(status="active")
    assert len(active) == 1
    v2 = active[0]
    assert v2.definition_id != v1_id
    # v1 is now an alias pointing at the v2 id, recorded in the alias table.
    assert intel.get_definition(v1_id).status == "alias"
    assert intel.get_definition(v1_id).superseded_by == v2.definition_id
    assert intel.get_definition_alias(v1_id)["new_definition_id"] == v2.definition_id
    # Evidence migrated onto the v2 definition; bindings recreated under v2.
    assert any(
        e.definition_id == v2.definition_id
        for e in intel.list_evidence(v2.definition_id)
    )
    assert all(b.definition_id == v2.definition_id for b in intel.list_bindings())
    # The source link records the v1 -> v2 resolution.
    link = intel.get_source_link("g1", "m1")
    assert link is not None
    assert link["original_definition_id"] == v1_id
    assert link["canonical_definition_id"] == v2.definition_id


def test_orphan_v1_definition_marked_unknown(tmp_path):
    intel = RuleMergeStore(tmp_path)
    v1_id = stable_hash(
        "rule-definition", "canonical",
        normalize_rule_text("提交代码前必须运行测试"),
    )
    v1 = build_definition("提交代码前必须运行测试", definition_id=v1_id)
    intel.upsert_definition(v1)

    RuleMergeService(intel).backfill_group(
        SharedMemoryStore(tmp_path, "empty"), "empty",
    )
    # No legacy source can recover this body -> strength unknown, never merged.
    assert intel.get_definition(v1_id).rule_strength == STRENGTH_UNKNOWN


def test_unknown_strength_never_auto_merges(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    a.rule_strength = STRENGTH_UNKNOWN
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")

    proposals = service.scan_and_propose()
    assert all(p["status"] != "candidate" for p in proposals)


def test_human_cannot_merge_unknown_strength(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    a = build_definition("提交代码前必须运行测试")
    b = build_definition("提交前必须执行测试")
    a.rule_strength = STRENGTH_UNKNOWN
    store.upsert_definition(a)
    store.upsert_definition(b)
    _seed(store, a, tag="a")
    _seed(store, b, tag="b")

    proposals = service.scan_and_propose()
    conflicted = [p for p in proposals if p["conflict_type"] == "strength"]
    assert conflicted, "unknown-strength pair must surface as a conflict"
    result = service.merge_proposal(conflicted[0]["proposal_id"], actor="admin")
    assert result["ok"] is False
    assert result["conflict_type"] == "strength"
