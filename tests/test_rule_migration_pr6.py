"""Migration / backfill / evolution tests (PR6).

  * backfill only migrates governed (``always``) rules;
  * backfill never resurrects a merged/superseded lifecycle (a re-run routes
    the source's new bindings/evidence to the current canonical Definition);
  * wide legacy assignments (group/project/provider/runtime_role/system) are
    copied losslessly as ``migration``-sourced bindings carrying the legacy
    assignment hash + migration run id — they are not rejected like automatic
    broadening;
  * ``sync_rule`` resolves the source link to the canonical Definition;
  * ``evolve_strength`` migrates bindings atomically and rolls back on failure.
"""
from __future__ import annotations

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import RuleDefinition, normalize_rule_text
from memoryguard.rule_binding import build_binding
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


def _seed_record(store, memory_id, body, *, agent="agent-1", policy="always",
                 assignments=None):
    if assignments is None:
        assignments = [{"target_type": "agent", "target_id": agent}]
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy=policy,
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=assignments)


def _evidence_for_merge(store: RuleMergeStore, group_id: str,
                        legacy: SharedMemoryStore) -> None:
    for definition in store.list_definitions():
        for i in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=next(
                    mid for mid in ("m1", "m2")
                    if normalize_rule_text(
                        "提交代码前必须运行测试" if mid == "m1"
                        else "提交前必须执行测试",
                    ) == definition.canonical_text
                ),
                agent_instance_id=f"a{i}", project_ref=f"p{i}",
                session_id=f"s{i}", session_trusted=1,
                content=definition.canonical_text,
                observed_at=_now_iso(),
            ))
        store.upsert_agent_reputation(
            agent_id="agent-2", success_rate=0.98, sample_count=200,
        )
    for i in range(3):
        store.upsert_project_profile(project_ref=f"p{i}", production_level=1.0)


def _merge_synonym_pair(store: RuleMergeStore, service: RuleMergeService):
    candidates = service.scan_and_propose()
    cand = [p for p in candidates if p["status"] == "candidate"]
    assert cand, "synonym pair must be a merge candidate"
    pid = cand[0]["proposal_id"]
    context = AccessContext("test-admin", True, True, False)
    token = store.issue_merge_capability(pid, context)
    store.approve_proposal(
        pid, approved_by=context.principal,
        capability_token=token, access_context=context,
    )
    result = service.merge_proposal(pid, actor="admin")
    assert result["ok"] is True
    return result


def test_backfill_after_merge_does_not_resurrect_definition(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "提交代码前必须运行测试")
    _seed_record(legacy, "m2", "提交前必须执行测试")

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    _evidence_for_merge(intel, group, legacy)
    result = _merge_synonym_pair(intel, service)
    merged_id = result["merged_definition_ids"][0]
    canonical_id = result["canonical_definition_id"]
    assert intel.get_definition(merged_id).status == "merged"

    # Re-run backfill: the merged rule must NOT be resurrected.
    service.backfill_group(legacy, group)
    assert intel.get_definition(merged_id).status == "merged"
    # New evidence from the merged source routes to the canonical.
    assert any(
        e.source_rule_id == "m2"
        for e in intel.list_evidence(definition_id=canonical_id)
    )


def test_v1_strength_collision_splits_existing_evidence_by_source(tmp_path):
    group = "g1"
    must_body = "必须运行测试"
    should_body = "应该运行测试"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "must", must_body)
    _seed_record(legacy, "should", should_body)

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    must_definition = service._definition_from_record(legacy.get_record("must"))
    should_definition = service._definition_from_record(legacy.get_record("should"))
    legacy_id = stable_hash(
        "rule-definition", "canonical", normalize_rule_text(must_body),
    )
    intel.upsert_definition(RuleDefinition.from_dict({
        **must_definition.to_dict(),
        "definition_id": legacy_id,
        "rule_strength": "observation",
    }))
    for source_id, body in (("must", must_body), ("should", should_body)):
        intel.upsert_evidence(build_evidence(
            definition_id=legacy_id,
            source_rule_id=source_id,
            agent_instance_id=source_id,
            project_ref=f"p-{source_id}",
            session_id=f"s-{source_id}",
            session_trusted=True,
            content=body,
            observed_at=_now_iso(),
        ))

    service.backfill_group(legacy, group)

    assert {
        item.source_rule_id: item.definition_id
        for item in intel.list_evidence()
    } == {
        "must": must_definition.definition_id,
        "should": should_definition.definition_id,
    }
    assert not intel.list_evidence(definition_id=legacy_id)
    assert intel.get_definition(legacy_id).status == "alias"


