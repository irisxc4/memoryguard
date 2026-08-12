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
            session_id=f"session-{agent}",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="team",
        project_ref=str((workspace / "project").resolve()),
        provider="codex",
        runtime_role="root",
    )


def _call(port: NativeV2RuntimePort, operation: str, payload: dict, context):
    return port.dispatch_mcp(
        operation,
        payload,
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )


def _data(result: dict) -> dict:
    assert result["ok"] is True, result
    return result["data"]


def test_auto_create_infers_trusted_agent_project_and_exposes_undo(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agent("a", "team")
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)

    result = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {
            "text": "必须先运行测试",
            "scope": {"target_type": "agent_project", "project_ref": str(tmp_path / "project")},
            "idempotency_key": "create-a",
        },
        context,
    ))

    assert result["definition_id"]
    assert result["binding_id"]
    assert result["undo_id"]
    store = RuleV2Store(tmp_path)
    definition = store.get_definition(result["definition_id"])
    binding = next(item for item in store.list_bindings(definition_id=result["definition_id"]))
    assert definition is not None and definition.status == "active"
    assert binding.target_type == "agent_project"
    assert binding.target_id == "a"
    assert binding.project_ref.endswith("/project") or binding.project_ref.endswith("\\project")

    undone = _data(_call(
        port,
        "memoryguard_rule_undo",
        {"undo_id": result["undo_id"], "idempotency_key": "undo-a"},
        context,
    ))
    assert undone["compensation"]["binding_status"] == "inactive"
    assert store.get_definition(result["definition_id"]).status == "inactive"


def test_agent_prefix_cannot_bypass_undo_owner(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    created = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "仅 codex-main 应遵循的规则", "idempotency_key": "create-owner"},
        _context(tmp_path, agent="codex-main"),
    ))
    denied = _call(
        port,
        "memoryguard_rule_undo",
        {"undo_id": created["undo_id"], "idempotency_key": "undo-prefix"},
        _context(tmp_path, agent="codex"),
    )
    assert denied["ok"] is False
    assert denied["code"] == "rule_undo_owner_mismatch"
    definition = RuleV2Store(tmp_path).get_definition(created["definition_id"])
    assert definition is not None and definition.status == "active"


def test_auto_scope_rejects_broad_or_other_agent_target(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    broad = _call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "全局必须遵守", "scope": {"target_type": "group", "target_id": "team"}},
        context,
    )
    other = _call(
        port,
        "memoryguard_rule_create_auto",
        {"text": "他人规则", "scope": {"target_type": "agent", "target_id": "b"}},
        context,
    )
    assert broad["code"] == "automatic_scope_expansion_denied"
    assert other["code"] == "other_agent_scope_denied"
    assert RuleV2Store(tmp_path).list_definitions() == []


def test_manual_broad_scope_requires_admin_and_is_audited(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    denied = _call(
        port,
        "memoryguard_rule_create_auto",
        {
            "text": "组规则",
            "scope": {"target_type": "group", "target_id": "team"},
            "manual": True,
            "idempotency_key": "manual-denied",
        },
        _context(tmp_path),
    )
    assert denied["code"] == "admin_scope_required"

    accepted = _data(_call(
        port,
        "memoryguard_rule_create_auto",
        {
            "text": "组规则",
            "scope": {"target_type": "group", "target_id": "team"},
            "manual": True,
            "idempotency_key": "manual-accepted",
        },
        _context(tmp_path, admin=True),
    ))
    store = RuleV2Store(tmp_path)
    binding = next(item for item in store.list_bindings(definition_id=accepted["definition_id"]))
    decision = store.get_decision(accepted["decision"]["decision_id"])
    assert binding.target_type == "group"
    assert decision is not None and '"manual":true' in decision["metadata_json"]
