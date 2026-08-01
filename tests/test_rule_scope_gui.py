"""GUI-facing rule audience controls must preserve the scoped-rule invariant."""
from __future__ import annotations

from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.gui import GovernanceApi
from memoryguard.schema_v3 import MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS
from memoryguard.shared_memory_store import SharedMemoryStore


def _prepare(workspace: Path) -> tuple[GovernanceApi, str]:
    group_id = "team-rules"
    bindings = AgentBindingStore(workspace)
    bindings.bind_agent("agent-a", group_id)
    bindings.bind_agent("agent-b", group_id)
    store = SharedMemoryStore(workspace, group_id)
    store.append_record(SharedMemoryRecord(
        memory_id="rule-1", body="keep scope isolated", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="relevant",
        agent_instance_id="agent-a",
    ))
    return GovernanceApi(workspace), group_id


def test_rule_scope_options_only_expose_discovered_or_bound_targets(tmp_path: Path) -> None:
    api, group_id = _prepare(tmp_path)

    options = api.get_rule_scope_options(group_id)

    assert options["ok"] is True
    assert {item["id"] for item in options["agents"]} >= {"agent-a", "agent-b"}
    assert group_id in {item["id"] for item in options["groups"]}
    # A folder name alone is not a trusted project identity.  GUI options only
    # contain resolver-emitted project refs, never a fabricated workspace name.
    assert tmp_path.name not in {item["id"] for item in options["projects"]}
    assert {item["id"] for item in options["runtime_roles"]} >= {"root", "subagent"}


def test_gui_audience_update_is_atomic_and_preview_is_agent_scoped(tmp_path: Path) -> None:
    api, group_id = _prepare(tmp_path)

    rejected = api.update_rule_audience(
        "rule-1", [], group_id, "always", confirmed=True, _admin_override=True,
    )
    assert rejected["error"] == "always_rule_requires_include_audience"
    assert SharedMemoryStore(tmp_path, group_id).get_record("rule-1").injection_policy == "relevant"

    unknown = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "invented-id"}],
        group_id, "always", confirmed=True, _admin_override=True,
    )
    assert unknown["error"] == "unknown_agent_target"

    changed = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "agent-a"}],
        group_id, "always", priority=50, confirmed=True, _admin_override=True,
    )
    assert changed["ok"] is True
    assert changed["assignments"][0]["target_id"] == "agent-a"
    assert any(
        item.action == "atomic_rule_transition"
        for item in SharedMemoryStore(tmp_path, group_id).list_decisions()
    )

    preview_a = api.preview_effective_rules("agent-a", group_id)
    preview_b = api.preview_effective_rules("agent-b", group_id)
    assert [item["memory_id"] for item in preview_a["effective"]] == ["rule-1"]
    assert [item["memory_id"] for item in preview_b["effective"]] == []
    assert [item["memory_id"] for item in preview_b["unavailable"]] == ["rule-1"]

    excluded = api.update_rule_audience(
        "rule-1", [
            {"target_type": "group", "target_id": group_id, "effect": "include"},
            {"target_type": "agent", "target_id": "agent-b", "effect": "exclude"},
        ], group_id, "always", priority=50, confirmed=True, _admin_override=True,
    )
    assert excluded["ok"] is True
    preview_b = api.preview_effective_rules("agent-b", group_id)
    assert [item["memory_id"] for item in preview_b["excluded"]] == ["rule-1"]

    # always -> relevant clears all assignments in the same store transaction;
    # it does not delete the memory body or its record.
    restored = api.update_rule_audience(
        "rule-1", [], group_id, "relevant", confirmed=True, _admin_override=True,
    )
    assert restored["ok"] is True
    store = SharedMemoryStore(tmp_path, group_id)
    assert store.get_record("rule-1").injection_policy == "relevant"
    assert store.list_rule_assignments("rule-1") == []


