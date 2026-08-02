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

from memoryguard.rule_definition import normalize_rule_text
from memoryguard.rule_evidence import build_evidence
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
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
                session_id=f"s{i}", content=definition.canonical_text,
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
    store.approve_proposal(pid, approved_by="admin")
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
