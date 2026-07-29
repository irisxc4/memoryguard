"""共享组神经图与单 Agent ProjectionBuilder 美化对齐。"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.governance_scope import build_shared_memory_graph
from memoryguard.schema_v3 import (
    MemoryKind,
    Provenance,
    SharedMemoryRecord,
    SharedMemoryStatus,
    stable_hash,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _prov(oid: str, locator: str = "L1") -> Provenance:
    return Provenance(source_object_id=oid, locator=locator, excerpt_hash=stable_hash(oid)[:16])


def _rec(
    mid: str,
    body: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    provenance: list[Provenance] | None = None,
) -> SharedMemoryRecord:
    now = _now_iso()
    return SharedMemoryRecord(
        memory_id=mid,
        body=body,
        kind=kind,
        status=SharedMemoryStatus.ACTIVE,
        confidence=0.8,
        provenance=list(provenance or []),
        created_at=now,
        updated_at=now,
        agent_instance_id="agent-a",
    )


def test_shared_graph_missing_group(tmp_path: Path) -> None:
    data = build_shared_memory_graph(tmp_path, "no-such-group")
    assert data["empty"] is True
    assert data["reason"] == "share_group_not_found"
    assert data["projection_kind"] == "shared_memory_projection"


def test_shared_graph_reuses_builder_beauty(tmp_path: Path) -> None:
    gid = "parity-group"
    store = SharedMemoryStore(tmp_path, gid)
    # 同文件两条 fact → source_hub；另有 preference 同源 → shared_source；相似 fact → related
    store.append_record(_rec(
        "m-fact-1", "Python 用异步处理 IO 密集任务，注意事件循环。",
        provenance=[_prov("src-doc-a", "h1")],
    ))
    store.append_record(_rec(
        "m-fact-2", "Python 异步处理 IO 密集时别阻塞事件循环。",
        provenance=[_prov("src-doc-a", "h2")],
    ))
    store.append_record(_rec(
        "m-pref-1", "偏好短句说明异步约束。",
        kind=MemoryKind.PREFERENCE,
        provenance=[_prov("src-doc-a", "h3")],
    ))
    store.append_record(_rec(
        "m-alone", "完全无关的独立记忆条目关于数据库备份策略。",
        provenance=[_prov("src-other", "h1")],
    ))

    data = build_shared_memory_graph(tmp_path, gid)
    assert data["empty"] is False
    assert data["projection_kind"] == "shared_memory_projection"
    assert data["mode"] == "share_group"
    meta = data.get("meta") or {}
    assert meta.get("projection_mode") == "share_group"
    assert meta.get("derivation_engine") == "deterministic_v3_shared"
    assert meta.get("llm_used") is False
    assert meta.get("share_group_id") == gid

    nodes = data["nodes"]
    kinds = {n["node_kind"] for n in nodes}
    labels = {n["label"] for n in nodes}
    assert "root" in kinds
    assert "topic" in kinds
    assert "claim_anchor" in kinds
    assert "source_hub" in kinds, "同 provenance 文件 ≥2 条应有同源突触"
    assert "共享胞体" in labels
    assert "共享项目" in labels

    root = next(n for n in nodes if n["node_kind"] == "root")
    assert root["label"] == "共享胞体"
    assert any("共享胞体" in (n.get("derivation") or "") for n in nodes if n["node_kind"] == "source_hub")

    edge_types = {e["edge_type"] for e in data["edges"]}
    assert "derived_from" in edge_types
    assert "related" in edge_types, "相似 KEEP_ALL 应对齐 related 虚线"
    assert "shared_source" in edge_types, "跨类型同源应对齐 shared_source"

    stats = data.get("stats") or {}
    assert stats.get("source_hub_count", 0) >= 1
    assert stats.get("related_edge_count", 0) >= 1
    assert stats.get("shared_source_edge_count", 0) >= 1


def test_shared_empty_group_still_has_root(tmp_path: Path) -> None:
    gid = "empty-group"
    SharedMemoryStore(tmp_path, gid)  # 建库无记录
    data = build_shared_memory_graph(tmp_path, gid)
    assert data["empty"] is True
    assert data.get("reason") == "share_group_empty"
    assert any(n["node_kind"] == "root" and n["label"] == "共享胞体" for n in data["nodes"])


def test_shared_graph_derives_scope_from_import_root_instead_of_forcing_project(
    tmp_path: Path,
) -> None:
    from memoryguard.schema_v3 import MemoryEvent, SourceRootType
    from memoryguard.source_registry import SourceRegistry

    gid = "scope-origin-group"
    source_file = tmp_path / "user_profile.md"
    source_file.write_text("用户偏好", encoding="utf-8")
    reg = SourceRegistry(tmp_path)
    root = reg.add(
        str(source_file), SourceRootType.SELECTED_FILE, "User profile",
        scope="user",
    )
    root.source_category = "native_memory"
    reg._save()

    store = SharedMemoryStore(tmp_path, gid)
    event = MemoryEvent(
        event_id="scope-event",
        agent_instance_id="agent-a",
        share_group_id=gid,
        raw_content="用户偏好",
        metadata={"source_root_id": root.root_id},
    )
    store.append_event(event)
    store.append_record(_rec(
        "scope-memory", "用户偏好",
        provenance=[_prov(event.event_id)],
    ))

    data = build_shared_memory_graph(tmp_path, gid)
    labels = {node["label"] for node in data["nodes"]}
    assert "用户来源" in labels
    assert "项目来源" not in labels


def test_share_get_neuron_graph_hydrates_related_jumps(tmp_path: Path) -> None:
    """共享组 get_neuron_graph 应从边补 related，供详情点击跳转。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.governance_scope import (
        GovernanceScope, authorized_roots_digest, share_group_projection_path,
    )
    import json

    gid = "jump-group"
    store = SharedMemoryStore(tmp_path, gid)
    store.append_record(_rec(
        "jump-a" + "0" * 10, "Python 异步事件循环不要阻塞。",
        provenance=[_prov("src-same")],
    ))
    store.append_record(_rec(
        "jump-b" + "0" * 10, "Python 异步处理 IO 时别阻塞事件循环。",
        provenance=[_prov("src-same")],
    ))
    graph = build_shared_memory_graph(tmp_path, gid)
    path = share_group_projection_path(
        tmp_path, GovernanceScope(mode="share_group", share_group_id=gid),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # 确保 digest 匹配 get_neuron_graph 校验
    meta = graph.setdefault("meta", {})
    meta["authorized_roots_digest"] = authorized_roots_digest([f"share:{gid}"])
    path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")

    loaded = GovernanceApi(str(tmp_path)).get_neuron_graph(share_group_id=gid)
    assert not loaded.get("empty")
    claims = [n for n in loaded["nodes"] if n.get("node_kind") == "claim_anchor"]
    assert claims
    # 至少一条有 related 或 hub 有 members
    hubs = [n for n in loaded["nodes"] if n.get("node_kind") == "source_hub"]
    assert hubs and hubs[0].get("members")
    assert any(n.get("related") for n in claims) or any(
        e.get("edge_type") in ("related", "shared_source") for e in loaded.get("edges") or []
    )


def test_shared_graph_hubs_from_event_metadata(tmp_path: Path) -> None:
    """旧导入 provenance=event_id 时，应从 events.metadata 还原同文件同源突触。"""
    from memoryguard.schema_v3 import MemoryEvent

    gid = "import-hub-group"
    store = SharedMemoryStore(tmp_path, gid)
    meta = {
        "source_root_id": "root-demo",
        "relative_path": "docs/README.md",
        "locator": "heading:why",
        "extraction_origin": "native_memory_import",
    }
    for i, body in enumerate([
        "## 为什么需要 MemoryGuard\n本地治理。",
        "## 它是什么\n边界说明。",
        "路线图\n后续计划。",
    ], start=1):
        eid = f"evt-import-{i}"
        store.append_event(MemoryEvent(
            event_id=eid,
            agent_instance_id="agent-a",
            share_group_id=gid,
            raw_content=body,
            metadata=dict(meta),
            created_at=_now_iso(),
        ))
        store.append_record(_rec(
            f"m-import-{i}", body,
            provenance=[_prov(eid, f"heading:{i}")],
        ))

    data = build_shared_memory_graph(tmp_path, gid)
    kinds = {n["node_kind"] for n in data["nodes"]}
    assert "source_hub" in kinds
    hubs = [n for n in data["nodes"] if n["node_kind"] == "source_hub"]
    assert any("README" in (n.get("label") or "") for n in hubs)
    assert (data.get("stats") or {}).get("source_hub_count", 0) >= 1
