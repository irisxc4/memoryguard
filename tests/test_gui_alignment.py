"""GUI 权限+只读+脱敏对齐验证。"""
from pathlib import Path
import pytest

from memoryguard.access_context import AccessContext
from memoryguard.assets_v2.store import AssetStore
from memoryguard.codegraph_v2.store import CodeGraphStore
from memoryguard.content.store import ContentStore
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.gui import GovernanceApi
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2.store import ProjectionStore
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.working_memory import RuntimeStore
from memoryguard.skills_v2.store import SkillStore
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
    RuleV2Store(root)
    ProjectionStore(root)
    ContentStore(root)
    RuntimeStore(root)
    CodeGraphStore(root)
    AssetStore(root)
    SkillStore(root)
    GovernanceV2(
        root,
        memory_store=MemoryAtomStore(root),
        evidence_store=EvidenceStore(root),
    )
    manager.transition(ManifestState.V2_BUILDING, migration_id="gui-alignment-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="gui-alignment-source",
        target_digest="gui-alignment-target",
        manifest_digest="gui-alignment-manifest",
        digests={"validator_passed": True, "checkpoints": {"gui": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _gui_api(
    root: Path,
    *,
    admin: bool = True,
    agent: str = "agent-a",
    group: str = "gui-alignment-group",
) -> GovernanceApi:
    _ensure_v2_workspace(root)
    GroupControlService(root, write=True).bind_agent(agent, group)
    access = AccessContext(
        trusted_agent_id=agent,
        is_admin=admin,
        strict_binding=True,
        allow_anon=False,
        session_id=f"gui-alignment-{agent}",
        session_source="transport",
        session_trusted=True,
    )
    return GovernanceApi(str(root), _trusted_access_context=access)


def _seed_v2_memory(root: Path, *, group: str = "gui-alignment-group") -> MemoryAtomStore:
    _ensure_v2_workspace(root)
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    scope = {
        "workspace_id": str(root.resolve()),
        "share_group_id": group,
        "agent_instance_id": "agent-a",
        "project_ref": str(root.resolve()),
        "provider": "gui",
        "runtime_role": "gui",
        "actor": "fixture",
        "authority": "manual",
    }
    atom = MemoryAtom(
        memory_id="r1",
        body="正常 V2 内容",
        kind="fact",
        status="active",
        confidence=1.0,
        locked=False,
        injection_policy="relevant",
        priority=0,
        metadata={},
        workspace_id=scope["workspace_id"],
        share_group_id=group,
        agent_instance_id="agent-a",
        project_ref=scope["project_ref"],
        provider="gui",
        runtime_role="gui",
    )
    persisted, _ = governance.put_atom(
        atom,
        context=scope,
        evidence=[{"source_ref": "fixture:r1", "authority": "governance"}],
        reason="GUI alignment fixture",
        confidence=1.0,
        idempotency_key="gui-alignment-r1",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])
    return memory


def _read_v2_memory(memory: MemoryAtomStore, *, group: str = "gui-alignment-group"):
    return memory.get_atom(
        "r1",
        scope={
            "workspace_id": str(memory.layout.workspace),
            "share_group_id": group,
            "agent_instance_id": "agent-a",
            "project_ref": str(memory.layout.workspace),
            "provider": "gui",
            "runtime_role": "gui",
        },
        include_building=True,
    )


@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")


def test_gui_edit_memory_secret_redacted(tmp_path: Path):
    """GUI edit_memory 脱敏:secret 不入持久层。"""
    group = "gui-alignment-group"
    memory = _seed_v2_memory(tmp_path, group=group)
    api = _gui_api(tmp_path, group=group)
    secret_body = "api_key=sk-gui-edit-test123def456ghi789"

    result = api.edit_memory("r1", secret_body, group)

    assert result["ok"] is True, result
    atom = _read_v2_memory(memory, group=group)
    assert atom is not None
    assert "[REDACTED]" in atom.body
    assert "sk-gui-edit-test123" not in atom.body
    db_path = WorkspaceV2Layout(tmp_path).memory_db
    assert b"sk-gui-edit-test123" not in db_path.read_bytes()


def test_gui_write_ops_require_admin(tmp_path: Path, monkeypatch):
    """GUI 写操作非 admin 被拒。"""
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    group = "gui-alignment-group"
    api = _gui_api(tmp_path, admin=False, group=group)
    for result in (
        api.edit_memory("r1", "body", group),
        api.delete_memory("r1", group),
        api.lock_memory("r1", group),
        api.rollback_memory("v1", group),
        api.resolve_conflict("c1", "r1", group),
        api.release_quarantine("q1", group),
        api.delete_quarantine("q1", group),
        api.unbind_agent("b1"),
    ):
        assert result["ok"] is False, result
        assert result["code"] == "admin_capability_required", result
        assert result["error"] == "admin_capability_required", result


def test_gui_write_ops_rejects_admin_override_forgery(tmp_path: Path, monkeypatch):
    """GUI 写操作不能用请求参数伪造 admin capability。"""
    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    group = "gui-alignment-group"
    memory = _seed_v2_memory(tmp_path, group=group)
    api = _gui_api(tmp_path, admin=False, group=group)

    r = api.lock_memory("r1", group, _admin_override=True)

    assert r["ok"] is False
    assert r["code"] == "admin_capability_required"
    assert r["error"] == "admin_capability_required"
    atom = _read_v2_memory(memory, group=group)
    assert atom is not None and atom.locked is False


def test_gui_readonly_no_side_effects(tmp_path: Path):
    """GUI 只读操作不创建空 group。"""
    group = "gui-readonly-group"
    api = _gui_api(tmp_path, admin=False, group=group)
    memory_db = WorkspaceV2Layout(tmp_path).memory_db
    before = memory_db.read_bytes()

    listed = api.list_memory(share_group_id=group)
    assert listed["ok"] is True, listed
    assert listed["data"] == []
    spoofed_list = api.list_memory(share_group_id="nonexistent-readonly")
    assert spoofed_list["code"] == "context_identity_spoof"

    status = api.get_memory_status(group)
    assert status["ok"] is True
    assert status["data"]["total_records"] == 0
    assert status["data"]["status_counts"] == {}

    snapshot = api.get_governance_snapshot(group)
    assert snapshot["ok"] is True, snapshot
    assert snapshot["data"]["status"] == "READY"
    assert snapshot["data"]["memory"]["total_records"] == 0

    assert memory_db.read_bytes() == before
    assert not (Path(tmp_path) / ".memoryguard" / "shared-memory").exists()


def test_gui_search_memory_readonly(tmp_path: Path):
    """GUI search_memory 用 read_only,不存在返回 error。"""
    group = "gui-search-group"
    api = _gui_api(tmp_path, admin=False, group=group)
    result = api.search_memory("test", share_group_id=group)
    assert result["ok"] is True, result
    assert result["data"] == []
    spoofed = api.search_memory("test", share_group_id="nonexistent-search")
    assert spoofed["code"] == "context_identity_spoof"
    assert not (Path(tmp_path) / ".memoryguard" / "shared-memory").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
