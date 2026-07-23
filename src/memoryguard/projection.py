"""Projection：神经图纯投影（spec §7.3, §10）。

v3 核心纠偏：
- 神经图是 Memory IR 的可视化投影，不是事实数据库
- get_neuron_graph() 只读取已有投影
- 未构建时返回 {empty: true, reason: "not_built"}
- build_projection() 需用户确认
- 删除 projection/neuron.json 后可从 IR + DecisionLog 完整重建
- 不做自动晋升/凋亡/基于 use_count 的强结论（spec §7.3）

与 v2.1 light_graph.py 的区别：
- light_graph.py 是事实源，自动挂载+晋升+凋亡
- Projection 只读 IR + decisions，生成投影 JSON
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .memory_ir import MemoryIR
from .schema_v3 import DuplicateDecision, MemoryStatus, stable_hash, _now_iso


# ---------------------------------------------------------------------------
# 投影数据结构（供 Cytoscape.js 消费）
# ---------------------------------------------------------------------------


@dataclass
class ProjectionNode:
    """神经图节点。"""
    id: str
    parent_id: str
    label: str
    node_kind: str  # root/topic/claim_anchor
    status: str = "confirmed"  # 永远 confirmed，不再 tentative（无自动晋升）
    memory_id: str = ""
    kind: str = ""  # MemoryKind
    provenance_count: int = 0
    bg: str = "#5b8def"
    title: str = ""
    body: str = ""
    original_title: str = ""
    original_body: str = ""
    display_language: str = "zh"
    scope: str = ""
    confidence: float = 0.0
    completeness: str = ""
    cluster_count: int = 0
    member_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "parent_id": self.parent_id, "label": self.label,
            "node_kind": self.node_kind, "status": self.status,
            "memory_id": self.memory_id, "kind": self.kind,
            "provenance_count": self.provenance_count, "bg": self.bg,
            "title": self.title, "body": self.body,
            "original_title": self.original_title, "original_body": self.original_body,
            "display_language": self.display_language, "scope": self.scope,
            "confidence": self.confidence, "completeness": self.completeness,
            "cluster_count": self.cluster_count, "member_ids": list(self.member_ids),
        }


@dataclass
class ProjectionEdge:
    """神经图边。"""
    id: str
    source: str
    target: str
    edge_type: str = "derived_from"  # derived_from/duplicate/conflict

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "target": self.target,
            "edge_type": self.edge_type,
        }


@dataclass
class NeuronProjection:
    """神经图投影。"""
    snapshot_id: str = ""
    built_at: str = ""
    nodes: list[ProjectionNode] = field(default_factory=list)
    edges: list[ProjectionEdge] = field(default_factory=list)
    # v3.1 §6.3：确定性内容哈希（不含 built_at），用于验证重建一致性
    content_hash: str = ""
    # v3.1 §6.3 七项状态 meta：Agent 实例 / Profile / 规范版本 / Release / 接管状态 / 覆盖状态 / 漂移
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # v3.1 §6.3：统一 v3 图契约，并附 stats 给前端展示
        node_count = len(self.nodes)
        edge_count = len(self.edges)
        # 按 node_kind 统计
        kind_counts: dict[str, int] = {}
        provenance_total = 0
        for n in self.nodes:
            kind_counts[n.node_kind] = kind_counts.get(n.node_kind, 0) + 1
            provenance_total += n.provenance_count
        return {
            "snapshot_id": self.snapshot_id, "built_at": self.built_at,
            "content_hash": self.content_hash,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "stats": {
                "node_count": node_count,
                "edge_count": edge_count,
                "root_count": kind_counts.get("root", 0),
                "topic_count": kind_counts.get("topic", 0),
                "claim_anchor_count": kind_counts.get("claim_anchor", 0),
                "provenance_total": provenance_total,
            },
            "meta": dict(self.meta),
        }


# ---------------------------------------------------------------------------
# 颜色映射
# ---------------------------------------------------------------------------

KIND_COLORS = {
    "preference": "#e6a23c",   # 橙
    "fact": "#409eff",         # 蓝
    "project": "#67c23a",      # 绿
    "episode": "#f56c6c",      # 红
    "procedure": "#909399",    # 灰
}


# ---------------------------------------------------------------------------
# ProjectionBuilder：从 IR + decisions 重建投影
# ---------------------------------------------------------------------------


class ProjectionBuilder:
    """从 Memory IR + DecisionLog 重建神经图投影。"""

    def __init__(self, workspace: str | Path, mode: str = "reconstructed"):
        self.workspace = Path(workspace).resolve()
        mode_name = "native" if mode == "native" else "reconstructed"
        self.mode = mode_name
        self.proj_path = self.workspace / ".memoryguard" / "projections" / f"{mode_name}.json"

    def build(self, ir: MemoryIR, meta: dict[str, Any] | None = None) -> NeuronProjection:
        """从 IR 构建投影。不自动晋升/凋亡。

        v3.1 §6.3：meta 携带 7 项状态信息（Agent 实例 / Profile / 规范版本 /
        Release / 接管状态 / 覆盖状态 / 漂移），由调用方聚合后传入。
        """
        nodes: list[ProjectionNode] = []
        edges: list[ProjectionEdge] = []

        # root
        root_id = "main"
        nodes.append(ProjectionNode(
            id=root_id, parent_id="", label="Memory Root", node_kind="root",
        ))

        records_by_id = {rec.memory_id: rec for rec in ir.records if rec.status != MemoryStatus.REJECTED}
        cluster_members: dict[str, list[str]] = {}
        clustered_ids: set[str] = set()
        for grp in ir.duplicate_groups:
            if grp.decision == DuplicateDecision.KEEP_ALL:
                continue
            members = [mid for mid in grp.member_ids if mid in records_by_id]
            if len(members) < 2:
                continue
            kinds = {records_by_id[mid].kind.value for mid in members}
            if len(kinds) != 1:
                continue
            cluster_id = "cluster-" + stable_hash(grp.group_id, *members)[:12]
            cluster_members[cluster_id] = members
            clustered_ids.update(members)

        kind_groups: dict[str, list[str]] = {}
        for rec in records_by_id.values():
            kind_groups.setdefault(rec.kind.value, []).append(rec.memory_id)

        for kind, record_ids in kind_groups.items():
            topic_id = "topic-" + kind
            nodes.append(ProjectionNode(
                id=topic_id, parent_id=root_id, label=kind, node_kind="topic",
                bg=KIND_COLORS.get(kind, "#5b8def"),
            ))
            for cluster_id, member_ids in cluster_members.items():
                if records_by_id[member_ids[0]].kind.value != kind:
                    continue
                member_records = [records_by_id[mid] for mid in member_ids]
                primary = member_records[0]
                title = primary.title or primary.memory_id[:8]
                body = "\n".join(
                    f"- {(rec.title or rec.memory_id[:8])}: {rec.body[:160]}"
                    for rec in member_records
                )
                provenance_count = sum(len(rec.provenance) for rec in member_records)
                avg_confidence = sum(rec.confidence for rec in member_records) / len(member_records)
                nodes.append(ProjectionNode(
                    id=cluster_id, parent_id=topic_id, label=f"{title} 等 {len(member_records)} 条",
                    node_kind="duplicate_cluster", memory_id=primary.memory_id,
                    kind=kind, provenance_count=provenance_count,
                    bg=KIND_COLORS.get(kind, "#5b8def"),
                    title=f"相似片段组：{title}", body=body,
                    original_title=primary.original_title, original_body=primary.original_body,
                    display_language=primary.display_language, scope=primary.scope,
                    confidence=avg_confidence, completeness=primary.completeness.value,
                    cluster_count=len(member_records), member_ids=member_ids,
                ))
                edges.append(ProjectionEdge(
                    id="e-" + stable_hash(cluster_id, topic_id),
                    source=topic_id, target=cluster_id,
                    edge_type="derived_from",
                ))
            for rid in record_ids:
                if rid in clustered_ids:
                    continue
                rec = records_by_id.get(rid)
                if not rec:
                    continue
                node_id = "claim-" + rid[:12]
                label = rec.title[:40] if rec.title else rec.memory_id[:8]
                nodes.append(ProjectionNode(
                    id=node_id, parent_id=topic_id, label=label,
                    node_kind="claim_anchor", memory_id=rid,
                    kind=rec.kind.value, provenance_count=len(rec.provenance),
                    bg=KIND_COLORS.get(rec.kind.value, "#5b8def"),
                    title=rec.title, body=rec.body,
                    original_title=rec.original_title, original_body=rec.original_body,
                    display_language=rec.display_language, scope=rec.scope,
                    confidence=rec.confidence, completeness=rec.completeness.value,
                ))
                edges.append(ProjectionEdge(
                    id="e-" + stable_hash(node_id, topic_id),
                    source=topic_id, target=node_id,
                    edge_type="derived_from",
                ))

        projection_meta = dict(meta or {})
        projection_meta["projection_mode"] = self.mode
        proj = NeuronProjection(
            snapshot_id=ir.snapshot_id, built_at=_now_iso(),
            nodes=nodes, edges=edges,
            meta=projection_meta,
        )
        # v3.1 §6.3：确定性 content_hash（不含 built_at 和 meta，仅基于图结构）
        proj.content_hash = self._compute_content_hash(proj)
        return proj

    def _compute_content_hash(self, proj: NeuronProjection) -> str:
        """计算投影内容哈希（不含 built_at，相同输入产生相同 hash）。"""
        content = json.dumps({
            "snapshot_id": proj.snapshot_id,
            "nodes": [n.to_dict() for n in proj.nodes],
            "edges": [e.to_dict() for e in proj.edges],
        }, sort_keys=True, ensure_ascii=False)
        return stable_hash(content)

    def save(self, proj: NeuronProjection) -> None:
        """持久化到 .memoryguard/projections/neuron.json。"""
        self.proj_path.parent.mkdir(parents=True, exist_ok=True)
        tombstone = self.proj_path.with_suffix(self.proj_path.suffix + ".deleted")
        if tombstone.exists():
            tombstone.unlink()
        self.proj_path.write_text(
            json.dumps(proj.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    def load(self) -> NeuronProjection | None:
        tombstone = self.proj_path.with_suffix(self.proj_path.suffix + ".deleted")
        if tombstone.exists():
            return None
        path = self.proj_path
        if not path.exists() and self.mode == "reconstructed":
            legacy_path = self.workspace / ".memoryguard" / "projections" / "neuron.json"
            if legacy_path.exists():
                path = legacy_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return NeuronProjection(
                snapshot_id=data.get("snapshot_id", ""),
                built_at=data.get("built_at", ""),
                content_hash=data.get("content_hash", ""),
                nodes=[ProjectionNode(**n) for n in data.get("nodes", [])],
                edges=[ProjectionEdge(**e) for e in data.get("edges", [])],
                meta=data.get("meta", {}) if isinstance(data.get("meta"), dict) else {},
            )
        except (OSError, json.JSONDecodeError):
            return None

    def delete(self) -> None:
        """删除投影（可从 IR + decisions 重建）。"""
        self.proj_path.parent.mkdir(parents=True, exist_ok=True)
        if self.proj_path.exists():
            self.proj_path.unlink()
        if self.mode == "reconstructed":
            legacy_path = self.workspace / ".memoryguard" / "projections" / "neuron.json"
            if legacy_path.exists():
                legacy_path.unlink()
        self.proj_path.with_suffix(self.proj_path.suffix + ".deleted").write_text("deleted", encoding="utf-8")

    def get_or_empty(self) -> dict[str, Any]:
        """GUI API 用：未构建时返回 empty 状态。"""
        proj = self.load()
        if proj is None:
            return {"empty": True, "reason": "not_built"}
        return proj.to_dict()