def test_v1_collision_routes_binding_runtime_and_source_links_per_source(tmp_path):
    group = "g1"
    must_body = "蹇呴』杩愯娴嬭瘯"
    should_body = "搴旇杩愯娴嬭瘯"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "must", must_body, agent="agent-must")
    _seed_record(legacy, "should", should_body, agent="agent-should")

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service._legacy_definition_id = lambda record: "legacy-strength-collision"
    must_definition = service._definition_from_record(legacy.get_record("must"))
    should_definition = service._definition_from_record(legacy.get_record("should"))
    legacy_id = "legacy-strength-collision"
    intel.upsert_definition(RuleDefinition.from_dict({
        **must_definition.to_dict(),
        "definition_id": legacy_id,
        "rule_strength": "observation",
    }))
    old_must = build_binding(
        legacy_id, share_group_id=group, target_type="agent",
        target_id="agent-must", owner_agent_id="agent-must",
        created_by="backfill",
    )
    old_should = build_binding(
        legacy_id, share_group_id=group, target_type="agent",
        target_id="agent-should", owner_agent_id="agent-should",
        created_by="backfill",
    )
    intel.replace_source_contributions(group, "must", [old_must])
    intel.replace_source_contributions(group, "should", [old_should])
    for source_id, body in (("must", must_body), ("should", should_body)):
        intel.upsert_evidence(build_evidence(
            definition_id=legacy_id, source_rule_id=source_id,
            agent_instance_id=source_id, project_ref=f"p-{source_id}",
            session_id=f"s-{source_id}", session_trusted=True, content=body,
            receipt_id=f"receipt-{source_id}",
            feedback_id=f"fb-{source_id}",
        ))
        intel.upsert_runtime_feedback(
            feedback_id=f"fb-{source_id}", definition_id=legacy_id,
            receipt_id=f"receipt-{source_id}", outcome="followed",
            agent_instance_id=source_id, project_ref=f"p-{source_id}",
            session_id=f"s-{source_id}", session_trusted=1,
        )
        intel.upsert_effective_feedback_projection(
            receipt_id=f"receipt-{source_id}",
            effective_feedback_id=f"fb-{source_id}",
            definition_id=legacy_id, outcome="followed",
        )

    service.backfill_group(legacy, group)

    assert {
        row["source_memory_id"]: row["definition_id"]
        for row in intel.list_binding_contributions(active=True)
        if row["source_memory_id"] in {"must", "should"}
    } == {
        "must": must_definition.definition_id,
        "should": should_definition.definition_id,
    }
    assert {
        item.source_rule_id: item.definition_id
        for item in intel.list_evidence()
        if item.source_rule_id in {"must", "should"}
    } == {
        "must": must_definition.definition_id,
        "should": should_definition.definition_id,
    }
    with intel._db() as conn:
        runtime = {
            row["feedback_id"]: row["definition_id"]
            for row in conn.execute(
                "SELECT feedback_id, definition_id FROM rule_runtime_feedback "
                "WHERE feedback_id IN ('fb-must', 'fb-should')"
            ).fetchall()
        }
    assert runtime == {
        "fb-must": must_definition.definition_id,
        "fb-should": should_definition.definition_id,
    }
    assert intel.get_source_link(group, "must")["canonical_definition_id"] == (
        must_definition.definition_id
    )
    assert intel.get_source_link(group, "should")["canonical_definition_id"] == (
        should_definition.definition_id
    )


