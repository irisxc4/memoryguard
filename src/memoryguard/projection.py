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

衍生逻辑（确定性，不调 LLM）：
  记忆胞体(root)
    └─ 项目来源树突(topic / scope：project|user|…)
         └─ 类型树突(topic / MemoryKind)
              ├─ 来源突触(source_hub，同文件 ≥2 条时)
              │    └─ 记忆末梢(claim_anchor)
              └─ 记忆末梢(claim_anchor)
  另画：duplicate/related 边（重复候选）、shared_source 边（跨类型同源）
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
    node_kind: str  # root/topic/source_hub/claim_anchor/duplicate_cluster
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
    derivation: str = ""          # 可读衍生路径，如 胞体 → 偏好 → 文件
    source_key: str = ""          # source_object_id（source_hub / claim）
    source_locator: str = ""      # 首个 provenance locator
    edge_hint: str = ""           # 与父节点的连接说明

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
            "derivation": self.derivation, "source_key": self.source_key,
            "source_locator": self.source_locator, "edge_hint": self.edge_hint,
        }


@dataclass
class ProjectionEdge:
    """神经图边。"""
    id: str
    source: str
    target: str
    edge_type: str = "derived_from"  # derived_from/related/duplicate/shared_source
    label: str = ""
    reason: str = ""
    strength: float = 0.4

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "source": self.source, "target": self.target,
            "edge_type": self.edge_type, "label": self.label,
            "reason": self.reason, "strength": self.strength,
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
        edge_type_counts: dict[str, int] = {}
        for e in self.edges:
            edge_type_counts[e.edge_type] = edge_type_counts.get(e.edge_type, 0) + 1
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
                "source_hub_count": kind_counts.get("source_hub", 0),
                "claim_anchor_count": kind_counts.get("claim_anchor", 0),
                "duplicate_cluster_count": kind_counts.get("duplicate_cluster", 0),
                "provenance_total": provenance_total,
                "derived_edge_count": edge_type_counts.get("derived_from", 0),
                "related_edge_count": edge_type_counts.get("related", 0)
                + edge_type_counts.get("duplicate", 0),
                "shared_source_edge_count": edge_type_counts.get("shared_source", 0),
            },
            "meta": dict(self.meta),
        }


# ---------------------------------------------------------------------------
# 颜色 / 标签
# ---------------------------------------------------------------------------

KIND_COLORS = {
    "preference": "#e6a23c",
    "fact": "#409eff",
    "project": "#67c23a",
    "episode": "#f56c6c",
    "procedure": "#909399",
    "correction": "#f687b3",
}

TOPIC_LABELS = {
    "preference": "偏好",
    "fact": "事实",
    "project": "项目",
    "episode": "事件",
    "procedure": "流程",
    "correction": "纠错",
}

# 一级树突：按记忆 scope（项目来源 / 用户来源），不再把 MemoryKind 挂在胞体下
SCOPE_TOPIC_LABELS = {
    "project": "项目来源",
    "user": "用户来源",
    "agent": "Agent 来源",
    "session": "会话来源",
    "share_group": "共享项目",
    "unknown": "未分来源",
}

SCOPE_TOPIC_COLORS = {
    "project": "#3d8bfd",
    "user": "#c084fc",
    "agent": "#38bdf8",
    "session": "#94a3b8",
    "share_group": "#2dd4bf",
    "unknown": "#64748b",
}

EDGE_STRENGTH = {
    "derived_from": 0.62,
    "shared_source": 0.36,
    "duplicate": 0.44,
    "related": 0.28,
}


def _topic_label(kind: str) -> str:
    return TOPIC_LABELS.get(kind, kind or "未知")


def _normalize_scope(scope: str) -> str:
    s = (scope or "").strip().lower() or "project"
    if s in SCOPE_TOPIC_LABELS:
        return s
    return "unknown"


def _scope_topic_label(scope: str) -> str:
    return SCOPE_TOPIC_LABELS.get(_normalize_scope(scope), "未分来源")


def _primary_provenance(rec: Any) -> tuple[str, str]:
    """返回 (source_object_id, locator)。"""
    prov = getattr(rec, "provenance", None) or []
    if not prov:
        return "", ""
    first = prov[0]
    return (
        str(getattr(first, "source_object_id", "") or ""),
        str(getattr(first, "locator", "") or ""),
    )


