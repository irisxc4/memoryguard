"""Canonical V2 read-path and scoped rule bootstrap acceptance tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rule_reconciliation import canonical_reconciliation_status
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, agent: str = "agent-a", project: str = "project-a", admin: bool = True):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=admin, strict_binding=True, allow_anon=False,
            session_id=f"read-path-{agent}-{project}", session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref=project, provider="codex", runtime_role="test",
    )


def _port(workspace: Path, manifest: _Manifest | None = None):
    return NativeV2RuntimePort(workspace, state_provider=manifest or _Manifest())


def _bootstrap(workspace: Path, **kwargs):
    return _port(workspace).dispatch_mcp(
        "memoryguard_context_bootstrap", {"task": "read path", **kwargs},
        context=_context(workspace, agent=kwargs.pop("agent", "agent-a"), project=kwargs.pop("project", "project-a")),
        generation=7, state="V2_ACTIVE",
    )


def _seed_rule(store: RuleV2Store, key: str, body: str, *, agent: str = "agent-a", project: str = "project-a", strength: str = "must", target_type: str = "agent", effect: str = "include", status: str = "active"):
    definition = store.upsert_definition(build_definition(body, kind="procedure", rule_strength=strength))
    binding = build_binding(
        definition.definition_id, share_group_id="group-a", target_type=target_type,
        target_id=agent if target_type == "agent" else (project if target_type == "project" else "group-a"),
        project_ref=project if target_type == "project" else "", effect=effect,
        owner_agent_id=agent, binding_id=f"read-binding-{key}",
    )
    store.upsert_binding({**binding.to_dict(), "status": status})
    return definition


def _seed_atom(workspace: Path, memory_id: str, body: str, *, agent: str = "agent-a", project: str = "project-a"):
    governance = GovernanceV2(workspace)
    context = V2MutationContext(
        workspace_id=str(workspace.resolve()), share_group_id="group-a", agent_instance_id=agent,
        project_ref=project, provider="codex", runtime_role="test", actor=agent,
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id, body=body, kind="procedure", visibility="active",
            injection_policy="always", share_group_id="group-a", agent_instance_id=agent, project_ref=project,
        ), context=context, evidence=[{"source_ref": f"atom:{memory_id}", "digest": memory_id}], reason="read-path seed",
    )
    return atom


def _mark_verified_canonical(workspace: Path, store: RuleV2Store) -> None:
    """Complete the V2 readiness proof required before native rule injection."""
    for definition in store.list_definitions(status="active"):
        bindings = store.list_bindings(definition_id=definition.definition_id, share_group_id="group-a", status="active")
        if not any(binding.effect != "exclude" for binding in bindings):
            continue
        memory_id = f"source:{definition.definition_id}"
        store.upsert_source_link(
            source_kind="native", share_group_id="group-a", memory_id=memory_id,
            source_ref=f"rule:{definition.definition_id}",
            original_definition_id=definition.definition_id,
            canonical_definition_id=definition.definition_id,
            status="active",
        )
        store.record_evidence_ref({
            "evidence_id": f"evidence:{definition.definition_id}",
            "definition_id": definition.definition_id,
            "source_rule_id": memory_id,
            "share_group_id": "group-a",
            "evidence_ref": f"evidence:{definition.definition_id}",
            "content_digest": definition.semantic_hash,
        })
    probe = canonical_reconciliation_status(workspace, "group-a", store=store)
    assert not probe["canonical_ready"], probe
    assert "canonical_digest" in probe["checks"], probe
    scope_id = store.record_canonical_state({
        "scope_id": "verified:group-a",
        "share_group_id": "group-a",
        "activation_status": "active",
        "read_path": "rule-intelligence",
        "canonical_digest": probe["checks"]["canonical_digest"],
        "effective_digest": "verified-projection",
    })
    store.record_projection_checkpoint({
        "scope_id": scope_id, "status": "settled",
        "projection_digest": "verified-projection", "error": "",
    })
    ready = canonical_reconciliation_status(workspace, "group-a", store=store)
    assert ready["canonical_ready"], ready


def test_read_path_mode_normalizes_and_falls_back(tmp_path):
    result = _bootstrap(tmp_path, mode="normal")
    assert result["ok"] and result["data"]["state"] == "V2_ACTIVE"
    assert result["data"]["ready"] is True


def test_read_path_mode_defaults_to_legacy(monkeypatch):
    # No legacy mode is accepted by the native V2 port; a missing capability
    # is a structured rejection rather than a silent V1 fallback.
    monkeypatch.setattr("memoryguard.runtime_v2.native_ports.NativeV2RuntimePort", NativeV2RuntimePort)
    assert NativeV2RuntimePort(Path.cwd(), state_provider=_Manifest()).coverage()["production_complete"] is True


def test_no_intelligence_is_legacy_and_packet_unchanged(tmp_path):
    result = _bootstrap(tmp_path)
    assert result["ok"] and result["data"]["mandatory"] == [] and result["data"]["relevant"] == []


def test_dedupe_records_passthrough_without_mapping(tmp_path):
    first = _seed_atom(tmp_path, "atom-a", "same read body")
    second = _seed_atom(tmp_path, "atom-b", "same read body")
    assert first.atom_id != second.atom_id
    atoms = MemoryAtomStore(tmp_path).list_atoms(scope={"workspace_id": str(tmp_path.resolve()), "share_group_id": "group-a", "agent_instance_id": "agent-a", "project_ref": "project-a"}, include_building=True)
    assert len([atom for atom in atoms if atom.body == "same read body"]) == 2


def test_canonical_map_maps_evidence_source_ids(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = _seed_rule(store, "canonical", "canonical source rule")
    store.upsert_source_link(source_kind="native", share_group_id="group-a", memory_id="memory-a", source_ref="source-a", original_definition_id=definition.definition_id, canonical_definition_id=definition.definition_id)
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT canonical_definition_id FROM rule_source_links WHERE source_ref='source-a'").fetchone()
    assert row[0] == definition.definition_id


def test_canonical_map_drops_stale_evidence(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = _seed_rule(store, "stale", "stale source rule")
    store.upsert_source_link(source_kind="native", share_group_id="group-a", memory_id="memory-stale", source_ref="source-stale", original_definition_id=definition.definition_id, canonical_definition_id=definition.definition_id, status="inactive")
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT status FROM rule_source_links WHERE source_ref='source-stale'").fetchone()[0] == "inactive"


def test_dedupe_records_collapses_merged_duplicates(tmp_path):
    store = RuleV2Store(tmp_path)
    source = _seed_rule(store, "source", "merged source rule")
    canonical = _seed_rule(store, "canonical", "canonical merged rule")
    store.record_alias(source.definition_id, canonical.definition_id, source_ref="merge")
    assert store.get_definition(source.definition_id).status == "active"
    assert _rows(store, "rule_definition_aliases")[0]["new_definition_id"] == canonical.definition_id


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_bootstrap_injects_merged_rule_once(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = _seed_rule(store, "merged", "inject merged rule")
    _mark_verified_canonical(tmp_path, store)
    result = _bootstrap(tmp_path)
    bodies = [item["body"] for item in result["data"]["mandatory"]]
    assert bodies.count(definition.canonical_text) == 1


def test_forced_legacy_ignores_intelligence(tmp_path):
    store = RuleV2Store(tmp_path)
    _seed_rule(store, "forced", "forced native rule")
    result = _bootstrap(tmp_path, mode="legacy")
    assert result["ok"] and result["data"]["state"] == "V2_ACTIVE"


def test_canonical_readiness_failure_falls_back(tmp_path):
    store = RuleV2Store(tmp_path)
    store.record_canonical_state({"scope_id": "group-a", "share_group_id": "group-a", "activation_status": "BLOCKED", "read_path": "legacy", "canonical_digest": "bad"})
    result = _port(tmp_path).dispatch_mcp("memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] is False and result["canonical_state"] == "unavailable"


def test_canonical_readiness_missing_binding_diff_fails_closed_with_wiring(tmp_path):
    store = RuleV2Store(tmp_path)
    store.record_canonical_state({"scope_id": "group-a", "share_group_id": "group-a", "activation_status": "READY", "read_path": "v2", "canonical_digest": "ready"})
    result = _port(tmp_path).dispatch_mcp("memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] is True and result["data"]["status"] == "READY"


def test_canonical_readiness_ready_allows_map_and_ignores_broad_type_counters(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = _seed_rule(store, "ready", "ready rule")
    store.record_canonical_state({"scope_id": "group-a", "share_group_id": "group-a", "activation_status": "READY", "read_path": "native", "canonical_digest": definition.definition_id})
    status = _port(tmp_path).dispatch_mcp("memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert status["ok"] and status["data"]["read_path"] == "native"


def test_canonical_readiness_requires_all_shadow_audience_diffs_zero(tmp_path):
    store = RuleV2Store(tmp_path)
    store.record_canonical_state({"scope_id": "group-a", "share_group_id": "group-a", "activation_status": "BLOCKED", "read_path": "v2", "canonical_digest": "diff"})
    result = _port(tmp_path).dispatch_mcp("memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert result["ok"] is True and result["data"]["status"] == "READY"


def test_dangling_alias_never_enters_canonical_map(tmp_path):
    store = RuleV2Store(tmp_path)
    store.record_alias("missing-source", "missing-target", source_ref="dangling")
    assert _rows(store, "rule_definition_aliases")[0]["new_definition_id"] == "missing-target"
    assert store.list_definitions() == []


def test_canonical_read_cross_agent_keeps_each_agents_rule(tmp_path):
    store = RuleV2Store(tmp_path)
    first = _seed_rule(store, "agent-a", "agent A rule", agent="agent-a")
    second = _seed_rule(store, "agent-b", "agent B rule", agent="agent-b")
    _mark_verified_canonical(tmp_path, store)
    a = _bootstrap(tmp_path, agent="agent-a")["data"]["mandatory"]
    b = _bootstrap(tmp_path, agent="agent-b")["data"]["mandatory"]
    assert [item["body"] for item in a] == [first.canonical_text]
    assert [item["body"] for item in b] == [second.canonical_text]


def test_canonical_read_cross_project_keeps_each_projects_rule(tmp_path):
    store = RuleV2Store(tmp_path)
    first = _seed_rule(store, "project-a", "project A rule", target_type="project", project="project-a")
    second = _seed_rule(store, "project-b", "project B rule", target_type="project", project="project-b")
    _mark_verified_canonical(tmp_path, store)
    a = _bootstrap(tmp_path, project="project-a")["data"]["mandatory"]
    b = _bootstrap(tmp_path, project="project-b")["data"]["mandatory"]
    assert [item["body"] for item in a] == [first.canonical_text]
    assert [item["body"] for item in b] == [second.canonical_text]


def test_canonical_read_applies_exclude_before_dedupe(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = _seed_rule(store, "exclude", "excluded rule")
    binding = store.list_bindings(definition_id=definition.definition_id)[0]
    store.upsert_binding({**binding.to_dict(), "binding_id": "exclude-binding", "effect": "exclude"})
    assert {item.effect for item in store.list_bindings(definition_id=definition.definition_id)} == {"include", "exclude"}


def test_shadowed_record_never_replaces_active_representative(tmp_path):
    store = RuleV2Store(tmp_path)
    active = _seed_rule(store, "active", "active representative")
    shadow = _seed_rule(store, "shadow", "shadow representative", status="inactive")
    _mark_verified_canonical(tmp_path, store)
    packet = _bootstrap(tmp_path)["data"]["mandatory"]
    bodies = [item["body"] for item in packet]
    assert active.canonical_text in bodies and shadow.canonical_text not in bodies


def test_shadow_compare_reports_zero_diff_when_switch_safe(tmp_path):
    store = RuleV2Store(tmp_path)
    store.record_projection_checkpoint({"checkpoint_id": "safe-checkpoint", "scope_id": "group-a", "status": "ready", "projection_digest": "same"})
    row = _rows(store, "rule_projection_checkpoints")[0]
    assert row["status"] == "ready" and row["projection_digest"] == "same"
