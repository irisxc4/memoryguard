from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.rules.v2_store import RuleV2Store


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(
    workspace: Path,
    *,
    agent: str = "a",
    admin: bool = False,
    source: str = "transport",
):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}-{source}",
            session_source=source,
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="team",
        project_ref=str((workspace / "project").resolve()),
        provider="codex",
        runtime_role="root",
    )


def _setup(tmp_path: Path) -> tuple[NativeV2RuntimePort, RuleV2Store]:
    GroupControlService(tmp_path, write=True).bind_agent("a", "team")
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("始终先运行测试", kind="procedure"))
    store.upsert_binding({
        "binding_id": "binding-a",
        "definition_id": definition.definition_id,
        "share_group_id": "team",
        "target_type": "agent",
        "target_id": "a",
        "owner_agent_id": "a",
        "created_by": "admin",
        "status": "active",
    })
    store.record_receipt({
        "receipt_id": "receipt-1",
        "definition_id": definition.definition_id,
        "source_rule_id": "source-rule",
        "share_group_id": "team",
        "agent_instance_id": "a",
        "project_ref": str((tmp_path / "project").resolve()),
        "session_id": "session-a-transport",
        "task_hash": "task",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-10T00:00:00+00:00",
    })
    return NativeV2RuntimePort(tmp_path, state_provider=_Manifest()), store


def _feedback(port, context, **payload):
    return port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-1", "outcome": "not_applicable", "idempotency_key": "feedback-a", **payload},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )


def test_mcp_actor_cannot_upgrade_authority(tmp_path):
    port, store = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), actor="user", evidence="display text")
    assert result["ok"] is True, result
    feedback = store.list_feedback(receipt_id="receipt-1")[0]
    # Actor/producer are transport-derived; a caller-controlled display label
    # cannot upgrade an agent feedback to user authority.
    assert feedback["authority"] == 3
    assert '"producer":"agent"' in feedback["metadata_json"]


def test_feedback_owner_is_bound_to_receipt_agent(tmp_path):
    port, store = _setup(tmp_path)
    denied = _feedback(
        port,
        _context(tmp_path, agent="b"),
        outcome="followed",
        idempotency_key="feedback-owner-mismatch",
    )
    assert denied["ok"] is False
    assert denied["code"] == "rule_receipt_owner_mismatch"
    assert store.list_feedback(receipt_id="receipt-1") == []


def test_lower_authority_feedback_is_recorded_but_not_effective(tmp_path):
    port, store = _setup(tmp_path)
    first = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-1", "outcome": "followed", "idempotency_key": "feedback-user"},
        context=_context(tmp_path, admin=True),
        generation=7,
        state="V2_ACTIVE",
    )
    assert first["ok"] is True, first
    second = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": "receipt-1",
            "outcome": "not_applicable",
            "actor": "user",
            "idempotency_key": "feedback-agent",
        },
        context=_context(tmp_path),
        generation=7,
        state="V2_ACTIVE",
    )
    assert second["ok"] is True, second
    assert len(store.list_feedback(receipt_id="receipt-1")) == 2
    projection = store.get_effective_feedback_projection("receipt-1")
    assert projection is not None
    assert projection["outcome"] == "followed"
