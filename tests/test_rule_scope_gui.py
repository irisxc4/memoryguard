"""GUI-facing V2 rule audience controls must preserve scoped-rule invariants."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.assets_v2.store import AssetStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.content.store import ContentStore
from memoryguard.cutover_v2.surfaces import GUI_OPERATION_SPECS
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.gui import GovernanceApi
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.skills_v2.store import SkillStore
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState
from memoryguard.security import MUTATION_API_METHODS, READONLY_API_METHODS


def _ensure_v2_workspace(root: Path) -> None:
    """Create the real V2 databases and activate their persisted manifest."""
    manager = ManifestManager(root)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return

    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    RuleV2Store(root)
    ProjectionStore(root)
    ContentStore(root)
    RuntimeStore(root)
    CodeGraphStore(root)
    AssetStore(root)
    SkillStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)

    manager.transition(ManifestState.V2_BUILDING, migration_id="gui-rule-scope-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="gui-rule-scope-source",
        target_digest="gui-rule-scope-target",
        manifest_digest="gui-rule-scope-manifest",
        digests={"validator_passed": True, "checkpoints": {"gui": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _gui_api(
    root: Path,
    *,
    admin: bool = True,
    agent: str = "agent-a",
    group: str = "team-rules",
) -> GovernanceApi:
    _ensure_v2_workspace(root)
    GroupControlService(root, write=True).bind_agent(agent, group)
    access = AccessContext(
        trusted_agent_id=agent,
        is_admin=admin,
        strict_binding=True,
        allow_anon=False,
        session_id=f"gui-rule-scope-{agent}",
        session_source="transport",
        session_trusted=True,
    )
    return GovernanceApi(str(root), _trusted_access_context=access)


def _prepare(workspace: Path) -> tuple[GovernanceApi, str, RuleV2Store]:
    group_id = "team-rules"
    _ensure_v2_workspace(workspace)
    group_service = GroupControlService(workspace, write=True)
    group_service.bind_agent("agent-a", group_id)
    group_service.bind_agent("agent-b", group_id)

    store = RuleV2Store(workspace)
    definition = build_definition(
        "keep scope isolated",
        definition_id="rule-1",
        kind="procedure",
    )
    store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=group_id,
        target_type="agent",
        target_id="agent-a",
        owner_agent_id="agent-a",
        created_by="admin",
        authorization="fixture:v2-rule-scope",
    ))
    return _gui_api(workspace, group=group_id), group_id, store


def test_rule_scope_options_only_expose_discovered_or_bound_targets(tmp_path: Path) -> None:
    api, group_id, _store = _prepare(tmp_path)

    result = api.get_rule_scope_options(group_id)

    assert result["ok"] is True, result
    options = result["data"]
    # V2 options are the current trusted capability scope, not a browser-
    # supplied directory or a global agent directory.
    assert GroupControlService(tmp_path, write=False).active_binding_for_agent("agent-b") is not None
    assert {item["id"] for item in options["agents"]} == {"agent-a"}
    assert group_id in {item["id"] for item in options["groups"]}
    assert str(tmp_path.resolve()) in {item["id"] for item in options["projects"]}
    assert tmp_path.name not in {item["id"] for item in options["projects"]}
    assert {item["id"] for item in options["runtime_roles"]} == {"gui"}
    assert "legacy_unknown" not in options


def test_gui_audience_update_is_atomic_and_preview_is_agent_scoped(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    api, group_id, store = _prepare(tmp_path)

    rejected = api.update_rule_audience(
        "rule-1", [], group_id, "always", confirmed=True,
    )
    assert rejected["ok"] is False, rejected
    assert rejected["error"] == "always_rule_requires_include_audience"
    before_unknown = [
        item.to_dict()
        for item in store.list_bindings(definition_id="rule-1", share_group_id=group_id)
    ]

    unknown = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "invented-id"}],
        group_id, "always", confirmed=True,
    )
    assert unknown["ok"] is False, unknown
    assert unknown["error"] == "unknown_agent_target"
    after_unknown = [
        item.to_dict()
        for item in store.list_bindings(definition_id="rule-1", share_group_id=group_id)
    ]
    assert after_unknown == before_unknown

    changed = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "agent-a"}],
        group_id, "always", priority=50, confirmed=True,
    )
    assert changed["ok"] is True, changed
    assert changed["data"]["bindings"][0]["target_id"] == "agent-a"
    decisions = api.list_rule_decisions()
    assert decisions["ok"] is True, decisions
    assert any(
        item["action"] == "rule_audience_update"
        for item in decisions["data"]["decisions"]
    )

    preview_a = api.preview_effective_rules("agent-a", group_id)
    preview_b_api = _gui_api(tmp_path, agent="agent-b", group=group_id)
    preview_b = preview_b_api.preview_effective_rules("agent-b", group_id)
    assert [item["definition_id"] for item in preview_a["data"]["effective"]] == ["rule-1"]
    assert [item["definition_id"] for item in preview_b["data"]["effective"]] == []
    assert [item["definition_id"] for item in preview_b["data"]["unavailable"]] == ["rule-1"]

    excluded = api.update_rule_audience(
        "rule-1", [
            {"target_type": "group", "target_id": group_id, "effect": "include"},
            {"target_type": "agent", "target_id": "agent-b", "effect": "exclude"},
        ], group_id, "always", priority=50, confirmed=True,
    )
    assert excluded["ok"] is True, excluded
    preview_b = preview_b_api.preview_effective_rules("agent-b", group_id)
    assert [item["definition_id"] for item in preview_b["data"]["excluded"]] == ["rule-1"]

    # always -> relevant clears active V2 bindings without deleting the rule
    # definition or its canonical text.
    restored = preview_b_api.update_rule_audience(
        "rule-1", [], group_id, "relevant", confirmed=True,
    )
    assert restored["ok"] is True, restored
    assert store.get_definition("rule-1") is not None
    assert store.list_bindings(
        definition_id="rule-1", share_group_id=group_id, status="active",
    ) == []


def test_legacy_unknown_targets_are_display_only_and_confirmation_is_noop(
    tmp_path: Path, monkeypatch,
) -> None:
    """V2 has no legacy-unknown bucket; unknown and unconfirmed scopes fail closed."""
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    api, group_id, store = _prepare(tmp_path)
    before = [
        item.to_dict()
        for item in store.list_bindings(definition_id="rule-1", share_group_id=group_id)
    ]

    options = api.get_rule_scope_options(group_id)
    assert options["ok"] is True, options
    assert "removed-agent" not in {
        item["id"] for item in options["data"]["agents"]
    }
    assert "legacy_unknown" not in options["data"]

    listed = api.list_rules_habits(group_id)
    assert listed["ok"] is True, listed
    assert {
        item["definition_id"] for item in listed["data"]["rules"]
    } == {"rule-1"}

    denied = api.update_rule_audience(
        "rule-1", [], group_id, "relevant", confirmed=False,
    )
    assert denied["ok"] is False, denied
    assert denied["error"] == "confirmation_required"
    assert [
        item.to_dict()
        for item in store.list_bindings(definition_id="rule-1", share_group_id=group_id)
    ] == before

    retained = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "agent-a"}],
        group_id, "always", confirmed=True,
    )
    assert retained["ok"] is True, retained
    invented = api.update_rule_audience(
        "rule-1", [{"target_type": "agent", "target_id": "new-invented-agent"}],
        group_id, "always", confirmed=True,
    )
    assert invented["ok"] is False, invented
    assert invented["error"] == "unknown_agent_target"


def test_rule_habits_list_hides_deleted_and_shadowed_records(tmp_path: Path) -> None:
    group_id = "team-rules"
    _ensure_v2_workspace(tmp_path)
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", group_id)
    store = RuleV2Store(tmp_path)

    active = build_definition(
        "active preference", definition_id="active-pref", kind="preference",
    )
    shadowed = replace(build_definition(
        "shadowed preference", definition_id="shadowed-pref", kind="preference",
    ), status="superseded")
    deleted = replace(build_definition(
        "deleted preference", definition_id="deleted-pref", kind="preference",
    ), status="deleted")
    for definition in (active, shadowed, deleted):
        store.upsert_definition(definition)
    store.upsert_binding(build_binding(
        active.definition_id,
        share_group_id=group_id,
        target_type="agent",
        target_id="agent-a",
        owner_agent_id="agent-a",
        created_by="admin",
        authorization="fixture:v2-rule-habits",
    ))

    listed = _gui_api(tmp_path, group=group_id).list_rules_habits(group_id)
    assert listed["ok"] is True, listed
    visible = {item["definition_id"] for item in listed["data"]["rules"]}
    assert visible == {"active-pref"}


def test_preview_accepts_only_verified_project_provider_and_role_contexts(tmp_path: Path, monkeypatch) -> None:
    del monkeypatch
    api, group_id, store = _prepare(tmp_path)
    project_ref = str(tmp_path.resolve())
    for memory_id, target_type, target_id, binding_project, provider, runtime_role in (
        ("project-rule", "project", "", project_ref, "", ""),
        ("provider-rule", "provider", "gui", "", "gui", ""),
        ("role-rule", "runtime_role", "gui", "", "", "gui"),
    ):
        definition = build_definition(
            memory_id, definition_id=memory_id, kind="procedure",
        )
        store.upsert_definition(definition)
        store.upsert_binding(build_binding(
            definition.definition_id,
            share_group_id=group_id,
            target_type=target_type,
            target_id=target_id,
            project_ref=binding_project,
            provider=provider,
            runtime_role=runtime_role,
            owner_agent_id="",
            created_by="admin",
            authorization="fixture:v2-preview-context",
        ))

    matched = api.preview_effective_rules(
        "agent-a", group_id, project_ref, "gui", "gui",
    )
    assert matched["ok"] is True, matched
    assert {
        item["definition_id"] for item in matched["data"]["effective"]
    } == {"rule-1", "project-rule", "provider-rule", "role-rule"}

    forged = api.preview_effective_rules(
        "agent-a", group_id, "fabricated-project", "forged-provider", "forged-role",
    )
    assert forged["ok"] is False, forged
    assert forged["code"] == "context_identity_spoof"


def test_rule_audience_is_confirmed_mutation_and_page_has_scope_controls() -> None:
    assert "get_rule_scope_options" in READONLY_API_METHODS
    assert "preview_effective_rules" in READONLY_API_METHODS
    assert "update_rule_audience" in MUTATION_API_METHODS
    assert GUI_OPERATION_SPECS["update_rule_audience"].confirmation == "required"

    ui = (Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "interactive.py").read_text(encoding="utf-8")
    assert "管理规则适用范围" in ui
    assert "当前 Agent 生效" in ui
    assert "删除范围不会删除记忆" in ui
    assert "强制规则至少需要一个“包含”适用范围" in ui
    assert 'data-mg-action="rule-edit"' in ui
    assert 'onclick="openRuleAudienceEditor(' not in ui
    assert 'onclick="readHistorySession(' not in ui
    assert 'data-session-id="${escapeHtml(r.session_id)}"' in ui

    gui = (Path(__file__).resolve().parents[1] / "src" / "memoryguard" / "gui.py").read_text(encoding="utf-8")
    assert 'self.headers.get("X-Session-Token", "")' in gui
    assert 'self._json_response(403, {"error": "invalid_session_token"})' in gui
