"""V2 rule split/exception atomicity and precise compensating undo tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.rule_lifecycle_native import NativeRuleLifecycleService


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, *, agent: str = "agent-a", admin: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=admin, strict_binding=True, allow_anon=False,
            session_id=f"split-{agent}", session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role="test",
    )


def _port(workspace: Path):
    return NativeV2RuntimePort(workspace, state_provider=_Manifest())


def _create(workspace: Path, key: str = "split-create"):
    return _port(workspace).dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "validate release fixtures", "idempotency_key": key},
        context=_context(workspace), generation=7, state="V2_ACTIVE",
    )


def _exception(workspace: Path, parent_id: str, *, text: str = "generated fixtures may omit validation"):
    return _port(workspace).dispatch_gui(
        "create_rule_exception", [parent_id, text, 10, "test exception", "group-a", True],
        context=_context(workspace), generation=7, state="V2_ACTIVE", mutation=True,
    )


def test_narrowing_aggregates_receipts_sessions_and_preserves_excludes(tmp_path):
    created = _create(tmp_path)
    store = RuleV2Store(tmp_path)
    definition_id = created["data"]["definition_id"]
    store.record_receipt({"receipt_id": "r-a", "definition_id": definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a", "session_id": "s-a"})
    store.record_receipt({"receipt_id": "r-b", "definition_id": definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a", "session_id": "s-b"})
    for receipt, key in (("r-a", "f-a"), ("r-b", "f-b")):
        result = _port(tmp_path).dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": receipt, "outcome": "followed", "idempotency_key": key}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
        assert result["ok"]
    assert len(store.list_feedback(definition_id=definition_id)) == 2
    assert len({row["session_id"] for row in (store.get_receipt("r-a"), store.get_receipt("r-b"))}) == 2


def test_narrow_undo_rejects_later_parent_edit(tmp_path):
    created = _create(tmp_path, key="narrow-parent")
    port = _port(tmp_path)
    first = port.dispatch_mcp("memoryguard_rule_undo", {"undo_id": created["data"]["undo_id"], "idempotency_key": "narrow-undo"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    conflict = port.dispatch_mcp("memoryguard_rule_undo", {"undo_id": created["data"]["undo_id"], "idempotency_key": "narrow-undo", "decision_id": "different"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert first["ok"] and conflict["ok"] is False


def test_followed_evidence_blocks_narrowing(tmp_path):
    created = _create(tmp_path, key="followed-narrow")
    store = RuleV2Store(tmp_path)
    store.record_receipt({"receipt_id": "followed-receipt", "definition_id": created["data"]["definition_id"], "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"})
    feedback = _port(tmp_path).dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": "followed-receipt", "outcome": "followed", "idempotency_key": "followed-feedback"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert feedback["ok"] and store.get_effective_feedback_projection("followed-receipt") is not None
    assert store.list_evidence_contributions(definition_id=created["data"]["definition_id"])[0]["active"] == 1


def test_exception_revoke_restores_parent_and_child_behavior(tmp_path):
    created = _create(tmp_path, key="exception-parent")
    result = _exception(tmp_path, created["data"]["definition_id"])
    assert result["ok"] is True, result
    exception_id = result.get("exception_id") or result.get("data", {}).get("exception_id")
    revoked = _port(tmp_path).dispatch_gui("revoke_rule_exception", [exception_id, "group-a", True], context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True)
    assert revoked["ok"]
    with sqlite3.connect(RuleV2Store(tmp_path).db_path) as conn:
        assert conn.execute("SELECT active FROM rule_exceptions WHERE exception_id=?", (exception_id,)).fetchone()[0] == 0


def test_exception_undo_rejects_later_parent_edit(tmp_path):
    created = _create(tmp_path, key="exception-undo")
    result = _exception(tmp_path, created["data"]["definition_id"])
    assert result["ok"]
    exception_id = result.get("exception_id") or result.get("data", {}).get("exception_id")
    denied = _port(tmp_path).dispatch_gui("revoke_rule_exception", ["unknown", "group-a", True], context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True)
    revoked = _port(tmp_path).dispatch_gui("revoke_rule_exception", [exception_id, "group-a", True], context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True)
    assert denied["ok"] is False and revoked["ok"] is True


def test_exception_revoke_keeps_preexisting_target_exclude(tmp_path):
    created = _create(tmp_path, key="preexisting-exclude")
    port = _port(tmp_path)
    store = RuleV2Store(tmp_path)
    include = store.list_bindings(definition_id=created["data"]["definition_id"])[0]
    store.upsert_binding({**include.to_dict(), "binding_id": "preexisting-exclude-binding", "effect": "exclude"})
    exception = _exception(tmp_path, created["data"]["definition_id"])
    assert exception["ok"], exception
    exception_id = exception.get("exception_id") or exception.get("data", {}).get("exception_id")
    assert port.dispatch_gui("revoke_rule_exception", [exception_id, "group-a", True], context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True)["ok"]
    assert any(item.effect == "exclude" for item in store.list_bindings(definition_id=created["data"]["definition_id"]))


def test_exception_revoke_keeps_exclude_while_sibling_active(tmp_path):
    created = _create(tmp_path, key="sibling-exclude")
    first = _exception(tmp_path, created["data"]["definition_id"], text="fixture one may omit validation")
    second = _exception(tmp_path, created["data"]["definition_id"], text="fixture two may omit validation")
    assert first["ok"] and second["ok"]
    first_id = first.get("exception_id") or first.get("data", {}).get("exception_id")
    revoked = _port(tmp_path).dispatch_gui("revoke_rule_exception", [first_id, "group-a", True], context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True)
    assert revoked["ok"]
    active = [row for row in sqlite3.connect(RuleV2Store(tmp_path).db_path).execute("SELECT active FROM rule_exceptions")]
    assert sum(int(row[0]) for row in active) == 1


def test_target_undo_requires_structured_inverse_and_never_group_snapshot(tmp_path):
    result = _port(tmp_path).dispatch_mcp("memoryguard_rule_undo", {"idempotency_key": "missing-target"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] is False and result["code"] == "rule_undo_target_required"
    raw = _port(tmp_path).dispatch_mcp("memoryguard_rule_create_auto", {"text": "unsafe bypass"}, context={"workspace_id": str(tmp_path), "agent_instance_id": "agent-a"}, generation=7, state="V2_ACTIVE")
    assert raw["ok"] is False
