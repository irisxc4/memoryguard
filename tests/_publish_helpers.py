"""发布/投影测试共用：显式 agent scope + native root + ManagedStore。"""
from __future__ import annotations

from pathlib import Path

from memoryguard.governance_scope import grant_root_to_agent
from memoryguard.gui import GovernanceApi
from memoryguard.managed_store import ManagedStore
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import Provenance, SourceRootType, stable_hash
from memoryguard.source_registry import ScanBudget, SourceRegistry

DEFAULT_AGENT = "agent-test"


def prepare_publish_target(
    workspace: Path,
    target: Path,
    ir: MemoryIR,
    *,
    agent_id: str = DEFAULT_AGENT,
    ownership: str = "agent_managed",
    target_role: str = "takeover_input",
    source_category: str = "native_memory",
) -> tuple[GovernanceApi, str, dict]:
    """注册 native root、授权 agent、写入带 provenance 的 IR + ManagedStore。"""
    workspace.mkdir(parents=True, exist_ok=True)
    target = Path(target)
    if target.suffix:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text("# Memory\n\n", encoding="utf-8")
        root_path = target
        root_type = SourceRootType.SELECTED_FILE
    else:
        target.mkdir(parents=True, exist_ok=True)
        root_path = target
        root_type = SourceRootType.SELECTED_DIRECTORY
        source_category = source_category if source_category != "native_memory" else "project_memory"

    reg = SourceRegistry(workspace)
    root = reg.add(str(root_path), root_type, display_name=root_path.name)
    root.source_category = source_category
    root.ownership = ownership
    root.target_role = target_role
    root.enabled = True
    grant_root_to_agent(root, agent_id)
    reg._save()

    snap = reg.scan(ScanBudget())
    obj = next((o for o in snap.source_objects if o.source_root_id == root.root_id), None)
    if obj is not None:
        oid = obj.source_object_id
    elif root_type == SourceRootType.SELECTED_FILE:
        from memoryguard.source_registry import normalize_rel_path
        oid = stable_hash(root.root_id, normalize_rel_path(root_path.name))
    else:
        from memoryguard.source_registry import normalize_rel_path
        oid = stable_hash(root.root_id, normalize_rel_path("memory.md"))
    for rec in ir.records:
        rec.provenance = [Provenance(
            source_object_id=oid,
            locator="L1",
            excerpt_hash=stable_hash(rec.memory_id, oid),
        )]
    MemoryNormalizer(workspace).save(ir)

    store = ManagedStore(workspace, agent_id)
    if store.get_active_version_id() is None:
        store.create_initial_version(list(ir.records))
    else:
        store.sync_records_from_ir(list(ir.records), notes="test prepare")
    scope = {"mode": "agent", "agent_instance_id": agent_id, "share_group_id": ""}
    api = GovernanceApi(str(workspace))
    return api, root.root_id, scope


def publish(
    api: GovernanceApi,
    *,
    scope: dict,
    target_root_id: str,
    target_file: str = "",
    use_distilled: bool = True,
) -> dict:
    return api.publish_reconstructed_memory(
        target_file,
        True,
        use_distilled,
        scope,
        scope["agent_instance_id"],
        target_root_id,
    )
