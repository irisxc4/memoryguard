"""P3 Rule Intelligence security tests: scope never expands (P3 §6-§7).

The most dangerous failure for a merge layer is permission creep: an automatic
merge that turns one Agent's rule into a system-wide broadcast.  These tests
prove the three defensive walls independently:

  1. Python: ``validate_binding_scope`` rejects auto/backfill broad audiences.
  2. Database: the ``rule_bindings`` CHECK constraint rejects the same input
     even when Python is bypassed.
  3. Behaviour: a merge never changes the binding audience identity set, and
     the system_auto_binding metric stays zero.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memoryguard.rule_binding import (
    AUTO_ALLOWED_TARGET_TYPES,
    build_binding,
)
from memoryguard.rule_definition import RuleDefinition, build_definition
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import _now_iso


def _def(text: str, definition_id: str = "") -> RuleDefinition:
    return build_definition(text, definition_id=definition_id)


def _store(tmp_path) -> RuleMergeStore:
    return RuleMergeStore(tmp_path)


# ---------------------------------------------------------------------------
# Wall 1: Python layer rejects auto broad scopes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_type", [
    "system", "group", "provider", "runtime_role",
])
def test_python_rejects_auto_broad_target_types(tmp_path, target_type):
    definition = _def("必须运行测试")
    with pytest.raises(ValueError):
        build_binding(
            definition.definition_id, share_group_id="g1",
            target_type=target_type, target_id="whatever",
            owner_agent_id="agent-a", created_by="auto",
        )


def test_python_rejects_backfill_system(tmp_path):
    definition = _def("必须运行测试")
    with pytest.raises(ValueError):
        build_binding(
            definition.definition_id, share_group_id="g1",
            target_type="system", owner_agent_id="agent-a",
            created_by="backfill",
        )


def test_python_allows_manual_system_for_human_governance(tmp_path):
    definition = _def("必须运行测试")
    binding = build_binding(
        definition.definition_id, share_group_id="g1",
        target_type="system", owner_agent_id="admin",
        created_by="manual", authorization="admin",
    )
    assert binding.target_type == "system"


def test_python_allows_auto_agent_and_agent_project(tmp_path):
    definition = _def("必须运行测试")
    for target_type in AUTO_ALLOWED_TARGET_TYPES:
        binding = build_binding(
            definition.definition_id, share_group_id="g1",
            target_type=target_type,
            target_id="agent-a" if target_type != "project" else "",
            project_ref="p1" if target_type == "agent_project" else "",
            owner_agent_id="agent-a", created_by="auto",
        )
        assert binding.target_type == target_type


# ---------------------------------------------------------------------------
# Wall 2: database CHECK constraint rejects broad auto scopes
# ---------------------------------------------------------------------------


def test_database_check_rejects_auto_system_binding(tmp_path):
    store = _store(tmp_path)
    definition = _def("必须运行测试")
    store.upsert_definition(definition)
    # Bypass the Python guard and hit the CHECK directly.
    conn = store._db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO rule_bindings (
                    binding_id, definition_id, share_group_id, target_type,
                    target_id, project_ref, provider, runtime_role, effect,
                    priority, owner_agent_id, created_by, authorization,
                    status, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'include', 0, ?, 'auto', '', 'active', 1, ?, ?)
                """,
                (
                    "binding-system", definition.definition_id, "g1", "system",
                    "", "", "", "", "agent-a",
                    _now_iso(), _now_iso(),
                ),
            )
    finally:
        conn.rollback()
        conn.close()


def test_database_check_allows_manual_system_binding(tmp_path):
    store = _store(tmp_path)
    definition = _def("必须运行测试")
    store.upsert_definition(definition)
    conn = store._db()
    try:
        conn.execute(
            """
            INSERT INTO rule_bindings (
                binding_id, definition_id, share_group_id, target_type,
                target_id, project_ref, provider, runtime_role, effect,
                priority, owner_agent_id, created_by, authorization,
                status, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'include', 0, ?, 'manual', 'admin', 'active', 1, ?, ?)
            """,
            (
                "binding-manual-system", definition.definition_id, "g1", "system",
                "", "", "", "", "admin",
                _now_iso(), _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    assert any(b.target_type == "system" for b in store.list_bindings())


# ---------------------------------------------------------------------------
# Wall 3: merge never creates system scope / never changes the identity set
# ---------------------------------------------------------------------------


def test_merge_never_creates_system_scope(tmp_path):
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
        for i in range(3):
            store.upsert_binding(build_binding(
                d.definition_id, share_group_id="g1", target_type="agent",
                target_id=f"agent-{i + 1}",
                owner_agent_id=f"agent-{i + 1}", created_by="backfill",
            ))
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    result = _svc_merge(store).merge_proposal(proposal["proposal_id"])
    assert result["ok"] is True
    metrics = store.metrics()
    assert metrics["system_auto_binding"] == 0
    assert metrics["auto_broad_binding"] == 0


def test_merge_preserves_binding_audience_identity_set(tmp_path):
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
        store.upsert_binding(build_binding(
            d.definition_id, share_group_id="g1", target_type="agent",
            target_id="agent-2", owner_agent_id="agent-2", created_by="backfill",
        ))
    before = {b.audience_identity() for b in store.list_bindings()}
    proposal = store.create_proposal(
        [a.definition_id, b.definition_id], 0.99,
        evidence=store.list_evidence(),
    )
    store.set_proposal_status(proposal["proposal_id"], "approved")
    result = _svc_merge(store).merge_proposal(proposal["proposal_id"])
    assert result["ok"] is True
    after = {b.audience_identity() for b in store.list_bindings()}
    assert before == after


def test_auto_merge_cannot_create_system_binding_metric_stays_zero(tmp_path):
    store = _store(tmp_path)
    # A single definition with a manual system binding is fine; the metric only
    # counts auto/backfill-created system bindings.
    definition = _def("必须运行测试")
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="g1", target_type="system",
        owner_agent_id="admin", created_by="manual", authorization="admin",
    ))
    assert store.metrics()["system_auto_binding"] == 0


def test_validate_binding_scope_rejects_project_without_project_ref(tmp_path):
    definition = _def("必须运行测试")
    with pytest.raises(ValueError):
        build_binding(
            definition.definition_id, share_group_id="g1",
            target_type="agent_project", target_id="agent-a",
            project_ref="", owner_agent_id="agent-a", created_by="manual",
        )


def _svc_merge(store: RuleMergeStore) -> RuleMergeService:
    return RuleMergeService(store)