def _short_source(source_key: str) -> str:
    if not source_key:
        return "未知来源"
    key = source_key
    # 共享组文件同源键：share-file:{root}:{rel} 或 share-file:{rel}
    if key.startswith("share-file:"):
        rest = key[len("share-file:"):]
        path = rest.split(":", 1)[-1].replace("\\", "/")
        base = path.rsplit("/", 1)[-1] or path
        return (base[:18] if len(base) > 18 else base) or "同源文件"
    if "/" in key or "\\" in key:
        base = key.replace("\\", "/").rsplit("/", 1)[-1]
        return base[:18] if base else key[:14]
    if len(key) <= 14:
        return key
    return key[:8] + "…" + key[-4:]


def _make_edge(
    source: str,
    target: str,
    edge_type: str,
    *,
    label: str = "",
    reason: str = "",
) -> ProjectionEdge:
    return ProjectionEdge(
        id="e-" + stable_hash(edge_type, source, target, label)[:16],
        source=source,
        target=target,
        edge_type=edge_type,
        label=label,
        reason=reason,
        strength=EDGE_STRENGTH.get(edge_type, 0.35),
    )


# ---------------------------------------------------------------------------
# ProjectionBuilder：从 IR + decisions 重建投影
# ---------------------------------------------------------------------------


