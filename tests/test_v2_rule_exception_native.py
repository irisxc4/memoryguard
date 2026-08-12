from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.rule_lifecycle_native import NativeRuleLifecycleService
from memoryguard.rule_scope import canonical_project_ref


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="rule-exception-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-rule-exception",
        sensitivity="normal",
        policy_class="private",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def test_rule_exception_create_list_revoke_is_atomic_v2(tmp_path: Path) -> None:
    context = _context(tmp_path)
    lifecycle = NativeRuleLifecycleService(tmp_path)
    created_parent = lifecycle.create_auto(
        {"text": "Always run the release verification before publishing."},
        context=context,
    )
    parent_id = created_parent["definition_id"]

    port = _port(tmp_path)
    created = port.dispatch_gui(
        "create_child_exception",
        [
            parent_id,
            "For this project, skip release verification only for generated fixtures.",
            50,
            "project fixture exception",
            "group-a",
            True,
        ],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert created["ok"] is True, created
    exception_id = created.get("exception_id") or created.get("data", {}).get("exception_id")
    assert exception_id

    listed = port.dispatch_gui(
        "list_rule_exceptions",
        ["group-a", parent_id],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    rows = listed.get("exceptions") or listed.get("data", {}).get("exceptions") or []
    assert len(rows) == 1
    assert rows[0]["exception_id"] == exception_id

    with lifecycle.store.transaction() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_evidence_outbox WHERE source_kind='rule_exception'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_domain_outbox WHERE event_type='rule_exception_created'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_decisions WHERE action='rule_exception_create'"
        ).fetchone()[0] == 1

    revoked = port.dispatch_gui(
        "revoke_rule_exception",
        [exception_id, "group-a", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert revoked["ok"] is True, revoked

    after = port.dispatch_gui(
        "list_rule_exceptions",
        ["group-a", parent_id],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert (after.get("exceptions") or after.get("data", {}).get("exceptions") or []) == []
    with lifecycle.store.transaction() as conn:
        assert conn.execute(
            "SELECT active FROM rule_exceptions WHERE exception_id=?", (exception_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_domain_outbox WHERE event_type='rule_exception_revoked'"
        ).fetchone()[0] == 1


def test_rule_exception_scope_is_current_agent_project_only(tmp_path: Path) -> None:
    context = _context(tmp_path)
    lifecycle = NativeRuleLifecycleService(tmp_path)
    parent = lifecycle.create_auto(
        {"text": "Always keep deployment manifests validated."},
        context=context,
    )
    parent_id = parent["definition_id"]
    port = _port(tmp_path)
    result = port.dispatch_gui(
        "create_rule_exception",
        [parent_id, "Generated project fixtures may omit validation.", 10, "fixture", "group-a", True],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    child_id = result.get("child_definition_id") or result.get("data", {}).get("child_definition_id")
    child_bindings = lifecycle.store.list_bindings(definition_id=child_id, share_group_id="group-a", status="active")
    assert len(child_bindings) == 1
    assert child_bindings[0].target_type == "agent_project"
    assert child_bindings[0].target_id == "agent-a"
    assert child_bindings[0].project_ref == canonical_project_ref(tmp_path.resolve())

    parent_bindings = lifecycle.store.list_bindings(definition_id=parent_id, share_group_id="group-a", status="active")
    excludes = [item for item in parent_bindings if item.effect == "exclude"]
    assert len(excludes) == 1
    assert excludes[0].target_type == "agent_project"
    assert excludes[0].target_id == "agent-a"
    assert excludes[0].project_ref == canonical_project_ref(tmp_path.resolve())


def test_rule_exception_gui_entries_are_implemented(tmp_path: Path) -> None:
    entries = _port(tmp_path).coverage()["surfaces"]["gui"]["entries"]
    selected = [
        item for item in entries
        if item["name"] in {"create_child_exception", "create_rule_exception", "revoke_rule_exception"}
    ]
    assert len(selected) == 3
    assert all(item["status"] == "implemented" for item in selected), selected
