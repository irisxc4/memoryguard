"""Formal V2 lifecycle model, receipt, exception, and rollback coverage."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, *, agent: str = "agent-a", admin: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=admin, strict_binding=True,
            allow_anon=False, session_id=f"lifecycle-{agent}",
            session_source="transport", session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role="test",
    )


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _create(workspace: Path, *, text: str = "record release provenance", key: str = "create"):
    return NativeV2RuntimePort(workspace, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": text, "idempotency_key": key},
        context=_context(workspace), generation=7, state="V2_ACTIVE",
    )


def test_rule_decision_round_trip_and_legacy_decision_compatibility(tmp_path):
    store = RuleV2Store(tmp_path)
    decision_id = store.record_decision({
        "decision_id": "decision-v2", "rule_id": "rule-v2", "action": "rule_create_auto",
        "reason": "model round trip", "metadata_json": json.dumps({"schema": "v2"}),
    })
    decision = store.get_decision(decision_id)
    assert decision["decision_id"] == "decision-v2"
    assert json.loads(decision["metadata_json"])["schema"] == "v2"


def test_scope_stats_are_cumulative_and_filterable_by_agent_project_rule(tmp_path):
    first = _create(tmp_path, key="scope-a")
    second = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "keep deployment notes", "idempotency_key": "scope-b"},
        context=_context(tmp_path, agent="agent-b"), generation=7, state="V2_ACTIVE",
    )
    assert first["ok"] and second["ok"]
    stats = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_rule_scope_stats", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert stats["ok"] and stats["data"]["active"] == 2


def test_exception_relation_rollback_is_reversible_metadata(tmp_path):
    store = RuleV2Store(tmp_path)
    parent = store.upsert_definition(build_definition("preserve release notes", rule_strength="must"))
    child = store.upsert_definition(build_definition("allow emergency release", rule_strength="should"))
    store.upsert_exception({
        "exception_id": "exception-v2", "parent_rule_id": parent.definition_id,
        "child_exception_id": child.definition_id, "source_ref": "ticket-1", "status": "active",
        "reason": "incident", "active": 1,
    })
    row = _rows(store, "rule_exceptions")[0]
    assert row["active"] == 1 and row["parent_rule_id"] == parent.definition_id
    with store.transaction() as conn:
        conn.execute("UPDATE rule_exceptions SET active=0 WHERE exception_id=?", (row["exception_id"],))
    assert _rows(store, "rule_exceptions")[0]["active"] == 0


def test_automatic_assignment_fails_closed_without_cross_scope_expansion(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    other = port.dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "foreign scope", "scope": {"target_type": "agent", "target_id": "agent-b"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    group = port.dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "broad scope", "scope": {"target_type": "group", "target_id": "group-a"}},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert other["code"] == "other_agent_scope_denied"
    assert group["code"] == "automatic_scope_expansion_denied"


def test_lifecycle_tables_survive_snapshot_rollback_and_clear(tmp_path):
    created = _create(tmp_path, key="snapshot-create")
    assert created["ok"]
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    undone = port.dispatch_mcp(
        "memoryguard_rule_undo", {"undo_id": created["data"]["undo_id"], "idempotency_key": "snapshot-undo"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert undone["ok"]
    store = RuleV2Store(tmp_path)
    assert store.get_definition(created["data"]["definition_id"]).status == "inactive"
    assert len(_rows(store, "rule_decisions")) == 2


def test_old_database_gets_lifecycle_schema_migration(tmp_path):
    store = RuleV2Store(tmp_path)
    path = store.db_path
    reopened = RuleV2Store(tmp_path)
    assert reopened.db_path == path
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rule_decisions", "rule_receipt_refs", "rule_feedback_refs"} <= tables


def test_c971_feedback_stream_migration_preserves_rows_and_allows_append(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("preserve feedback stream", kind="procedure"))
    store.record_receipt({"receipt_id": "r1", "definition_id": definition.definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a"})
    store.record_feedback({"feedback_id": "f1", "receipt_id": "r1", "definition_id": definition.definition_id, "outcome": "followed", "authority": 3})
    store.record_feedback({"feedback_id": "f2", "receipt_id": "r1", "definition_id": definition.definition_id, "outcome": "corrected", "authority": 3})
    assert [row["feedback_id"] for row in store.list_feedback(receipt_id="r1")] == ["f1", "f2"]


def test_first_receipt_insert_persists_context_fields(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("receipt context rule", kind="procedure"))
    store.record_receipt({
        "receipt_id": "receipt-context", "definition_id": definition.definition_id,
        "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a",
        "provider": "codex", "runtime_role": "test", "session_id": "session-a",
    })
    receipt = store.get_receipt("receipt-context")
    assert receipt["agent_instance_id"] == "agent-a" and receipt["session_id"] == "session-a"


def test_effective_feedback_evidence_groups_one_event_per_receipt(tmp_path):
    created = _create(tmp_path, key="receipt-effective")
    store = RuleV2Store(tmp_path)
    definition_id = created["data"]["definition_id"]
    store.record_receipt({"receipt_id": "receipt-effective", "definition_id": definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"})
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    result = port.dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": "receipt-effective", "outcome": "followed", "idempotency_key": "effective-feedback"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] and store.get_effective_feedback_projection("receipt-effective")["receipt_id"] == "receipt-effective"
    assert len(store.list_evidence_contributions(definition_id=definition_id)) == 1


def test_rule_split_is_atomic_and_revert_restores_behavior(tmp_path):
    created = _create(tmp_path, text="split parent rule", key="split-create")
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    undone = port.dispatch_mcp("memoryguard_rule_undo", {"undo_id": created["data"]["undo_id"], "idempotency_key": "split-undo"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert undone["ok"]
    assert RuleV2Store(tmp_path).get_definition(created["data"]["definition_id"]).status == "inactive"


def test_rule_split_failure_rolls_back_child_parent_exception_and_decision(tmp_path, monkeypatch):
    store = RuleV2Store(tmp_path)
    parent = store.upsert_definition(build_definition("parent rule", rule_strength="must"))
    before = len(_rows(store, "rule_definitions"))
    with pytest.raises(RuntimeError):
        with store.transaction():
            child = store.upsert_definition(build_definition("child exception", rule_strength="should"))
            store.upsert_exception({"exception_id": "failed-exception", "parent_rule_id": parent.definition_id, "child_exception_id": child.definition_id, "source_ref": "failed"})
            raise RuntimeError("simulated split failure")
    assert len(_rows(store, "rule_definitions")) == before
    assert _rows(store, "rule_exceptions") == []


def test_bootstrap_receipts_separate_sessions_and_persist_context(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("receipt session rule", kind="procedure"))
    for receipt_id, session in (("receipt-a", "session-a"), ("receipt-b", "session-b")):
        store.record_receipt({"receipt_id": receipt_id, "definition_id": definition.definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a", "session_id": session})
    rows = {row["receipt_id"]: row["session_id"] for row in _rows(store, "rule_receipt_refs")}
    assert rows == {"receipt-a": "session-a", "receipt-b": "session-b"}
