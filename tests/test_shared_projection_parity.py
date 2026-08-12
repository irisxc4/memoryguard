"""V2 共享组投影图的确定性拓扑回归。"""
from __future__ import annotations

from pathlib import Path

from memoryguard.content.store import ContentStore, stable_id
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.projection_build import ProjectionBuildService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _ensure_v2_workspace(root: Path) -> None:
    manager = ManifestManager(root)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    initialize_all(WorkspaceV2Layout(root))
    MemoryAtomStore(root)
    EvidenceStore(root)
    ProjectionStore(root)
    ContentStore(root)
    GovernanceV2(
        root,
        memory_store=MemoryAtomStore(root),
        evidence_store=EvidenceStore(root),
    )
    manager.transition(ManifestState.V2_BUILDING, migration_id="shared-projection-v2-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="shared-projection-source",
        target_digest="shared-projection-target",
        manifest_digest="shared-projection-manifest",
        digests={"validator_passed": True, "checkpoints": {"projection": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _bind_group(root: Path, group_id: str) -> None:
    _ensure_v2_workspace(root)
    result = GroupControlService(root, write=True).bind_agent("agent-a", group_id)
    assert result["ok"] is True


def _scope(root: Path, group_id: str) -> ProjectionReadScope:
    resolved = str(root.resolve())
    return ProjectionReadScope(
        workspace_id=resolved,
        agent_instance_id="agent-a",
        project_ref=resolved,
        provider="gui",
        share_group_id=group_id,
        sensitivity="normal",
        policy_class="private",
    )


def _mutation_context(root: Path, group_id: str) -> V2MutationContext:
    resolved = str(root.resolve())
    return V2MutationContext(
        workspace_id=resolved,
        share_group_id=group_id,
        agent_instance_id="agent-a",
        project_ref=resolved,
        provider="gui",
        runtime_role="gui",
        actor="agent-a",
        authority="manual",
    )


def _seed_atoms(root: Path, group_id: str, specs: list[dict]) -> None:
    _ensure_v2_workspace(root)
    memory = MemoryAtomStore(root, readonly=False)
    governance = GovernanceV2(root, memory_store=memory)
    context = _mutation_context(root, group_id)
    atom_ids: list[str] = []
    for spec in specs:
        memory_id = str(spec["memory_id"])
        evidence, _ = governance.put_evidence(
            context=context,
            reason="shared projection V2 fixture evidence",
            source_ref=f"fixture:{memory_id}",
            digest=(memory_id.encode("utf-8").hex() * 64)[:64].ljust(64, "0"),
            authority="governance",
            evidence_type="reference",
        )
        atom, _ = governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=str(spec.get("body") or memory_id),
                kind=str(spec.get("kind") or "fact"),
                workspace_id=str(root.resolve()),
                share_group_id=group_id,
                agent_instance_id="agent-a",
                project_ref=str(root.resolve()),
                provider="gui",
                runtime_role="gui",
                provenance=list(spec.get("provenance") or []),
                metadata=dict(spec.get("metadata") or {}),
            ),
            context=context,
            evidence=[evidence.to_dict()],
            reason="shared projection V2 fixture atom",
            idempotency_key=f"shared-projection-fixture:{memory_id}",
        )
        atom_ids.append(atom.atom_id)

    for _ in range(4):
        state = memory.project_evidence(governance.evidence)
        if int(state.get("pending", 0)) == 0:
            break
    assert memory.pending_outbox(include_failed=True) == []
    memory.set_visibility("active", atom_ids=atom_ids)


def _build_graph(root: Path, group_id: str) -> tuple[dict, dict, dict]:
    service = ProjectionBuildService(root)
    scope = _scope(root, group_id)
    result = service.build(mode="reconstructed", scope=scope, runtime_role="gui")
    assert result["status"] == "succeeded", result
    key = service._scope_key("reconstructed", scope)
    record = ProjectionStore(root).get_projection("scenario", key, scope=scope)
    assert record is not None
    metadata = dict(record.payload.get("metadata") or {})
    graph = dict(metadata.get("derived_graph") or {})
    stats = dict(metadata.get("derived_stats") or {})
    return result, graph, stats


def test_shared_graph_missing_group(tmp_path: Path) -> None:
    _ensure_v2_workspace(tmp_path)
    group_id = "no-such-group"
    assert not GroupControlService(tmp_path).active_binding_for_agent("agent-a")
    service = ProjectionBuildService(tmp_path)
    current = service.current(mode="reconstructed", scope=_scope(tmp_path, group_id))
    assert current["status"] == "succeeded"
    assert current["projection"] is None
    result = service.build(mode="reconstructed", scope=_scope(tmp_path, group_id))
    assert result["status"] == "NO_SOURCE"
    assert result["atom_count"] == 0


def test_shared_graph_reuses_builder_beauty(tmp_path: Path) -> None:
    gid = "parity-group"
    _bind_group(tmp_path, gid)
    _seed_atoms(tmp_path, gid, [
        {
            "memory_id": "m-fact-1",
            "body": "Python 用异步处理 IO 密集任务，注意事件循环。",
            "metadata": {
                "scope": "shared",
                "source_key": "src-doc-a",
                "source_locator": "h1",
            },
            "provenance": [{"source_object_id": "src-doc-a", "locator": "h1"}],
        },
        {
            "memory_id": "m-fact-2",
            "body": "Python 异步处理 IO 密集时别阻塞事件循环。",
            "metadata": {
                "scope": "shared",
                "source_key": "src-doc-a",
                "source_locator": "h2",
                "related_memory_ids": ["m-fact-1"],
            },
            "provenance": [{"source_object_id": "src-doc-a", "locator": "h2"}],
        },
        {
            "memory_id": "m-pref-1",
            "body": "偏好短句说明异步约束。",
            "kind": "preference",
            "metadata": {
                "scope": "shared",
                "source_key": "src-doc-a",
                "source_locator": "h3",
            },
            "provenance": [{"source_object_id": "src-doc-a", "locator": "h3"}],
        },
        {
            "memory_id": "m-alone",
            "body": "完全无关的独立记忆条目关于数据库备份策略。",
            "metadata": {
                "scope": "shared",
                "source_key": "src-other",
                "source_locator": "h1",
            },
            "provenance": [{"source_object_id": "src-other", "locator": "h1"}],
        },
    ])

    result, graph, stats = _build_graph(tmp_path, gid)
    assert result["projection"]["kind"] == "scenario"
    assert result["projection"]["status"] == "ready"
    assert graph["root_id"] == "main"
    kinds = {node["node_kind"] for node in graph["nodes"]}
    labels = {node["label"] for node in graph["nodes"]}
    assert {"root", "topic", "claim_anchor", "source_hub"} <= kinds
    assert "记忆胞体" in labels
    assert "共享来源" in labels
    assert "事实" in labels
    assert "偏好" in labels
    hubs = [node for node in graph["nodes"] if node["node_kind"] == "source_hub"]
    assert any(node["source_key"] == "src-doc-a" and len(node["member_ids"]) == 2 for node in hubs)
    edge_types = {edge["edge_type"] for edge in graph["edges"]}
    assert {"derived_from", "related"} <= edge_types
    assert stats["source_hub_count"] >= 1
    assert stats["related_edge_count"] >= 1


def test_shared_empty_group_has_no_v2_projection(tmp_path: Path) -> None:
    gid = "empty-group"
    _bind_group(tmp_path, gid)
    service = ProjectionBuildService(tmp_path)
    scope = _scope(tmp_path, gid)
    result = service.build(mode="reconstructed", scope=scope, runtime_role="gui")
    assert result["status"] == "NO_SOURCE"
    assert result["atom_count"] == 0
    assert service.current(mode="reconstructed", scope=scope)["projection"] is None


def test_shared_graph_derives_scope_from_import_root_instead_of_forcing_project(
    tmp_path: Path,
) -> None:
    gid = "scope-origin-group"
    _bind_group(tmp_path, gid)
    source_file = tmp_path / "user_profile.md"
    source_file.write_text("用户偏好", encoding="utf-8")
    source_id = stable_id(
        "agent-source", "agent-a", str(source_file.resolve()),
    )
    ContentStore(tmp_path).upsert_source_connector(
        source_id=source_id,
        provider="agent-native",
        source_type="file",
        external_root_key=str(source_file.resolve()),
        workspace_id=str(tmp_path.resolve()),
        enabled=True,
    )
    selection = GroupControlService(tmp_path, write=True).record_selection(
        "agent-a", [source_id], "scope-origin-selection",
    )
    assert selection["ok"] is True
    _seed_atoms(tmp_path, gid, [{
        "memory_id": "scope-memory",
        "body": "用户偏好",
        "metadata": {
            "scope": "user",
            "source_key": source_id,
            "source_locator": "profile:user",
        },
        "provenance": [{"source_object_id": source_id, "locator": "profile:user"}],
    }])

    _result, graph, _stats = _build_graph(tmp_path, gid)
    labels = {node["label"] for node in graph["nodes"]}
    assert "用户来源" in labels
    assert "项目来源" not in labels


def test_share_get_neuron_graph_hydrates_related_jumps(tmp_path: Path) -> None:
    """V2 projection graph exposes claim anchors, hubs and related jumps."""
    gid = "jump-group"
    _bind_group(tmp_path, gid)
    _seed_atoms(tmp_path, gid, [
        {
            "memory_id": "jump-a",
            "body": "Python 异步事件循环不要阻塞。",
            "metadata": {
                "scope": "shared",
                "source_key": "src-same",
                "source_locator": "h1",
            },
            "provenance": [{"source_object_id": "src-same", "locator": "h1"}],
        },
        {
            "memory_id": "jump-b",
            "body": "Python 异步处理 IO 时别阻塞事件循环。",
            "metadata": {
                "scope": "shared",
                "source_key": "src-same",
                "source_locator": "h2",
                "related_memory_ids": ["jump-a"],
            },
            "provenance": [{"source_object_id": "src-same", "locator": "h2"}],
        },
    ])

    _result, graph, stats = _build_graph(tmp_path, gid)
    claims = [node for node in graph["nodes"] if node["node_kind"] == "claim_anchor"]
    hubs = [node for node in graph["nodes"] if node["node_kind"] == "source_hub"]
    assert len(claims) == 2
    assert hubs and set(hubs[0]["member_ids"]) == {node["id"] for node in claims}
    assert any(edge["edge_type"] == "related" for edge in graph["edges"])
    assert stats["related_edge_count"] == 1


def test_shared_graph_hubs_from_event_metadata(tmp_path: Path) -> None:
    """V2 provenance/source metadata derives same-source hubs."""
    gid = "import-hub-group"
    _bind_group(tmp_path, gid)
    source_id = "root-demo"
    specs = []
    for index, body in enumerate([
        "## 为什么需要 MemoryGuard\n本地治理。",
        "## 它是什么\n边界说明。",
        "路线图\n后续计划。",
    ], start=1):
        specs.append({
            "memory_id": f"m-import-{index}",
            "body": body,
            "metadata": {
                "scope": "shared",
                "source_key": source_id,
                "source_locator": f"heading:{index}",
            },
            "provenance": [{
                "source_object_id": source_id,
                "locator": f"heading:{index}",
            }],
        })
    _seed_atoms(tmp_path, gid, specs)

    _result, graph, stats = _build_graph(tmp_path, gid)
    hubs = [node for node in graph["nodes"] if node["node_kind"] == "source_hub"]
    assert hubs
    assert any(node["label"] == source_id and len(node["member_ids"]) == 3 for node in hubs)
    assert stats["source_hub_count"] >= 1