class ProjectionBuilder:
    """从 Memory IR + DecisionLog 重建神经图投影。"""

    def __init__(
        self,
        workspace: str | Path,
        mode: str = "reconstructed",
        *,
        scope_key: str = "",
    ):
        self.workspace = Path(workspace).resolve()
        mode_name = "native" if mode == "native" else "reconstructed"
        self.mode = mode_name
        self.scope_key = (scope_key or "").strip()
        if self.scope_key:
            self.proj_path = (
                self.workspace / ".memoryguard" / "projections" / mode_name / f"{self.scope_key}.json"
            )
        else:
            # 无 scope 的遗留路径仅用于迁移探测，不再作为默认读写目标
            self.proj_path = self.workspace / ".memoryguard" / "projections" / f"{mode_name}.json"

    def build(
        self,
        ir: MemoryIR,
        meta: dict[str, Any] | None = None,
        *,
        root_label: str = "记忆胞体",
        root_body: str = "",
    ) -> NeuronProjection:
        """从 IR 构建投影。不自动晋升/凋亡。确定性衍生，不调用 LLM。

        root_label/root_body：共享组可传「共享胞体」与单 Agent 共用同一套美化衍生。
        """
        nodes: list[ProjectionNode] = []
        edges: list[ProjectionEdge] = []
        claim_node_by_memory: dict[str, str] = {}
        root_name = (root_label or "记忆胞体").strip() or "记忆胞体"
        root_desc = (root_body or "").strip() or (
            "所有主题树突与记忆末梢由此辐射。节点内容来自 Memory IR，图本身不是事实源。"
        )

        root_id = "main"
        nodes.append(ProjectionNode(
            id=root_id, parent_id="", label=root_name, node_kind="root",
            title=root_name,
            body=root_desc,
            derivation=root_name,
            edge_hint="",
        ))

        records_by_id = {
            rec.memory_id: rec
            for rec in ir.records
            if rec.status != MemoryStatus.REJECTED
        }

        # ---- 重复簇（同 scope + 同 kind）----
        cluster_members: dict[str, list[str]] = {}
        clustered_ids: set[str] = set()
        for grp in ir.duplicate_groups:
            if grp.decision == DuplicateDecision.KEEP_ALL:
                continue
            members = [mid for mid in grp.member_ids if mid in records_by_id]
            if len(members) < 2:
                continue
            kinds = {records_by_id[mid].kind.value for mid in members}
            scopes = {_normalize_scope(records_by_id[mid].scope) for mid in members}
            if len(kinds) != 1 or len(scopes) != 1:
                continue
            cluster_id = "cluster-" + stable_hash(grp.group_id, *members)[:12]
            cluster_members[cluster_id] = members
            clustered_ids.update(members)

        # 一级：项目来源 / 用户来源；二级：MemoryKind
        scope_kind_groups: dict[str, dict[str, list[str]]] = {}
        for rec in records_by_id.values():
            scope_key = _normalize_scope(rec.scope)
            kind_key = rec.kind.value
            scope_kind_groups.setdefault(scope_key, {}).setdefault(kind_key, []).append(rec.memory_id)

        for scope_key, kind_groups in scope_kind_groups.items():
            scope_topic_id = "scope-" + scope_key
            scope_label = _scope_topic_label(scope_key)
            scope_path = f"{root_name} → {scope_label}"
            record_total = sum(len(v) for v in kind_groups.values())
            nodes.append(ProjectionNode(
                id=scope_topic_id, parent_id=root_id, label=scope_label, node_kind="topic",
                kind=scope_key, bg=SCOPE_TOPIC_COLORS.get(scope_key, "#5b8def"),
                title=f"{scope_label}",
                body=f"按记忆 scope={scope_key} 汇聚。共 {record_total} 条，下再按类型分叉。",
                derivation=scope_path,
                edge_hint="按项目/用户等来源从胞体分叉",
                provenance_count=record_total,
            ))
            edges.append(_make_edge(
                root_id, scope_topic_id, "derived_from",
                label="来源分叉", reason=f"按记忆来源 {scope_key} 衍生",
            ))

            for kind, record_ids in kind_groups.items():
                topic_id = f"topic-{scope_key}-{kind}"
                topic_label = _topic_label(kind)
                topic_path = f"{scope_path} → {topic_label}"
                nodes.append(ProjectionNode(
                    id=topic_id, parent_id=scope_topic_id, label=topic_label, node_kind="topic",
                    kind=kind, bg=KIND_COLORS.get(kind, "#5b8def"),
                    title=f"{topic_label}",
                    body=f"{scope_label}下 kind={kind} 的记忆末梢。共 {len(record_ids)} 条。",
                    derivation=topic_path,
                    edge_hint="按 MemoryKind 从项目来源分叉",
                    provenance_count=len(record_ids),
                ))
                edges.append(_make_edge(
                    scope_topic_id, topic_id, "derived_from",
                    label="类型分叉", reason=f"按记忆类型 {kind} 衍生",
                ))

                # 重复簇挂在类型主题下
                for cluster_id, member_ids in cluster_members.items():
                    primary = records_by_id[member_ids[0]]
                    if primary.kind.value != kind or _normalize_scope(primary.scope) != scope_key:
                        continue
                    member_records = [records_by_id[mid] for mid in member_ids]
                    title = primary.title or primary.memory_id[:8]
                    body = "\n".join(
                        f"- {(rec.title or rec.memory_id[:8])}: {rec.body[:160]}"
                        for rec in member_records
                    )
                    provenance_count = sum(len(rec.provenance) for rec in member_records)
                    avg_confidence = sum(rec.confidence for rec in member_records) / len(member_records)
                    src_key, src_loc = _primary_provenance(primary)
                    nodes.append(ProjectionNode(
                        id=cluster_id, parent_id=topic_id,
                        label=f"{title} 等 {len(member_records)} 条",
                        node_kind="duplicate_cluster", memory_id=primary.memory_id,
                        kind=kind, provenance_count=provenance_count,
                        bg=KIND_COLORS.get(kind, "#5b8def"),
                        title=f"相似片段组：{title}", body=body,
                        original_title=primary.original_title, original_body=primary.original_body,
                        display_language=primary.display_language, scope=primary.scope,
                        confidence=avg_confidence, completeness=primary.completeness.value,
                        cluster_count=len(member_records), member_ids=member_ids,
                        derivation=f"{topic_path} → 相似簇",
                        source_key=src_key, source_locator=src_loc,
                        edge_hint="重复候选合并为簇节点",
                    ))
                    edges.append(_make_edge(
                        topic_id, cluster_id, "derived_from",
                        label="相似簇", reason="同来源同类型重复候选合并展示",
                    ))
                    for mid in member_ids:
                        claim_node_by_memory[mid] = cluster_id

                # 按同源文件拆成 source_hub（≥2 条才建突触）
                by_source: dict[str, list[str]] = {}
                singles: list[str] = []
                for rid in record_ids:
                    if rid in clustered_ids:
                        continue
                    src_key, _ = _primary_provenance(records_by_id[rid])
                    if src_key:
                        by_source.setdefault(src_key, []).append(rid)
                    else:
                        singles.append(rid)

                hub_ids: dict[str, str] = {}
                for src_key, mids in by_source.items():
                    if len(mids) < 2:
                        singles.extend(mids)
                        continue
                    hub_id = "hub-" + stable_hash(scope_key, kind, src_key)[:12]
                    hub_ids[src_key] = hub_id
                    first = records_by_id[mids[0]]
                    _, loc = _primary_provenance(first)
                    nodes.append(ProjectionNode(
                        id=hub_id, parent_id=topic_id,
                        label=_short_source(src_key),
                        node_kind="source_hub", kind=kind,
                        bg=KIND_COLORS.get(kind, "#5b8def"),
                        title=f"同源突触 · {_short_source(src_key)}",
                        body=f"同一来源对象下分出 {len(mids)} 条记忆末梢。\nlocator: {loc or '—'}",
                        provenance_count=len(mids),
                        cluster_count=len(mids),
                        member_ids=list(mids),
                        derivation=f"{topic_path} → 同源突触",
                        source_key=src_key, source_locator=loc,
                        edge_hint="同源文件聚合成突触",
                        confidence=sum(records_by_id[m].confidence for m in mids) / len(mids),
                        scope=first.scope,
                    ))
                    edges.append(_make_edge(
                        topic_id, hub_id, "derived_from",
                        label="同源突触", reason=f"同一 source_object 衍生 {len(mids)} 条",
                    ))

                def _attach_claim(rid: str, parent_id: str, parent_path: str, parent_hint: str,
                                  kind_name: str = kind) -> None:
                    rec = records_by_id.get(rid)
                    if not rec:
                        return
                    node_id = "claim-" + rid[:12]
                    src_key, src_loc = _primary_provenance(rec)
                    label = rec.title[:40] if rec.title else rec.memory_id[:8]
                    nodes.append(ProjectionNode(
                        id=node_id, parent_id=parent_id, label=label,
                        node_kind="claim_anchor", memory_id=rid,
                        kind=kind_name, provenance_count=len(rec.provenance),
                        bg=KIND_COLORS.get(kind_name, "#5b8def"),
                        title=rec.title, body=rec.body,
                        original_title=rec.original_title, original_body=rec.original_body,
                        display_language=rec.display_language, scope=rec.scope,
                        confidence=rec.confidence, completeness=rec.completeness.value,
                        derivation=f"{parent_path} → {label[:24] or '记忆'}",
                        source_key=src_key, source_locator=src_loc,
                        edge_hint=parent_hint,
                    ))
                    edges.append(_make_edge(
                        parent_id, node_id, "derived_from",
                        label="记忆末梢", reason=parent_hint,
                    ))
                    claim_node_by_memory[rid] = node_id

                for src_key, hub_id in hub_ids.items():
                    for rid in by_source[src_key]:
                        _attach_claim(
                            rid, hub_id,
                            f"{topic_path} → 同源突触",
                            "从同源突触长出记忆末梢",
                        )

                for rid in singles:
                    _attach_claim(
                        rid, topic_id, topic_path,
                        "直接从类型树突长出",
                    )

        # ---- 重复/关联边（含 KEEP_ALL）----
        seen_pair: set[tuple[str, str]] = set()
        for grp in ir.duplicate_groups:
            members = [mid for mid in grp.member_ids if mid in claim_node_by_memory]
            if len(members) < 2:
                continue
            etype = "duplicate" if grp.decision != DuplicateDecision.KEEP_ALL else "related"
            reason = f"重复组 {grp.group_id[:8]} · {grp.decision.value}"
            for i, a in enumerate(members):
                for b in members[i + 1:]:
                    na, nb = claim_node_by_memory[a], claim_node_by_memory[b]
                    if na == nb:
                        continue
                    key = tuple(sorted((na, nb)))
                    if key in seen_pair:
                        continue
                    seen_pair.add(key)
                    edges.append(_make_edge(
                        na, nb, etype,
                        label="相似关联" if etype == "related" else "重复候选",
                        reason=reason,
                    ))

        # ---- 跨类型同源边（同 source_object，不同 kind）----
        # 先按 kind 取样，再跨 kind 连边；避免 unique[:4] 全是同 kind 时画不出虚线
        source_to_nodes: dict[str, list[str]] = {}
        node_kind_by_id = {n.id: n.kind for n in nodes}
        for node in nodes:
            if node.node_kind not in {"claim_anchor", "duplicate_cluster"}:
                continue
            if not node.source_key:
                continue
            source_to_nodes.setdefault(node.source_key, []).append(node.id)
        for src_key, nids in source_to_nodes.items():
            by_kind: dict[str, list[str]] = {}
            for nid in dict.fromkeys(nids):
                k = node_kind_by_id.get(nid) or ""
                if not k:
                    continue
                bucket = by_kind.setdefault(k, [])
                if len(bucket) < 2:
                    bucket.append(nid)
            kind_keys = list(by_kind.keys())
            if len(kind_keys) < 2:
                continue
            for i, ka in enumerate(kind_keys):
                for kb in kind_keys[i + 1:]:
                    for a in by_kind[ka]:
                        for b in by_kind[kb]:
                            key = tuple(sorted((a, b)))
                            if key in seen_pair:
                                continue
                            seen_pair.add(key)
                            edges.append(_make_edge(
                                a, b, "shared_source",
                                label="同源跨类型",
                                reason=f"共享来源 {_short_source(src_key)}",
                            ))

        projection_meta = dict(meta or {})
        # 调用方可覆盖（共享组传 share_group / deterministic_v3_shared）
        projection_meta.setdefault("projection_mode", self.mode)
        projection_meta.setdefault("derivation_engine", "deterministic_v3")
        projection_meta["llm_used"] = False
        proj = NeuronProjection(
            snapshot_id=ir.snapshot_id, built_at=_now_iso(),
            nodes=nodes, edges=edges,
            meta=projection_meta,
        )
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
        """持久化到 .memoryguard/projections/{mode}.json。"""
        self.proj_path.parent.mkdir(parents=True, exist_ok=True)
        tombstone = self.proj_path.with_suffix(self.proj_path.suffix + ".deleted")
        if tombstone.exists():
            tombstone.unlink()
        self.proj_path.write_text(
            json.dumps(proj.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")

    def load(self) -> NeuronProjection | None:
        """只加载当前 scoped 路径。禁止回退旧全局图（Sol P0：避免混显）。"""
        if not self.scope_key:
            return None
        tombstone = self.proj_path.with_suffix(self.proj_path.suffix + ".deleted")
        if tombstone.exists():
            return None
        path = self.proj_path
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            node_fields = set(ProjectionNode.__dataclass_fields__)
            edge_fields = set(ProjectionEdge.__dataclass_fields__)
            nodes = [
                ProjectionNode(**{k: v for k, v in n.items() if k in node_fields})
                for n in data.get("nodes", [])
            ]
            edges = [
                ProjectionEdge(**{k: v for k, v in e.items() if k in edge_fields})
                for e in data.get("edges", [])
            ]
            return NeuronProjection(
                snapshot_id=data.get("snapshot_id", ""),
                built_at=data.get("built_at", ""),
                content_hash=data.get("content_hash", ""),
                nodes=nodes,
                edges=edges,
                meta=data.get("meta", {}) if isinstance(data.get("meta"), dict) else {},
            )
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def delete(self) -> None:
        """删除当前 scoped 投影。"""
        if not self.scope_key:
            return
        self.proj_path.parent.mkdir(parents=True, exist_ok=True)
        if self.proj_path.exists():
            self.proj_path.unlink()
        self.proj_path.with_suffix(self.proj_path.suffix + ".deleted").write_text(
            "deleted", encoding="utf-8",
        )

    def get_or_empty(self) -> dict[str, Any]:
        """GUI API 用：未构建时返回 empty 状态。"""
        proj = self.load()
        if proj is None:
            return {"empty": True, "reason": "not_built"}
        return proj.to_dict()