def test_v1_backfill_fault_rolls_back_definitions_bindings_evidence_and_links(
    tmp_path, monkeypatch,
):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "run tests before submit")
    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)

    def fail_source_link(**_kwargs):
        raise RuntimeError("source-link fault")

    monkeypatch.setattr(intel, "upsert_source_link", fail_source_link)
    with pytest.raises(RuntimeError, match="source-link fault"):
        service.backfill_group(legacy, group)

    assert intel.list_definitions() == []
    assert intel.list_bindings(status=None) == []
    assert intel.list_binding_contributions() == []
    assert intel.list_evidence() == []
    assert intel.get_source_link(group, "m1") is None


def test_sync_after_merge_targets_canonical_definition(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "提交代码前必须运行测试")
    _seed_record(legacy, "m2", "提交前必须执行测试")

    intel = RuleMergeStore(tmp_path)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)
    _evidence_for_merge(intel, group, legacy)
    result = _merge_synonym_pair(intel, service)
    canonical_id = result["canonical_definition_id"]

    # A rule re-created for the merged source must write to the canonical.
    record = legacy.get_record("m2")
    out = service.sync_rule(
        legacy, group, record,
        receipts=legacy.list_rule_match_receipts(memory_id="m2") or [],
    )
    assert out["definition_id"] == canonical_id
    assert intel.get_definition(canonical_id).status == "active"


def test_backfill_preserves_manual_system_and_group_bindings(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(
        legacy, "m1", "提交代码前必须运行测试",
        assignments=[{"target_type": "group", "target_id": group}],
    )
    intel = RuleMergeStore(tmp_path)
    # Previously backfill raised on a wide (non agent/agent_project) scope; now
    # it is a lossless, audited migration copy.
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group)

    definition = intel.list_definitions()[0]
    bindings = intel.list_bindings(definition_id=definition.definition_id)
    assert bindings
    group_binding = next(
        b for b in bindings if b.target_type == "group"
    )
    assert group_binding.created_by == "migration"
    assert "legacy_assignment_hash" in (group_binding.authorization or "")
    assert "migration_run_id" in (group_binding.authorization or "")


def test_backfill_skips_non_rule_relevant_memories(tmp_path):
    group = "g1"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "m1", "提交代码前必须运行测试", policy="always")
    # A relevant recall memory carries no audience assignment (assignments
    # require injection_policy=always), so backfill must skip it entirely.
    _seed_record(legacy, "m2", "用户长期偏好：输出简洁", policy="relevant",
                 assignments=[])

    intel = RuleMergeStore(tmp_path)
    RuleMergeService(intel).backfill_group(legacy, group)
    # Only the governed (always) rule is backfilled.
    assert intel.count_definitions() == 1


def test_strength_evolution_moves_bindings_atomically(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    definition = service._definition_from_record(
        SharedMemoryRecord(
            memory_id="m1", body="提交代码前必须运行测试",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE, injection_policy="always",
            priority=10, agent_instance_id="agent-1",
            created_at=_now_iso(), updated_at=_now_iso(),
        ),
    )
    store.upsert_definition(definition)
    from memoryguard.rule_binding import build_binding
    store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="g1", target_type="agent",
        target_id="agent-1", owner_agent_id="agent-1", created_by="backfill",
    ))

    result = service.evolve_strength(
        definition.definition_id, "suggestion", reason="治理放宽", actor="admin",
    )
    new_id = result["new_definition_id"]
    # Old definition superseded; bindings migrated onto the new definition.
    assert store.get_definition(definition.definition_id).status == "superseded"
    assert all(
        b.definition_id == new_id for b in store.list_bindings()
    )
    assert store.count_definition_versions() == 1


def test_strength_evolution_failure_rolls_back_all_state(tmp_path):
    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)
    definition = build_definition_copy = None
    from memoryguard.rule_definition import build_definition
    definition = build_definition("提交代码前必须运行测试")
    store.upsert_definition(definition)
    # A non-active definition cannot evolve: the atomic transaction rolls back.
    store.set_definition_status(definition.definition_id, "merged",
                                superseded_by="other")
    before_defs = store.count_definitions()
    before_versions = store.count_definition_versions()
    with pytest.raises(ValueError):
        service.evolve_strength(definition.definition_id, "suggestion")
    assert store.count_definitions() == before_defs
    assert store.count_definition_versions() == before_versions
