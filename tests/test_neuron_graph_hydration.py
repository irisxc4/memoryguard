"""V2 projection graph hydration tests."""
from __future__ import annotations

import hashlib
from pathlib import Path

from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


def _scope(tmp_path: Path, agent: str = "agent-hydrate", group: str = "group-hydrate") -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        share_group_id=group,
        sensitivity="normal",
        policy_class="private",
    )


def _read_scope(tmp_path: Path, agent: str = "agent-hydrate", group: str = "group-hydrate") -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id=agent,
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        runtime_role="test",
        share_group_id=group,
    )


def _seed(tmp_path: Path, memory_id: str, body: str, title: str, *, related: list[str] | None = None) -> None:
    ContentStore(tmp_path)
    memory = MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory)
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-hydrate",
        project_ref=str(tmp_path.resolve()),
        provider="pytest",
        runtime_role="test",
        share_group_id="group-hydrate",
        actor="hydration-test",
        authority="manual",
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    evidence, _ = governance.put_evidence(
        context=context,
        reason="projection hydration fixture",
        source_ref=f"fixture:{memory_id}",
        digest=digest,
        authority="governance",
        evidence_type="reference",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            confidence=0.9,
            workspace_id=str(tmp_path.resolve()),
            agent_instance_id="agent-hydrate",
            project_ref=str(tmp_path.resolve()),
            provider="pytest",
            runtime_role="test",
            share_group_id="group-hydrate",
            metadata={
                "scope": "project",
                "title": title,
                "source_key": "hydration.md",
                "source_locator": "L1",
                "related_memory_ids": related or [],
            },
            provenance=[{"source_ref": f"fixture:{memory_id}", "source_digest": digest}],
        ),
        context=context,
        evidence=[evidence.to_dict()],
        reason="projection hydration atom",
        idempotency_key=f"hydration:{memory_id}",
    )
    for _ in range(4):
        if memory.project_evidence(governance.evidence).get("pending", 0) == 0:
            break
    memory.set_visibility("active", atom_ids=[atom.atom_id])


def _record(tmp_path: Path, scope: ProjectionReadScope):
    service = ProjectionBuildService(tmp_path)
    service.build(mode="reconstructed", scope=scope, runtime_role="test")
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


def test_v2_projection_graph_hydrates_claims_from_scoped_memory_atoms(tmp_path: Path) -> None:
    _seed(tmp_path, "m1", "正文内容只在 MemoryAtomStore 中", "正文标题")
    _seed(tmp_path, "m2", "关联正文", "关联标题", related=["m1"])
    scope = _scope(tmp_path)
    record = _record(tmp_path, scope)
    assert record is not None
    graph = _derived_graph(record)
    nodes = {node.get("memory_id"): node for node in graph["nodes"] if node.get("memory_id")}
    assert nodes["m1"]["label"] == "正文标题"
    assert nodes["m2"]["label"] == "关联标题"
    assert any(
        edge.get("edge_type") == "related"
        and {edge.get("source"), edge.get("target")} == {
            nodes["m1"]["id"], nodes["m2"]["id"]
        }
        for edge in graph["edges"]
    )
    assert "正文内容只在 MemoryAtomStore 中" not in str(graph)
    hydrated = MemoryAtomStore(tmp_path, readonly=True).get_atom("m1", scope=_read_scope(tmp_path))
    assert hydrated is not None
    assert hydrated.body == "正文内容只在 MemoryAtomStore 中"


def test_v2_projection_hydration_is_scope_fail_closed(tmp_path: Path) -> None:
    _seed(tmp_path, "m1", "private body", "Private title")
    scope = _scope(tmp_path)
    record = _record(tmp_path, scope)
    assert record is not None
    foreign = _read_scope(tmp_path, agent="agent-other", group="group-other")
    assert MemoryAtomStore(tmp_path, readonly=True).get_atom("m1", scope=foreign) is None
    foreign_projection_scope = _scope(tmp_path, agent="agent-other", group="group-other")
    key = ProjectionBuildService(tmp_path)._scope_key("reconstructed", scope)
    assert ProjectionStore(tmp_path, initialize=False).get_projection(
        "scenario", key, scope=foreign_projection_scope,
    ) is None
