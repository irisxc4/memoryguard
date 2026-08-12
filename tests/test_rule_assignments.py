from __future__ import annotations

from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import settle_native_canonical_snapshot
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.rules.v2_store import RuleV2Store


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, *, agent: str = "a", project: str = "p", provider: str = "codex", runtime: str = "terra", admin: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"assignment-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="team",
        project_ref=project,
        provider=provider,
        runtime_role=runtime,
    )


def _seed_rule(store: RuleV2Store, memory_id: str, *, target_type: str = "agent", target_id: str = "a", project_ref: str = "", provider: str = "", runtime_role: str = "", priority: int = 0, strength: str = "must", effect: str = "include"):
    definition = store.upsert_definition(build_definition(
        f"rule {memory_id}", kind="procedure", rule_strength=strength,
    ))
    binding = build_binding(
        definition.definition_id,
        share_group_id="team",
        target_type=target_type,
        target_id=target_id,
        project_ref=project_ref,
        provider=provider,
        runtime_role=runtime_role,
        priority=priority,
        effect=effect,
        owner_agent_id="a",
        created_by="admin",
        authorization="test",
        binding_id=f"binding-{memory_id}",
    )
    store.upsert_binding(binding)
    return definition, binding


def _bootstrap(workspace: Path, *, agent: str = "a", project: str = "p", provider: str = "codex", runtime: str = "terra"):
    store = RuleV2Store(workspace)
    for definition in store.list_definitions(status="active"):
        bindings = store.list_bindings(
            definition_id=definition.definition_id,
            share_group_id="team",
            status="active",
        )
        if not any(binding.effect != "exclude" for binding in bindings):
            continue
        source_id = f"test-source:{definition.definition_id}"
        source_ref = f"test-rule:{definition.definition_id}"
        store.upsert_source_link(
            source_kind="test-governed-rule",
            share_group_id="team",
            memory_id=source_id,
            source_ref=source_ref,
            original_definition_id=definition.definition_id,
            canonical_definition_id=definition.definition_id,
            status="active",
        )
        store.record_evidence_ref({
            "evidence_id": f"test-evidence:{definition.definition_id}",
            "definition_id": definition.definition_id,
            "source_rule_id": source_id,
            "share_group_id": "team",
            "evidence_ref": source_ref,
            "content_digest": definition.semantic_hash,
        })
    if store.list_definitions(status="active"):
        settle_native_canonical_snapshot(workspace, "team", store=store)
    port = NativeV2RuntimePort(workspace, state_provider=_Manifest())
    result = port.dispatch_mcp(
        "memoryguard_context_bootstrap",
        {"task": "unrelated work"},
        context=_context(workspace, agent=agent, project=project, provider=provider, runtime=runtime),
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return result["data"]


def test_agent_assignment_has_zero_cross_agent_leakage(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agents(["a", "b"], share_group_id="team")
    store = RuleV2Store(tmp_path)
    definition, _ = _seed_rule(store, "only-a", target_id="a")
    assert [item["body"] for item in _bootstrap(tmp_path, agent="a")["mandatory"]] == [definition.canonical_text]
    assert _bootstrap(tmp_path, agent="b")["mandatory"] == []


def test_group_exclude_is_persisted_as_an_explicit_permission_record(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, _ = _seed_rule(store, "public", target_type="group", target_id="team")
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="team",
        target_type="agent",
        target_id="a",
        effect="exclude",
        owner_agent_id="a",
        created_by="admin",
        authorization="test",
        binding_id="exclude-public-a",
    ))
    bindings = store.list_bindings(definition_id=definition.definition_id)
    assert {(item.target_type, item.target_id, item.effect) for item in bindings} == {
        ("group", "team", "include"), ("agent", "a", "exclude"),
    }


def test_project_provider_and_role_are_intersections(tmp_path):
    store = RuleV2Store(tmp_path)
    project, _ = _seed_rule(store, "project", target_type="project", target_id="", project_ref="p")
    provider, _ = _seed_rule(store, "provider", target_type="provider", target_id="codex")
    role, _ = _seed_rule(store, "role", target_type="runtime_role", target_id="terra")
    packet = _bootstrap(tmp_path, project="p", provider="codex", runtime="terra")
    assert {item["body"] for item in packet["mandatory"]} == {project.canonical_text, provider.canonical_text, role.canonical_text}
    assert _bootstrap(tmp_path, project="q", provider="claude", runtime="worker")["mandatory"] == []


def test_windows_project_paths_are_canonicalized_for_matching(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed_rule(
        store,
        "windows-project",
        target_type="agent_project",
        target_id="a",
        project_ref=r"C:\Work\Demo",
    )
    assert binding.project_ref == "c:/work/demo"
    assert definition.definition_id


def test_unscoped_rule_is_quarantined_from_injection_without_group_dos(tmp_path):
    store = RuleV2Store(tmp_path)
    _seed_rule(store, "unscoped", target_type="agent", target_id="")
    assert _bootstrap(tmp_path, agent="a")["mandatory"] == []
    assert _bootstrap(tmp_path, agent="b")["mandatory"] == []


def test_equal_body_relevant_and_mandatory_do_not_collapse_in_either_order(tmp_path):
    store = RuleV2Store(tmp_path)
    relevant = store.upsert_definition(build_definition("shared release process", kind="procedure"))
    mandatory = store.upsert_definition(build_definition("shared release process", kind="procedure", rule_strength="must"))
    store.upsert_binding(build_binding(relevant.definition_id, share_group_id="team", target_type="agent", target_id="a", owner_agent_id="a", binding_id="relevant"))
    store.upsert_binding(build_binding(mandatory.definition_id, share_group_id="team", target_type="agent", target_id="a", owner_agent_id="a", binding_id="mandatory"))
    packet = _bootstrap(tmp_path)
    assert {item["body"] for item in packet["mandatory"]} == {mandatory.canonical_text}
    assert {item["body"] for item in packet["relevant"]} == {relevant.canonical_text}
    assert relevant.definition_id != mandatory.definition_id


def test_mandatory_budget_is_per_agent(tmp_path):
    store = RuleV2Store(tmp_path)
    for index in range(25):
        _seed_rule(store, f"a-{index}", target_id="a")
    b_first, _ = _seed_rule(store, "b-first", target_id="b")
    a_packet = _bootstrap(tmp_path, agent="a")
    b_packet = _bootstrap(tmp_path, agent="b")
    assert len(a_packet["mandatory"]) <= 20
    assert [item["body"] for item in b_packet["mandatory"]] == [b_first.canonical_text]


def test_priority_override_orders_and_receipts_stable_assignment_id(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed_rule(store, "high", target_id="a", priority=100)
    assert binding.priority == 100
    assert store.list_bindings(definition_id=definition.definition_id)[0].binding_id == binding.binding_id


def test_corrupt_rule_only_blocks_matching_audience(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed_rule(store, "scope-a", target_id="a")
    inactive = build_binding(
        definition.definition_id,
        share_group_id="team",
        target_type="agent",
        target_id="b",
        owner_agent_id="a",
        binding_id="inactive-b",
    )
    store.upsert_binding({**inactive.to_dict(), "status": "inactive"})
    assert _bootstrap(tmp_path, agent="a")["mandatory"]
    assert _bootstrap(tmp_path, agent="b")["mandatory"] == []
    assert binding.status == "active"


def test_assignments_survive_snapshot_and_clear_cascades(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed_rule(store, "snap", target_id="a")
    inactive = build_binding(
        definition.definition_id,
        share_group_id="team",
        target_type="agent",
        target_id="a",
        owner_agent_id="a",
        binding_id="snap-inactive",
    )
    store.upsert_binding({**inactive.to_dict(), "status": "inactive"})
    assert len(store.list_bindings(definition_id=definition.definition_id)) == 2
    assert store.get_definition(definition.definition_id).status == "active"
    assert binding.binding_id == "binding-snap"
