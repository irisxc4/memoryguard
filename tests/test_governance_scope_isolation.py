"""Exact-scope, source authorization, and V2 projection isolation tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.group_native import GroupControlError, GroupControlService, personal_group_id
from memoryguard.runtime_v2.projection_build import (
    ProjectionBuildError,
    ProjectionBuildService,
    V2ReleaseService,
    projection_scope_from_context,
)
from memoryguard.runtime_v2.source_control import SourceControlError, SourceControlService


def _context(
    tmp_path: Path,
    agent: str,
    group: str,
    *,
    provider: str = "pytest",
    runtime_role: str = "test",
    admin: bool = False,
) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider=provider,
        runtime_role=runtime_role,
        actor=agent,
        authority="admin" if admin else "manual",
        admin=admin,
    )


def _read_scope(tmp_path: Path, agent: str, group: str) -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        runtime_role="test",
    )


def _projection_scope(tmp_path: Path, agent: str, group: str) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        share_group_id=group,
        sensitivity="normal",
        policy_class="private",
    )


def _seed_atom(
    tmp_path: Path,
    *,
    agent: str,
    group: str,
    memory_id: str,
    body: str,
    metadata: dict | None = None,
) -> MemoryAtom:
    ContentStore(tmp_path)
    memory = MemoryAtomStore(tmp_path, readonly=False)
    EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory)
    context = _context(tmp_path, agent, group)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    evidence, _ = governance.put_evidence(
        context=context,
        reason="scope fixture evidence",
        source_ref=f"fixture:{memory_id}",
        digest=digest,
        authority="governance",
        evidence_type="reference",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            kind="fact",
            confidence=0.9,
            workspace_id=str(tmp_path.resolve()),
            agent_instance_id=agent,
            share_group_id=group,
            project_ref=str(tmp_path.resolve()),
            provider="pytest",
            runtime_role="test",
            metadata=metadata or {"scope": "project", "title": memory_id},
            provenance=[{"source_ref": f"fixture:{memory_id}", "source_digest": digest}],
        ),
        context=context,
        evidence=[evidence.to_dict()],
        reason="scope fixture atom",
        idempotency_key=f"scope-fixture:{group}:{memory_id}",
    )
    for _ in range(4):
        state = memory.project_evidence(governance.evidence)
        if int(state.get("pending", 0)) == 0:
            break
    memory.set_visibility("active", atom_ids=[atom.atom_id])
    return memory.get_atom(memory_id, scope=_read_scope(tmp_path, agent, group), include_building=True) or atom


def _projection_record(tmp_path: Path, service: ProjectionBuildService, scope: ProjectionReadScope):
    key = service._scope_key("reconstructed", scope)
    return ProjectionStore(tmp_path, initialize=False).get_projection("scenario", key, scope=scope)


def _derived_graph(record):
    metadata = record.payload.get("metadata")
    assert isinstance(metadata, dict)
    graph = metadata.get("derived_graph")
    assert isinstance(graph, dict)
    assert isinstance(graph.get("nodes"), list)
    assert isinstance(graph.get("edges"), list)
    return graph


def test_scope_reads_keep_agents_and_groups_isolated(tmp_path: Path) -> None:
    _seed_atom(tmp_path, agent="agent-a", group="group-a", memory_id="a1", body="only A")
    _seed_atom(tmp_path, agent="agent-b", group="group-b", memory_id="b1", body="only B")
    memory = MemoryAtomStore(tmp_path, readonly=True)
    a_atoms = memory.list_atoms(scope=_read_scope(tmp_path, "agent-a", "group-a"))
    b_atoms = memory.list_atoms(scope=_read_scope(tmp_path, "agent-b", "group-b"))
    assert [atom.memory_id for atom in a_atoms] == ["a1"]
    assert [atom.memory_id for atom in b_atoms] == ["b1"]

    projections = ProjectionBuildService(tmp_path)
    scope_a = _projection_scope(tmp_path, "agent-a", "group-a")
    scope_b = _projection_scope(tmp_path, "agent-b", "group-b")
    assert projections.build(mode="reconstructed", scope=scope_a, runtime_role="test")["atom_count"] == 1
    assert projections.build(mode="reconstructed", scope=scope_b, runtime_role="test")["atom_count"] == 1
    record_a = _projection_record(tmp_path, projections, scope_a)
    record_b = _projection_record(tmp_path, projections, scope_b)
    assert record_a is not None and record_b is not None
    assert "only A" not in str(record_b.payload)
    assert "only B" not in str(record_a.payload)
    graph_a = _derived_graph(record_a)
    graph_b = _derived_graph(record_b)
    assert {node.get("memory_id") for node in graph_a["nodes"]} >= {"a1"}
    assert {node.get("memory_id") for node in graph_b["nodes"]} >= {"b1"}


def test_projection_scope_is_required_and_workspace_bound(tmp_path: Path) -> None:
    with pytest.raises(ProjectionBuildError, match="projection_scope_required"):
        projection_scope_from_context(tmp_path, {"agent_instance_id": "agent-a"})
    with pytest.raises(ProjectionBuildError, match="projection_scope_workspace_mismatch"):
        projection_scope_from_context(
            tmp_path,
            {
                "workspace_id": str(tmp_path / "foreign"),
                "agent_instance_id": "agent-a",
                "project_ref": str(tmp_path),
                "provider": "pytest",
                "share_group_id": "group-a",
                "sensitivity": "normal",
                "policy_class": "private",
            },
        )


def test_personal_group_identity_is_not_an_authorization_grant(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    with pytest.raises(GroupControlError, match="active_binding_required"):
        service.set_scope("agent-a", {"mode": "agent", "agent_instance_id": "agent-a"})
    personal = service.ensure_personal("agent-a")
    assert personal["share_group_id"] == personal_group_id("agent-a")
    scope = service.set_scope("agent-a", {"mode": "agent", "agent_instance_id": "agent-a"})
    assert scope["scope"]["share_group_id"] == personal["share_group_id"]
    with pytest.raises(GroupControlError, match="governance_scope_forbidden"):
        service.set_scope("agent-a", {"mode": "share_group", "share_group_id": "other"})


def test_source_control_hides_selected_source_from_foreign_agent(tmp_path: Path) -> None:
    source = tmp_path / "agent-a.md"
    source.write_text("# private\nA only\n", encoding="utf-8")
    control = SourceControlService(tmp_path)
    created = control.add(str(source), "selected_file", {"admin": True, "agent_instance_id": "agent-a"})
    assert created["source_id"]
    own = control.list_sources({"agent_instance_id": "agent-a"})
    foreign = control.list_sources({"agent_instance_id": "agent-b"})
    assert [item["source_id"] for item in own["sources"]] == [created["source_id"]]
    assert foreign["sources"] == []
    with pytest.raises(SourceControlError) as exc_info:
        control.resolve_path(str(source), {"agent_instance_id": "agent-b"})
    assert exc_info.value.code == "no_source"


def test_source_control_mutations_require_admin_capability(tmp_path: Path) -> None:
    source = tmp_path / "memory.md"
    source.write_text("memory", encoding="utf-8")
    control = SourceControlService(tmp_path)
    created = control.add(str(source), "selected_file", {"admin": True})
    with pytest.raises(SourceControlError, match="admin_capability_required"):
        control.remove(created["source_id"], {"agent_instance_id": "agent-a"})
    assert control.list_sources({"admin": True})["sources"][0]["enabled"] is True


def test_dissolving_group_preserves_v2_memory_domain(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    service.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    _seed_atom(tmp_path, agent="agent-a", group="shared-team", memory_id="shared-1", body="shared body")
    dissolved = service.dissolve("shared-team")
    assert dissolved["unbound_count"] == 2
    assert service.group_preview("shared-team")["member_count"] == 0
    assert MemoryAtomStore(tmp_path, readonly=True).list_atoms(
        scope=MemoryReadScope(workspace_id=str(tmp_path.resolve()), share_group_id="shared-team", admin=True),
        include_building=True,
    )


def test_projection_delete_is_a_store_tombstone(tmp_path: Path) -> None:
    _seed_atom(tmp_path, agent="agent-a", group="group-a", memory_id="a1", body="body")
    service = ProjectionBuildService(tmp_path)
    scope = _projection_scope(tmp_path, "agent-a", "group-a")
    built = service.build(mode="reconstructed", scope=scope, runtime_role="test")
    assert built["status"] == "succeeded"
    deleted = service.delete(mode="reconstructed", scope=scope)
    assert deleted["deleted"] is True
    assert service.current(mode="reconstructed", scope=scope)["projection"] is None
    assert ProjectionStore(tmp_path, initialize=False).counts("scenario")["tombstones"] >= 1


def test_empty_projection_build_and_delete_are_non_creating_after_v2_init(tmp_path: Path) -> None:
    ContentStore(tmp_path)
    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    service = ProjectionBuildService(tmp_path)
    scope = _projection_scope(tmp_path, "agent-a", "group-a")
    built = service.build(mode="reconstructed", scope=scope, runtime_role="test")
    assert built["status"] == "NO_SOURCE"
    first_delete = service.delete(mode="reconstructed", scope=scope)
    second_delete = service.delete(mode="reconstructed", scope=scope)
    assert first_delete["deleted"] is True
    assert first_delete["tombstone_id"]
    assert second_delete["deleted"] is False
    assert second_delete["tombstone_id"] == first_delete["tombstone_id"]


def test_release_plan_is_bound_to_exact_scope(tmp_path: Path) -> None:
    _seed_atom(tmp_path, agent="agent-a", group="group-a", memory_id="a1", body="body")
    service = ProjectionBuildService(tmp_path)
    scope_a = _projection_scope(tmp_path, "agent-a", "group-a")
    scope_b = _projection_scope(tmp_path, "agent-b", "group-b")
    service.build(mode="reconstructed", scope=scope_a, runtime_role="test")
    release = V2ReleaseService(tmp_path)
    target = release.resolve_target(scope=scope_a, target_path="published/memoryguard.json")
    plan = release.create_plan(str(target), scope=scope_a, mode="reconstructed", runtime_role="test")
    with pytest.raises(ProjectionBuildError, match="release_plan_scope_mismatch"):
        release.apply(plan["plan_id"], str(target), scope=scope_b, confirmed=True, runtime_role="test")


def test_release_target_cannot_escape_workspace(tmp_path: Path) -> None:
    scope = _projection_scope(tmp_path, "agent-a", "group-a")
    with pytest.raises(ProjectionBuildError, match="release_target_root_required"):
        V2ReleaseService(tmp_path).resolve_target(scope=scope, target_path=str(tmp_path.parent / "outside.json"))


def test_projection_store_acl_rejects_foreign_scope_reads(tmp_path: Path) -> None:
    _seed_atom(tmp_path, agent="agent-a", group="group-a", memory_id="a1", body="private")
    service = ProjectionBuildService(tmp_path)
    own = _projection_scope(tmp_path, "agent-a", "group-a")
    foreign = _projection_scope(tmp_path, "agent-b", "group-a")
    service.build(mode="reconstructed", scope=own, runtime_role="test")
    key = service._scope_key("reconstructed", own)
    store = ProjectionStore(tmp_path, initialize=False)
    assert store.get_projection("scenario", key, scope=own) is not None
    assert store.get_projection("scenario", key, scope=foreign) is None


def test_source_selection_is_read_only_and_does_not_widen_scope(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("notes", encoding="utf-8")
    control = SourceControlService(tmp_path)
    created = control.add(str(source), "selected_file", {"admin": True, "agent_instance_id": "agent-a"})
    before = control.list_sources({"agent_instance_id": "agent-b"})
    assert before["sources"] == []
    assert control.preview_path(str(source), {"agent_instance_id": "agent-a"})["root_id"] == created["source_id"]
    with pytest.raises(SourceControlError) as exc_info:
        control.preview_path(str(source), {"agent_instance_id": "agent-b"})
    assert exc_info.value.code == "no_source"


def test_group_scope_state_is_binding_backed(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    first = service.bind_agent("agent-a", "shared-team")
    own = service.set_scope("agent-a", {"mode": "share_group", "share_group_id": "shared-team"})
    assert own["scope"]["share_group_id"] == "shared-team"
    assert service.scope_state("agent-a")["scope"]["share_group_id"] == "shared-team"
    moved = service.bind_agent("agent-a", "shared-other")
    assert moved["binding_id"] != first["binding_id"]
    assert service.scope_state("agent-a")["scope"]["share_group_id"] == "shared-team"


def test_v2_projection_payload_has_reference_only_hydration_fields(tmp_path: Path) -> None:
    _seed_atom(
        tmp_path,
        agent="agent-a",
        group="group-a",
        memory_id="m1",
        body="sensitive body stays in MemoryAtomStore",
        metadata={"scope": "project", "title": "Hydrated title", "source_key": "notes.md", "source_locator": "L1"},
    )
    service = ProjectionBuildService(tmp_path)
    scope = _projection_scope(tmp_path, "agent-a", "group-a")
    service.build(mode="reconstructed", scope=scope, runtime_role="test")
    record = _projection_record(tmp_path, service, scope)
    assert record is not None
    encoded = str(record.payload)
    assert "sensitive body stays in MemoryAtomStore" not in encoded
    graph = _derived_graph(record)
    node = next(node for node in graph["nodes"] if node.get("memory_id") == "m1")
    hydrated = MemoryAtomStore(tmp_path, readonly=True).get_atom("m1", scope=_read_scope(tmp_path, "agent-a", "group-a"))
    assert node["label"] == "Hydrated title"
    assert hydrated is not None
    assert hydrated.body == "sensitive body stays in MemoryAtomStore"
