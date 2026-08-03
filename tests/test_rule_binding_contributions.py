from __future__ import annotations

import pytest

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_merge_store import RuleMergeStore


def _binding(definition_id: str, *, priority: int = 0):
    return build_binding(
        definition_id,
        share_group_id="group-1",
        target_type="agent",
        target_id="agent-1",
        project_ref="project-1",
        provider="codex",
        runtime_role="developer",
        priority=priority,
        owner_agent_id="owner-1",
    )


def test_same_scope_different_priority_has_distinct_bindings(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("提交前必须运行测试", definition_id="def-1")
    store.upsert_definition(definition)

    low = _binding(definition.definition_id, priority=10)
    high = _binding(definition.definition_id, priority=20)
    assert low.binding_id != high.binding_id
    store.upsert_binding(low)
    store.upsert_binding(high)

    assert {item.binding_id for item in store.list_bindings(definition_id="def-1")} == {
        low.binding_id,
        high.binding_id,
    }


def test_shared_binding_stays_active_until_last_source_is_deactivated(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("提交前必须运行测试", definition_id="def-1")
    store.upsert_definition(definition)
    binding = _binding(definition.definition_id)

    store.replace_source_contributions("group-1", "source-a", [binding])
    store.replace_source_contributions("group-1", "source-b", [binding])
    store.deactivate_source_contributions("group-1", "source-a")
    assert store.list_bindings(definition_id="def-1")[0].status == "active"

    store.deactivate_source_contributions("group-1", "source-b")
    assert store.list_bindings(definition_id="def-1") == []
    assert store.list_bindings(definition_id="def-1", status="revoked")[0].status == "revoked"


def test_replacing_source_does_not_change_other_source(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("提交前必须运行测试", definition_id="def-1")
    store.upsert_definition(definition)
    first = _binding(definition.definition_id, priority=1)
    second = _binding(definition.definition_id, priority=2)

    store.replace_source_contributions("group-1", "source-a", [first])
    store.replace_source_contributions("group-1", "source-b", [first])
    store.replace_source_contributions("group-1", "source-a", [second])

    source_b = store.list_binding_contributions(source_memory_id="source-b", active=True)
    assert len(source_b) == 1
    assert source_b[0]["binding_id"] == first.binding_id
    assert store.list_bindings(definition_id="def-1")
    assert {item.binding_id for item in store.list_bindings(definition_id="def-1")} == {
        first.binding_id,
        second.binding_id,
    }


def test_rehome_preserves_contribution_multiset(tmp_path):
    store = RuleMergeStore(tmp_path)
    old = build_definition("提交前必须运行测试", definition_id="def-old")
    new = build_definition("提交代码前必须运行测试", definition_id="def-new")
    store.upsert_definition(old)
    store.upsert_definition(new)
    first = _binding(old.definition_id, priority=1)
    second = _binding(old.definition_id, priority=2)
    store.replace_source_contributions("group-1", "source-a", [first])
    store.replace_source_contributions("group-1", "source-b", [second])

    before = sorted(
        (row["source_memory_id"], row["legacy_assignment_hash"])
        for row in store.list_binding_contributions(active=True)
    )
    store.rehome_binding_contributions(old.definition_id, new.definition_id)
    after_rows = store.list_binding_contributions(active=True)
    after = sorted(
        (row["source_memory_id"], row["legacy_assignment_hash"])
        for row in after_rows
    )

    assert after == before
    assert len(after_rows) == 2
    assert {row["definition_id"] for row in after_rows} == {new.definition_id}


def test_public_binding_write_has_manual_contribution_and_real_zero_diff(tmp_path):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("direct binding", definition_id="def-1")
    store.upsert_definition(definition)
    binding = _binding(definition.definition_id)

    store.upsert_binding(binding)

    assert store.list_binding_contributions(
        binding_id=binding.binding_id, active=True,
    )
    assert store.metrics()["binding_contribution_diff"] == 0


def test_replace_source_contributions_rolls_back_on_materialize_failure(
    tmp_path, monkeypatch,
):
    store = RuleMergeStore(tmp_path)
    definition = build_definition("atomic binding", definition_id="def-1")
    store.upsert_definition(definition)
    binding = _binding(definition.definition_id)

    def fail(cls, conn, binding_ids):
        raise RuntimeError("injected materialize failure")

    monkeypatch.setattr(
        RuleMergeStore,
        "_materialize_affected_bindings_conn",
        classmethod(fail),
    )
    with pytest.raises(RuntimeError, match="injected materialize failure"):
        store.replace_source_contributions("group-1", "source-a", [binding])

    assert store.list_binding_contributions(active=True) == []
    assert store.list_bindings(status=None) == []
