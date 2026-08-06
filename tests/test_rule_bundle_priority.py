"""Req4: priority must not split bundles.

Three governed ``always`` records for the *same* audience with priorities
100/20/10 express one obligation and must collapse into a single canonical
overlay carrying the max priority (100).  ``bundle_scope_identity()`` excludes
priority, and ``build_bundles`` groups by the scope signature (which also
excludes priority) — these tests pin that behaviour at the heuristic-builder
level.
"""
from __future__ import annotations

from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.rule_reconciliation import build_bundles
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore

# The same Codex audience, three priorities.  schema_v3.RuleAssignment carries
# no provider field, so the "Codex" audience is scoped by target_type=agent +
# target_id (per ``_classify_scope``: a non-group agent audience folds into
# agent_overlay).
AUDIENCE = [{"target_type": "agent", "target_id": "codex"}]


def _always_record(memory_id: str, body: str, priority: int) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=body,
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        priority=priority,
        agent_instance_id="",
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )


def test_priority_does_not_split_bundle(tmp_path):
    store = RuleMergeStore(tmp_path)
    legacy = SharedMemoryStore(tmp_path, "g1")
    records = [
        _always_record("m1", "Codex 规则 A", 100),
        _always_record("m2", "Codex 规则 B", 20),
        _always_record("m3", "Codex 规则 C", 10),
    ]
    for record in records:
        legacy.append_record(record, assignments=AUDIENCE)

    plan = build_bundles(store, legacy, "g1", records)

    assert plan["kept_separate"] == []
    assert len(plan["bundles"]) == 1, (
        "same audience at different priorities must collapse into ONE bundle"
    )
    bundle = plan["bundles"][0]
    assert bundle.bundle_kind == "agent_overlay"
    assert sorted(bundle.source_memory_ids) == ["m1", "m2", "m3"]
    assert bundle.priority == 100


def test_same_priority_also_one_bundle(tmp_path):
    """Control: identical priorities collapse too (they already did)."""
    store = RuleMergeStore(tmp_path)
    legacy = SharedMemoryStore(tmp_path, "g1")
    records = [
        _always_record("m1", "Codex 规则 A", 50),
        _always_record("m2", "Codex 规则 B", 50),
    ]
    for record in records:
        legacy.append_record(record, assignments=AUDIENCE)

    plan = build_bundles(store, legacy, "g1", records)
    assert len(plan["bundles"]) == 1
    assert plan["bundles"][0].priority == 50


def test_different_audience_stays_separate(tmp_path):
    """Control: different audiences must NOT merge — priorities are irrelevant
    to that decision, but the bundle boundary is still the audience."""
    store = RuleMergeStore(tmp_path)
    legacy = SharedMemoryStore(tmp_path, "g1")
    a = _always_record("m1", "Codex 规则 A", 100)
    b = _always_record("m2", "Cursor 规则 B", 10)
    legacy.append_record(a, assignments=[{"target_type": "agent", "target_id": "codex"}])
    legacy.append_record(
        b, assignments=[{"target_type": "agent", "target_id": "cursor"}],
    )

    plan = build_bundles(store, legacy, "g1", [a, b])
    assert len(plan["bundles"]) == 2
    assert sorted(b.bundle_kind for b in plan["bundles"]) == ["agent_overlay", "agent_overlay"]
