from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.rules.v2_store import RuleV2Store


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, *, agent: str = "a", admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"lifecycle-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="team",
        project_ref=str((workspace / "project").resolve()),
        provider="codex",
        runtime_role="root",
    )


def _port(tmp_path: Path):
    GroupControlService(tmp_path, write=True).bind_agent("a", "team")
    return NativeV2RuntimePort(tmp_path, state_provider=_Manifest())


def _data(response: dict) -> dict:
    assert response["ok"] is True, response
    return response["data"]


def _call(port, name: str, payload: dict, context):
    return port.dispatch_mcp(
        name,
        payload,
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )


def test_mcp_auto_rule_tool_and_scope_stats(tmp_path):
    port = _port(tmp_path)
    context = _context(tmp_path)
    entries = {item["name"]: item for item in port.coverage()["surfaces"]["mcp"]["entries"]}
    assert entries["memoryguard_rule_create_auto"]["status"] == "implemented"
    created = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "不要跳过测试", "idempotency_key": "create"},
        context,
    ))
    stats = _data(_call(port, "memoryguard_rule_scope_stats", {}, context))
    assert stats["active"] == 1
    assert stats["by_target_type"] == {"agent": 1}
    definition = RuleV2Store(tmp_path).get_definition(created["definition_id"])
    assert definition is not None and definition.status == "active"


def test_feedback_is_audited_and_corrected_does_not_change_binding(tmp_path):
    port = _port(tmp_path)
    context = _context(tmp_path)
    created = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "始终先运行测试", "idempotency_key": "parent"},
        context,
    ))
    store = RuleV2Store(tmp_path)
    store.record_receipt({
        "receipt_id": "receipt-corrected",
        "definition_id": created["definition_id"],
        "source_rule_id": created["definition_id"],
        "share_group_id": "team",
        "agent_instance_id": "a",
        "project_ref": str((tmp_path / "project").resolve()),
        "session_id": "lifecycle-a",
        "task_hash": "t",
        "selection_digest": "s",
        "metadata_json": "{}",
        "created_at": "2026-08-10T00:00:00+00:00",
    })
    before = [item.to_dict() for item in store.list_bindings(definition_id=created["definition_id"])]
    feedback = _data(_call(
        port,
        "memoryguard_rule_feedback",
        {
            "receipt_id": "receipt-corrected",
            "outcome": "corrected",
            "evidence": "仍需先运行测试",
            "idempotency_key": "corrected",
        },
        context,
    ))
    assert feedback["outcome"] == "corrected"
    assert [item.to_dict() for item in store.list_bindings(definition_id=created["definition_id"])] == before


def test_atomic_rule_create_dedup_undo_preserves_original_rule(tmp_path):
    port = _port(tmp_path)
    context = _context(tmp_path)
    first = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "始终先运行定向测试", "idempotency_key": "same"},
        context,
    ))
    replay = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "始终先运行定向测试", "idempotency_key": "same"},
        context,
    ))
    assert replay["idempotent_replay"] is True
    assert replay["decision"]["decision_id"] == first["decision"]["decision_id"]
    undone = _data(_call(
        port,
        "memoryguard_rule_undo",
        {"undo_id": first["undo_id"], "idempotency_key": "undo"},
        context,
    ))
    assert undone["compensation"]["binding_status"] == "inactive"
    store = RuleV2Store(tmp_path)
    assert store.get_definition(first["definition_id"]).status == "inactive"
    assert len(store.list_definitions()) == 1


def test_non_created_lifecycle_decisions_are_atomic_and_reversible(tmp_path):
    port = _port(tmp_path)
    context = _context(tmp_path)
    missing = _call(
        port,
        "memoryguard_rule_feedback",
        {"receipt_id": "missing", "outcome": "followed", "idempotency_key": "missing"},
        context,
    )
    assert missing["ok"] is False
    assert missing["code"] == "rule_receipt_not_found"
    store = RuleV2Store(tmp_path)
    assert store.list_feedback() == []