def test_legacy_unknown_targets_are_display_only_and_confirmation_is_noop(tmp_path: Path) -> None:
    api, group_id = _prepare(tmp_path)
    store = SharedMemoryStore(tmp_path, group_id)
    store.transition_injection_policy(
        "rule-1", "always", 0,
        assignments=[{"target_type": "agent", "target_id": "removed-agent"}],
    )

    before = store.get_record("rule-1").to_dict()
    denied = api.update_rule_audience(
        "rule-1", [], group_id, "relevant", confirmed=False, _admin_override=True,
    )
    assert denied["error"] == "confirmation_required"
    assert store.get_record("rule-1").to_dict() == before
    assert store.list_rule_assignments("rule-1")[0].target_id == "removed-agent"

    options = api.get_rule_scope_options(group_id)
    assert "removed-agent" not in {item["id"] for item in options["agents"]}
    assert any(item["target_id"] == "removed-agent" for item in options["legacy_unknown"])
    listed = api.list_rules_habits(group_id)
    rule = listed["buckets"]["mandatory"][0]
    assert rule["legacy_unknown_assignment_ids"]

    # Existing legacy relations can be retained during a separate edit, but a
    # new invented target is still rejected by the server-side option check.
    retained = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "removed-agent"}],
        group_id, "always", confirmed=True, _admin_override=True,
    )
    assert retained["ok"] is True
    invented = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "new-invented-agent"}],
        group_id, "always", confirmed=True, _admin_override=True,
    )
    assert invented["error"] == "unknown_agent_target"


def test_preview_accepts_only_verified_project_provider_and_role_contexts(tmp_path: Path, monkeypatch) -> None:
    api, group_id = _prepare(tmp_path)
    verified = {
        "agents": [{"id": "agent-a", "label": "A"}],
        "groups": [{"id": group_id, "label": group_id}],
        "projects": [{"id": "project:alpha", "label": "Alpha"}],
        "providers": [{"id": "codex", "label": "Codex"}],
        "runtime_roles": [{"id": "root", "label": "root"}],
        "legacy_unknown": [],
    }
    monkeypatch.setattr(api, "_rule_scope_options", lambda _group: verified)
    store = SharedMemoryStore(tmp_path, group_id)
    for memory_id, assignment in (
        ("project-rule", {"target_type": "project", "project_ref": "project:alpha"}),
        ("provider-rule", {"target_type": "provider", "target_id": "codex"}),
        ("role-rule", {"target_type": "runtime_role", "target_id": "root"}),
    ):
        store.append_record(SharedMemoryRecord(
            memory_id=memory_id, body=memory_id, kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE, injection_policy="relevant",
            agent_instance_id="agent-a",
        ))
        store.transition_injection_policy(memory_id, "always", 0, assignments=[assignment])

    matched = api.preview_effective_rules(
        "agent-a", group_id, "project:alpha", "codex", "root",
    )
    assert {item["memory_id"] for item in matched["effective"]} == {
        "project-rule", "provider-rule", "role-rule",
    }
    unknown = api.preview_effective_rules("agent-a", group_id, "fabricated-project")
    assert unknown["error"] == "unknown_project_target"


def test_rule_audience_is_confirmed_mutation_and_page_has_scope_controls() -> None:
    assert "get_rule_scope_options" in READONLY_API_METHODS
    assert "preview_effective_rules" in READONLY_API_METHODS
    assert "update_rule_audience" in MUTATION_API_METHODS

    ui = (Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "interactive.py").read_text(encoding="utf-8")
    assert "管理规则适用范围" in ui
    assert "当前 Agent 生效" in ui
    assert "删除范围不会删除记忆" in ui
    assert "强制规则至少需要一个“包含”适用范围" in ui
    assert "data-mg-action=\"rule-edit\"" in ui
    assert "onclick=\"openRuleAudienceEditor(" not in ui
    assert "onclick=\"readHistorySession(" not in ui
    assert "data-session-id=\"${escapeHtml(r.session_id)}\"" in ui

    gui = (Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "gui.py").read_text(encoding="utf-8")
    assert 'self.headers.get("X-Session-Token", "")' in gui
    assert 'self._json_response(403, {"error": "invalid_session_token"})' in gui
