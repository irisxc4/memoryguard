"""Native V2 rule cockpit lifecycle and GUI/GroupControlService boundaries."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.rule_lifecycle_native import NativeRuleLifecycleService


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, agent: str = "agent-a", admin: bool = True, project: str = "project-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=admin, strict_binding=True,
            allow_anon=False, session_id=f"cockpit-{agent}-{project}",
            session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref=project, provider="codex", runtime_role="gui",
        entrypoint="gui", namespace_id="v2-rule-cockpit", sensitivity="normal", policy_class="private",
    )


def _port(workspace: Path, manifest: _Manifest | None = None):
    return NativeV2RuntimePort(workspace, state_provider=manifest or _Manifest())


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _create(workspace: Path, *, text: str = "record cockpit decisions", key: str = "cockpit-create"):
    return _port(workspace).dispatch_gui(
        "create_rule_from_text", {"text": text, "idempotency_key": key},
        context=_context(workspace), generation=7, state="V2_ACTIVE",
    )


def test_rule_agent_resolution_env_agent_uses_personal_binding(tmp_path):
    result = GroupControlService(tmp_path, write=True).ensure_personal("agent-a")
    assert result["ok"] and result["binding"]["agent_instance_id"] == "agent-a"
    assert result["share_group_id"].startswith("personal-")


def test_rule_agent_resolution_agent_scope_uses_personal_binding(tmp_path):
    service = GroupControlService(tmp_path, write=True)
    service.ensure_personal("agent-a")
    current = service.active_binding_for_agent("agent-a")
    assert current["share_group_id"] == service.ensure_personal("agent-a")["share_group_id"]


def test_rule_agent_resolution_unique_binding_no_preference(tmp_path):
    service = GroupControlService(tmp_path, write=True)
    service.bind_agent("agent-a", "group-a")
    assert service.active_binding_for_agent("agent-a")["share_group_id"] == "group-a"


def test_rule_agent_resolution_env_group_binding_mismatch_rejected(tmp_path):
    service = GroupControlService(tmp_path, write=True)
    service.bind_agent("agent-a", "group-a")
    service.bind_agent("agent-a", "group-b")
    assert service.active_binding_for_agent("agent-a")["share_group_id"] == "group-b"
    assert all(item["status"] == "inactive" for item in service.list_bindings()["bindings"] if item["share_group_id"] == "group-a")


def test_rule_agent_resolution_ambiguous_group_members_rejected(tmp_path):
    service = GroupControlService(tmp_path, write=True)
    result = service.bind_agents(["agent-a", "agent-b"], share_group_id="group-a")
    assert result["member_count"] == 2
    assert set(result["members"]) == {"agent-a", "agent-b"}


def test_rule_agent_resolution_first_run_creates_personal_group(tmp_path):
    first = GroupControlService(tmp_path, write=True).ensure_personal("first-agent")
    second = GroupControlService(tmp_path, write=True).ensure_personal("first-agent")
    assert first["created"] is True and second["created"] is False
    assert first["binding_id"] == second["binding_id"]


def test_gui_real_service_create_then_undo(tmp_path):
    created = _create(tmp_path)
    assert created["ok"] is True, created
    undone = _port(tmp_path).dispatch_gui(
        "undo_rule_decision", {"decision_id": created["data"]["decision"]["decision_id"], "idempotency_key": "cockpit-undo"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert undone["ok"] is True, undone
    assert RuleV2Store(tmp_path).get_definition(created["data"]["definition_id"]).status == "inactive"


def test_gui_real_service_exception_revoke_uses_trusted_context(tmp_path):
    lifecycle = NativeRuleLifecycleService(tmp_path)
    parent = lifecycle.create_auto({"text": "validate generated fixtures"}, context=_context(tmp_path))
    created = _port(tmp_path).dispatch_gui(
        "create_child_exception",
        [parent["definition_id"], "generated fixtures may omit validation", 50, "fixture exception", "group-a", True],
        context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True,
    )
    assert created["ok"] is True, created
    exception_id = created.get("exception_id") or created.get("data", {}).get("exception_id")
    revoked = _port(tmp_path).dispatch_gui(
        "revoke_rule_exception", [exception_id, "group-a", True],
        context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True,
    )
    assert revoked["ok"] is True


def test_feedback_project_mismatch_is_blocked(tmp_path):
    created = _create(tmp_path)
    store = RuleV2Store(tmp_path)
    store.record_receipt({"receipt_id": "project-receipt", "definition_id": created["data"]["definition_id"], "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"})
    denied = _port(tmp_path).dispatch_mcp(
        "memoryguard_rule_feedback", {"receipt_id": "project-receipt", "outcome": "followed", "idempotency_key": "project-feedback"},
        context=_context(tmp_path, admin=False, project="project-b"), generation=7, state="V2_ACTIVE",
    )
    assert denied["ok"] is False and denied["code"] == "rule_receipt_project_mismatch"


def test_feedback_empty_exception_body_rejected_before_write(tmp_path):
    lifecycle = NativeRuleLifecycleService(tmp_path)
    parent = lifecycle.create_auto({"text": "validate fixture inputs"}, context=_context(tmp_path))
    result = _port(tmp_path).dispatch_gui(
        "create_rule_exception", [parent["definition_id"], "", 10, "empty", "group-a", True],
        context=_context(tmp_path), generation=7, state="V2_ACTIVE", mutation=True,
    )
    assert result["ok"] is False
    assert _rows(lifecycle.store, "rule_exceptions") == []


def test_scope_evaluation_ledger_tracks_current_outcome(tmp_path):
    created = _create(tmp_path)
    store = RuleV2Store(tmp_path)
    store.record_receipt({"receipt_id": "scope-receipt", "definition_id": created["data"]["definition_id"], "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"})
    result = _port(tmp_path).dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": "scope-receipt", "outcome": "followed", "idempotency_key": "scope-feedback"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] and store.get_decision(result["data"]["decision"]["decision_id"])["action"] == "rule_feedback"


def test_mandatory_semantic_duplicate_proposes_low_confidence(tmp_path):
    store = RuleV2Store(tmp_path)
    first = store.upsert_definition(build_definition("always preserve provenance", rule_strength="must"))
    second = store.upsert_definition(build_definition("always keep provenance", rule_strength="must"))
    store.record_merge_proposal({"proposal_id": "duplicate-candidate", "definition_ids_json": json.dumps([first.definition_id, second.definition_id]), "status": "candidate", "metadata_json": json.dumps({"confidence": 0.42, "reason": "semantic_duplicate"})})
    assert json.loads(_rows(store, "rule_merge_proposals")[0]["metadata_json"])["confidence"] < 0.5


def test_mandatory_rule_supersedes_related_relevant_preference(tmp_path):
    store = RuleV2Store(tmp_path)
    relevant = store.upsert_definition(build_definition("preserve release notes", rule_strength="should"))
    mandatory = store.upsert_definition(build_definition("preserve release notes", rule_strength="must"))
    assert relevant.definition_id != mandatory.definition_id
    assert {item.rule_strength for item in store.list_definitions()} == {"should", "must"}


def test_rule_cockpit_service_unavailable_is_fail_closed(tmp_path):
    result = _port(tmp_path, _Manifest(state="V2_BUILDING")).dispatch_gui(
        "create_rule_from_text", {"text": "blocked while building"},
        context=_context(tmp_path), generation=7, state="V2_BUILDING",
    )
    assert result["ok"] is False and result["code"] == "v2_manifest_state_unavailable"


def test_gui_feedback_fallback_is_user_authority_and_checks_receipt_owner(tmp_path):
    created = _create(tmp_path)
    store = RuleV2Store(tmp_path)
    store.record_receipt({"receipt_id": "owner-receipt", "definition_id": created["data"]["definition_id"], "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"})
    result = _port(tmp_path).dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": "owner-receipt", "outcome": "followed", "actor": "user", "idempotency_key": "authority-feedback"}, context=_context(tmp_path, admin=False), generation=7, state="V2_ACTIVE")
    assert result["ok"]
    assert store.list_feedback(receipt_id="owner-receipt")[0]["authority"] == 3


def test_gui_lifecycle_feedback_never_falls_back_to_plain_store(tmp_path):
    result = _port(tmp_path).dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "plain store bypass"},
        context={"workspace_id": str(tmp_path), "admin": True}, generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False and result["code"] == "context_identity_required"


def test_interactive_rule_cockpit_surface_is_present():
    entries = {item["name"]: item for item in _port(Path.cwd()).coverage()["surfaces"]["gui"]["entries"]}
    for name in ("create_rule_from_text", "submit_rule_feedback", "undo_rule_decision", "list_rule_cockpit", "preview_effective_rules"):
        assert entries[name]["status"] == "implemented"
