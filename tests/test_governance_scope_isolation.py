"""治理范围隔离回归：单 Agent / 共享组 / 全局 IR / 发布授权。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.governance_scope import (
    GovernanceScope,
    filter_ir_for_agent,
    grant_root_to_agent,
    root_authorizes_agent,
    revoke_root_from_agent,
    scope_storage_key,
    validate_scope,
)
from memoryguard.gui import GovernanceApi
from memoryguard.memory_ir import MemoryIR
from memoryguard.projection import ProjectionBuilder
from memoryguard.schema_v3 import (
    CoverageLedger,
    MemoryKind,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    SourceRoot,
    SourceRootType,
    SourceObject,
    SourceSnapshot,
    stable_hash,
)


def _prov(oid: str) -> Provenance:
    return Provenance(source_object_id=oid, locator="L1", excerpt_hash=stable_hash(oid))


def _record(mid: str, oid: str, title: str) -> MemoryRecord:
    return MemoryRecord(
        memory_id=mid,
        kind=MemoryKind.FACT,
        title=title,
        body=f"body-{title}",
        status=MemoryStatus.ACCEPTED,
        provenance=[_prov(oid)],
    )


def _snapshot(mapping: dict[str, str]) -> SourceSnapshot:
    """mapping: source_object_id -> source_root_id"""
    objs = [
        SourceObject(
            source_object_id=oid,
            source_root_id=rid,
            relative_path=f"{oid}.md",
            content_hash=stable_hash(oid),
            media_type="text/markdown",
        )
        for oid, rid in mapping.items()
    ]
    return SourceSnapshot(
        snapshot_id="snap-iso",
        created_at="2026-01-01T00:00:00Z",
        source_objects=objs,
        coverage=CoverageLedger(source_snapshot_id="snap-iso"),
    )


def test_validate_scope_fail_closed() -> None:
    ok, err = validate_scope(None)
    assert ok is None and err == "missing_governance_scope"
    ok, err = validate_scope({"mode": "agent"})
    assert ok is None and "agent_instance_id" in err


def test_filter_ir_keeps_only_allowed_roots() -> None:
    rec_a = _record("a1", "obj-a", "A")
    rec_b = _record("b1", "obj-b", "B")
    ir = MemoryIR(records=[rec_a, rec_b], snapshot_id="s1")
    snap = _snapshot({"obj-a": "root-a", "obj-b": "root-b"})
    scoped = filter_ir_for_agent(ir, {"root-a"}, snap)
    assert [r.memory_id for r in scoped.records] == ["a1"]
    assert len(ir.records) == 2


def test_project_root_multi_agent_authorization() -> None:
    root = SourceRoot(
        root_id="src-project-default",
        type=SourceRootType.PROJECT_DIRECTORY,
        display_name="proj",
        path="/tmp/proj",
    )
    grant_root_to_agent(root, "agent-a")
    grant_root_to_agent(root, "agent-b")
    assert root_authorizes_agent(root, "agent-a")
    assert root_authorizes_agent(root, "agent-b")
    revoke_root_from_agent(root, "agent-a")
    assert not root_authorizes_agent(root, "agent-a")
    assert root_authorizes_agent(root, "agent-b")


def test_agent_graphs_are_isolated(tmp_path: Path) -> None:
    ir_a = MemoryIR(records=[_record("a1", "oa", "only-a")], snapshot_id="sa")
    ir_b = MemoryIR(records=[_record("b1", "ob", "only-b")], snapshot_id="sb")

    pa = ProjectionBuilder(
        tmp_path, "reconstructed",
        scope_key=scope_storage_key(GovernanceScope(mode="agent", agent_instance_id="agent-a")),
    )
    pb = ProjectionBuilder(
        tmp_path, "reconstructed",
        scope_key=scope_storage_key(GovernanceScope(mode="agent", agent_instance_id="agent-b")),
    )
    pa.save(pa.build(ir_a))
    pb.save(pb.build(ir_b))

    ga = pa.get_or_empty()
    gb = pb.get_or_empty()
    labels_a = {n.get("title") or n.get("label") for n in ga.get("nodes", [])}
    labels_b = {n.get("title") or n.get("label") for n in gb.get("nodes", [])}
    assert any("only-a" in str(x) for x in labels_a)
    assert not any("only-b" in str(x) for x in labels_a)
    assert any("only-b" in str(x) for x in labels_b)
    assert not any("only-a" in str(x) for x in labels_b)


def test_gui_get_neuron_graph_requires_scope(tmp_path: Path) -> None:
    api = GovernanceApi(str(tmp_path))
    out = api.get_neuron_graph()
    assert out.get("empty") is True
    assert out.get("error") == "missing_governance_scope"
    assert out.get("reason") == "missing_governance_scope"


def test_gui_publish_rejects_cross_agent_target(tmp_path: Path) -> None:
    api = GovernanceApi(str(tmp_path))
    from memoryguard.source_registry import SourceRegistry
    reg = SourceRegistry(tmp_path)
    mem = tmp_path / "agent-a-memory.md"
    mem.write_text("# mem\n", encoding="utf-8")
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, display_name="a-mem")
    root.source_category = "native_memory"
    root.ownership = "agent_managed"
    root.target_role = "takeover_input"
    root.enabled = True
    grant_root_to_agent(root, "agent-a")
    reg._save()

    result = api.publish_reconstructed_memory(
        str(mem), True, True,
        {"mode": "agent", "agent_instance_id": "agent-b"},
        "agent-b",
        root.root_id,
    )
    assert "error" in result
    assert result["error"] in {
        "target_root_not_authorized_for_agent",
        "scoped_ir_empty",
        "没有可发布的重构记忆",
    }


def test_preference_is_not_authorization(tmp_path: Path) -> None:
    api = GovernanceApi(str(tmp_path))
    saved = api.set_governance_scope({"mode": "agent", "agent_instance_id": "agent-x"})
    assert saved.get("ok") is True
    out = api.get_neuron_graph()
    assert out.get("empty") is True
    assert "missing_governance_scope" in (out.get("reason") or out.get("error") or "")


def test_scope_storage_key_does_not_collide() -> None:
    from memoryguard.governance_scope import scope_storage_key
    a = scope_storage_key(GovernanceScope(mode="agent", agent_instance_id="agent:a"))
    b = scope_storage_key(GovernanceScope(mode="agent", agent_instance_id="agent?a"))
    assert a != b
    assert a.count("-") >= 2
    assert a.endswith(stable_hash("agent", "agent:a")[:16])
    # 哈希后缀保证不同原始 ID 不碰撞
    assert a.split("-")[-1] != b.split("-")[-1]


def test_resolve_rejects_conflicting_scopes() -> None:
    from memoryguard.governance_scope import resolve_governance_scope
    ok, err = resolve_governance_scope(agent_instance_id="a", share_group_id="g")
    assert ok is None and err == "conflicting_governance_scope"


def test_revoked_authorization_invalidates_projection(tmp_path: Path) -> None:
    from memoryguard.memory_ir import MemoryIR
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord, MemoryStatus, Provenance
    from memoryguard.source_registry import SourceRegistry
    from memoryguard.governance_scope import grant_root_to_agent, revoke_root_from_agent, authorized_roots_digest

    mem = tmp_path / "a.md"
    mem.write_text("x", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, "A")
    root.source_category = "knowledge_source"
    root.enabled = True
    grant_root_to_agent(root, "agent-a")
    reg._save()

    api = GovernanceApi(str(tmp_path))
    # 构建空 IR 投影也会写入 auth digest
    built = api.build_projection(True, "reconstructed", agent_instance_id="agent-a")
    assert "error" not in built or built.get("empty") is not None
    graph = api.get_neuron_graph(agent_instance_id="agent-a")
    # 可能 empty（无记录）但不该是 auth_stale
    assert graph.get("error") != "projection_auth_stale"

    revoke_root_from_agent(root, "agent-a")
    reg._save()
    after = api.get_neuron_graph(agent_instance_id="agent-a")
    assert after.get("empty") is True
    assert after.get("reason") == "not_built"
    assert after.get("error") in {"projection_auth_stale", None}


def test_set_projection_source_enabled_requires_authorization(tmp_path: Path) -> None:
    from memoryguard.source_registry import SourceRegistry
    mem = tmp_path / "a.md"
    mem.write_text("x", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, "A")
    root.enabled = True
    grant_root_to_agent(root, "agent-a")
    reg._save()
    api = GovernanceApi(str(tmp_path))
    denied = api.set_projection_source_enabled(root.root_id, False, agent_instance_id="agent-b")
    assert denied.get("ok") is False
    assert denied.get("error") == "root_not_authorized_for_agent"
    assert SourceRegistry(tmp_path).get(root.root_id).enabled is True


def test_publish_rejects_target_file_mismatch(tmp_path: Path) -> None:
    from memoryguard.managed_store import ManagedStore
    from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord
    from memoryguard.source_registry import SourceRegistry

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root_a = reg.add(str(a), SourceRootType.SELECTED_FILE, "A")
    root_a.source_category = "native_memory"
    root_a.ownership = "agent_managed"
    root_a.target_role = "takeover_input"
    grant_root_to_agent(root_a, "agent-a")
    reg._save()
    ir = MemoryIR(records=[MemoryRecord(memory_id="m1", kind=MemoryKind.FACT, title="t", body="b")], snapshot_id="s")
    MemoryNormalizer(tmp_path).save(ir)
    ManagedStore(tmp_path, "agent-a").create_initial_version(list(ir.records))
    api = GovernanceApi(str(tmp_path))
    result = api.publish_reconstructed_memory(
        str(b), True, True,
        {"mode": "agent", "agent_instance_id": "agent-a"},
        "agent-a",
        root_a.root_id,
    )
    assert result.get("error") == "target_file_mismatch_root"


def test_share_group_delete_returns_not_built(tmp_path: Path) -> None:
    # 无真实 SharedMemoryStore 时 build 会得到 share_group_not_found/empty，但仍写快照
    api = GovernanceApi(str(tmp_path))
    built = api.build_projection(True, share_group_id="sg-demo")
    # 组不存在时仍可能写文件
    deleted = api.delete_projection(True, share_group_id="sg-demo")
    assert deleted.get("ok") is True
    got = api.get_neuron_graph(share_group_id="sg-demo")
    assert got.get("empty") is True
    assert got.get("reason") == "not_built"


def test_publish_blocks_after_root_revoked(tmp_path: Path) -> None:
    from memoryguard.managed_store import ManagedStore
    from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord, Provenance, stable_hash
    from memoryguard.source_registry import SourceRegistry, normalize_rel_path
    from memoryguard.governance_scope import revoke_root_from_agent

    mem = tmp_path / "a.md"
    mem.write_text("hello", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, "A")
    root.source_category = "native_memory"
    root.ownership = "agent_managed"
    root.target_role = "takeover_input"
    grant_root_to_agent(root, "agent-a")
    reg._save()
    oid = stable_hash(root.root_id, normalize_rel_path(mem.name))
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1", kind=MemoryKind.FACT, title="t", body="b",
        provenance=[Provenance(oid, "L1", stable_hash("m1"))],
    )], snapshot_id="s")
    MemoryNormalizer(tmp_path).save(ir)
    ManagedStore(tmp_path, "agent-a").create_initial_version(list(ir.records))
    api = GovernanceApi(str(tmp_path))
    ok = api.publish_reconstructed_memory(
        "", True, True,
        {"mode": "agent", "agent_instance_id": "agent-a"},
        "agent-a", root.root_id,
    )
    assert ok.get("ok") is True
    revoke_root_from_agent(root, "agent-a")
    reg._save()
    denied = api.publish_reconstructed_memory(
        "", True, True,
        {"mode": "agent", "agent_instance_id": "agent-a"},
        "agent-a", root.root_id,
    )
    assert "error" in denied
    assert denied["error"] in {
        "target_root_not_authorized_for_agent",
        "no_authorized_roots",
        "scoped_ir_empty",
    }


def test_create_build_plan_requires_scope_binding(tmp_path: Path) -> None:
    from memoryguard.adapters import GenericMarkdownTarget
    from memoryguard.memory_ir import MemoryIR
    from memoryguard.release_manager import ReleaseManager
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord

    target_dir = tmp_path / "out"
    target_dir.mkdir()
    ir = MemoryIR(records=[MemoryRecord(memory_id="m1", kind=MemoryKind.FACT, title="t", body="b")], snapshot_id="s")
    rm = ReleaseManager(tmp_path)
    try:
        rm.create_build_plan(ir, GenericMarkdownTarget(), target_dir)
        assert False, "expected plan_scope_binding_required"
    except ValueError as exc:
        assert "plan_scope_binding_required" in str(exc)


def test_apply_build_rejects_scope_mismatch(tmp_path: Path) -> None:
    from memoryguard.adapters import GenericMarkdownTarget
    from memoryguard.memory_ir import MemoryIR
    from memoryguard.release_manager import ReleaseManager
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord
    from memoryguard.source_registry import SourceRegistry

    mem = tmp_path / "a.md"
    mem.write_text("x", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, "A")
    grant_root_to_agent(root, "agent-a")
    grant_root_to_agent(root, "agent-b")
    reg._save()
    target_dir = tmp_path / "out"
    target_dir.mkdir()
    ir = MemoryIR(records=[MemoryRecord(memory_id="m1", kind=MemoryKind.FACT, title="t", body="b")], snapshot_id="s")
    rm = ReleaseManager(tmp_path)
    plan = rm.create_build_plan(
        ir, GenericMarkdownTarget(), target_dir,
        governance_scope={"mode": "agent", "agent_instance_id": "agent-a"},
        target_root_id=root.root_id,
    )
    try:
        rm.apply_build(
            plan.plan_id, GenericMarkdownTarget(), target_dir, approval=True,
            expected_scope={"mode": "agent", "agent_instance_id": "agent-b"},
            expected_target_root_id=root.root_id,
        )
        assert False, "expected plan_scope_mismatch"
    except ValueError as exc:
        assert "plan_scope_mismatch" in str(exc)


def test_gui_verify_release_requires_binding(tmp_path: Path) -> None:
    api = GovernanceApi(str(tmp_path))
    denied = api.verify_release("rel-missing")
    assert denied.get("error") in {
        "agent_scope_required", "agent_instance_id_required", "missing_governance_scope",
    }
    denied2 = api.rollback_release("rel-missing", True)
    assert denied2.get("error") in {
        "agent_scope_required", "agent_instance_id_required", "missing_governance_scope",
    }


def test_gui_create_build_plan_requires_agent_scope(tmp_path: Path) -> None:
    api = GovernanceApi(str(tmp_path))
    denied = api.create_build_plan("")
    assert denied.get("error") in {
        "agent_scope_required", "agent_instance_id_required", "missing_governance_scope",
    }


def test_rollback_native_requires_scope_and_rejects_cross_agent(tmp_path: Path) -> None:
    from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
    from memoryguard.schema_v3 import MemoryKind, MemoryRecord
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _publish_helpers import prepare_publish_target, publish

    workspace = tmp_path / "ws"
    target = tmp_path / "native" / "memory.md"
    target.parent.mkdir(parents=True)
    target.write_text("# old\n", encoding="utf-8")
    ir = MemoryIR(records=[MemoryRecord(
        memory_id="m1", kind=MemoryKind.FACT, title="t", body="b",
    )], snapshot_id="s")
    MemoryNormalizer(workspace).save(ir)
    api, root_id, scope = prepare_publish_target(workspace, target, ir, agent_id="agent-a")
    published = publish(api, scope=scope, target_root_id=root_id)
    assert published.get("ok") is True
    denied = api.rollback_native_memory_release(published["release_id"], confirmed=True)
    assert denied.get("error") in {
        "agent_scope_required", "agent_instance_id_required", "missing_governance_scope",
    }
    cross = api.rollback_native_memory_release(
        published["release_id"], confirmed=True,
        scope={"mode": "agent", "agent_instance_id": "agent-b"},
        agent_instance_id="agent-b",
        target_root_id=root_id,
    )
    assert cross.get("error") in {
        "target_root_not_authorized_for_agent",
        "release_scope_mismatch",
    }


def test_nrel_file_root_rejects_sibling_targets(tmp_path: Path) -> None:
    import json
    from memoryguard.source_registry import SourceRegistry

    workspace = tmp_path / "ws"
    workspace.mkdir()
    mem = tmp_path / "native" / "memory.md"
    sibling = tmp_path / "native" / "other.md"
    mem.parent.mkdir(parents=True)
    mem.write_text("a", encoding="utf-8")
    sibling.write_text("b", encoding="utf-8")
    reg = SourceRegistry(workspace)
    root = reg.add(str(mem), SourceRootType.SELECTED_FILE, "A")
    grant_root_to_agent(root, "agent-a")
    reg._save()
    release_id = "nrel-sibling"
    release_dir = workspace / ".memoryguard" / "native_releases" / release_id
    release_dir.mkdir(parents=True)
    (release_dir / "manifest.json").write_text(json.dumps({
        "release_id": release_id,
        "status": "applied",
        "files": [{
            "target_path": str(sibling.resolve()),
            "existed_before": True,
            "backup_path": str(release_dir / "bak"),
            "before_hash": "x",
            "after_hash": "y",
        }],
    }), encoding="utf-8")
    api = GovernanceApi(str(workspace))
    denied = api.rollback_native_memory_release(
        release_id, confirmed=True,
        scope={"mode": "agent", "agent_instance_id": "agent-a"},
        agent_instance_id="agent-a",
        target_root_id=root.root_id,
    )
    assert denied.get("error") == "native_release_not_authorized_for_agent"


def test_validate_release_binding_rejects_path_drift(tmp_path: Path) -> None:
    from memoryguard.release_manager import ReleaseManager

    data = {
        "governance_scope": {"mode": "agent", "agent_instance_id": "agent-a"},
        "target_root_id": "root-1",
        "target_path": str((tmp_path / "a").resolve()),
    }
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    try:
        ReleaseManager.validate_release_binding(
            data,
            expected_scope={"mode": "agent", "agent_instance_id": "agent-a"},
            expected_target_root_id="root-1",
            expected_target_path=tmp_path / "b",
        )
        assert False, "expected release_target_path_mismatch"
    except ValueError as exc:
        assert "release_target_path_mismatch" in str(exc)


def test_shared_takeover_commit_survives_projection_refresh_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """版本快照一旦提交成功，投影刷新异常不能把正式接管误报为整体失败。"""
    from memoryguard.shared_memory_store import SharedMemoryStore

    group_id = "shared-takeover"
    SharedMemoryStore(tmp_path, group_id)
    api = GovernanceApi(str(tmp_path))

    def fail_projection(*args, **kwargs):
        raise RuntimeError("projection unavailable")

    monkeypatch.setattr(api, "build_projection", fail_projection)
    result = api.commit_shared_memory_governance(
        group_id,
        "MCP 正式接管（推荐）",
        True,
        _admin_override=True,
    )

    assert result.get("ok") is True
    assert result.get("version_id")
    assert "projection unavailable" in result.get("projection_warning", "")
