"""治理范围（GovernanceScope）：单 Agent / MCP 共享组显式隔离。

设计约束（Sol P0）：
- scope 必须由调用方显式传入；governance_scope.json 只作 UI 偏好，不作授权依据
- 缺 scope / 非法 scope / 双 scope 冲突 → fail closed
- scoped normalize 不得覆盖全局 ir/current.json
- 旧全局投影禁止「过滤后回退混显」；应重建或 not_built
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .memory_ir import MemoryIR
from .schema_v3 import (
    DuplicateGroup,
    MemoryRecord,
    MemoryStatus,
    SourceRoot,
    SourceSnapshot,
    stable_hash,
    _now_iso,
)


_SCOPE_KEY_RE = re.compile(r"[^a-zA-Z0-9._-]+")


@dataclass
class GovernanceScope:
    """显式治理范围。"""

    mode: str  # agent | share_group
    agent_instance_id: str = ""
    share_group_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "agent_instance_id": self.agent_instance_id,
            "share_group_id": self.share_group_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "GovernanceScope | None":
        if not isinstance(data, dict):
            return None
        mode = str(data.get("mode", "")).strip()
        if mode not in {"agent", "share_group"}:
            return None
        return cls(
            mode=mode,
            agent_instance_id=str(data.get("agent_instance_id", "") or ""),
            share_group_id=str(data.get("share_group_id", "") or ""),
        )


def _validate_id(value: str, *, kind: str) -> str:
    text = value.strip()
    if not text:
        return f"{kind}_required"
    # 路径穿越与控制字符拒绝；其余靠 storage_key 哈希防碰撞
    if "/" in text or "\\" in text or ".." in text:
        return f"invalid_{kind}"
    if any(ord(ch) < 32 for ch in text):
        return f"invalid_{kind}"
    if len(text) > 128:
        return f"invalid_{kind}"
    return ""


def validate_scope(scope: GovernanceScope | dict[str, Any] | None) -> tuple[GovernanceScope | None, str]:
    """校验并规范化 scope。失败返回 (None, error)。"""
    if scope is None:
        return None, "missing_governance_scope"
    if isinstance(scope, dict):
        parsed = GovernanceScope.from_dict(scope)
    elif isinstance(scope, GovernanceScope):
        parsed = scope
    else:
        return None, "invalid_governance_scope_type"
    if parsed is None:
        return None, "invalid_governance_scope"
    if parsed.mode == "agent":
        err = _validate_id(parsed.agent_instance_id, kind="agent_instance_id")
        if err:
            return None, err
        if parsed.share_group_id.strip():
            return None, "conflicting_governance_scope"
        return GovernanceScope(mode="agent", agent_instance_id=parsed.agent_instance_id.strip()), ""
    err = _validate_id(parsed.share_group_id, kind="share_group_id")
    if err:
        return None, err
    if parsed.agent_instance_id.strip():
        return None, "conflicting_governance_scope"
    return GovernanceScope(mode="share_group", share_group_id=parsed.share_group_id.strip()), ""


def resolve_governance_scope(
    scope: GovernanceScope | dict[str, Any] | None = None,
    *,
    agent_instance_id: str = "",
    share_group_id: str = "",
    mode: str = "",
) -> tuple[GovernanceScope | None, str]:
    """GUI/CLI/MCP 共用 resolver：显式、互斥、fail closed。"""
    agent = str(agent_instance_id or "").strip()
    share = str(share_group_id or "").strip()
    mode_hint = str(mode or "").strip()
    payload: dict[str, Any] | None = None

    if scope is not None:
        if isinstance(scope, GovernanceScope):
            payload = scope.to_dict()
        elif isinstance(scope, dict):
            payload = dict(scope)
        else:
            return None, "invalid_governance_scope_type"
        payload_agent = str(payload.get("agent_instance_id", "") or "").strip()
        payload_share = str(payload.get("share_group_id", "") or "").strip()
        if agent and payload_agent and agent != payload_agent:
            return None, "conflicting_governance_scope"
        if share and payload_share and share != payload_share:
            return None, "conflicting_governance_scope"
        if agent:
            payload["agent_instance_id"] = agent
        if share:
            payload["share_group_id"] = share
        if mode_hint:
            payload["mode"] = mode_hint
        elif not payload.get("mode"):
            if payload.get("share_group_id"):
                payload["mode"] = "share_group"
            elif payload.get("agent_instance_id"):
                payload["mode"] = "agent"
    elif agent or share or mode_hint:
        if agent and share:
            return None, "conflicting_governance_scope"
        if mode_hint == "share_group" or share:
            if agent:
                return None, "conflicting_governance_scope"
            payload = {"mode": "share_group", "share_group_id": share}
        else:
            if share:
                return None, "conflicting_governance_scope"
            payload = {"mode": "agent", "agent_instance_id": agent}
    else:
        return None, "missing_governance_scope"

    # 双标识互斥（含 payload 内同时带齐）
    final_agent = str(payload.get("agent_instance_id", "") or "").strip()
    final_share = str(payload.get("share_group_id", "") or "").strip()
    final_mode = str(payload.get("mode", "") or "").strip()
    if final_agent and final_share:
        return None, "conflicting_governance_scope"
    if final_mode == "agent" and final_share:
        return None, "conflicting_governance_scope"
    if final_mode == "share_group" and final_agent:
        return None, "conflicting_governance_scope"
    return validate_scope(payload)


def scope_storage_key(scope: GovernanceScope) -> str:
    """安全文件名：sanitize 前缀 + 原始 ID 稳定哈希，杜绝碰撞。"""
    ok, err = validate_scope(scope)
    if ok is None:
        raise ValueError(err)
    if ok.mode == "agent":
        kind, raw_id = "agent", ok.agent_instance_id
    else:
        kind, raw_id = "share", ok.share_group_id
    cleaned = _SCOPE_KEY_RE.sub("_", raw_id).strip("._-") or "id"
    if len(cleaned) > 48:
        cleaned = cleaned[:48]
    digest = stable_hash(kind, raw_id)[:16]
    return f"{kind}-{cleaned}-{digest}"


def preference_path(workspace: str | Path) -> Path:
    return Path(workspace).resolve() / ".memoryguard" / "governance_scope.json"


def load_scope_preference(workspace: str | Path) -> GovernanceScope | None:
    """UI 偏好（非授权依据）。"""
    path = preference_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scope, err = validate_scope(data)
    return scope if not err else None


def save_scope_preference(workspace: str | Path, scope: GovernanceScope) -> dict[str, Any]:
    ok, err = validate_scope(scope)
    if ok is None:
        return {"error": err}
    path = preference_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = ok.to_dict()
    payload["updated_at"] = _now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "scope": payload}


def root_authorizes_agent(root: SourceRoot, agent_instance_id: str) -> bool:
    """多对多：authorized_agent_ids 或遗留 agent_instance_id。"""
    if not agent_instance_id:
        return False
    ids = list(getattr(root, "authorized_agent_ids", None) or [])
    if agent_instance_id in ids:
        return True
    return bool(root.agent_instance_id and root.agent_instance_id == agent_instance_id)


def grant_root_to_agent(root: SourceRoot, agent_instance_id: str) -> None:
    if not agent_instance_id:
        return
    ids = list(getattr(root, "authorized_agent_ids", None) or [])
    if agent_instance_id not in ids:
        ids.append(agent_instance_id)
    root.authorized_agent_ids = ids
    if not root.agent_instance_id:
        root.agent_instance_id = agent_instance_id
    enabled_map = dict(getattr(root, "agent_enabled", None) or {})
    enabled_map.setdefault(agent_instance_id, True)
    root.agent_enabled = enabled_map


def revoke_root_from_agent(root: SourceRoot, agent_instance_id: str) -> None:
    if not agent_instance_id:
        return
    ids = [a for a in (getattr(root, "authorized_agent_ids", None) or []) if a != agent_instance_id]
    root.authorized_agent_ids = ids
    enabled_map = dict(getattr(root, "agent_enabled", None) or {})
    enabled_map.pop(agent_instance_id, None)
    root.agent_enabled = enabled_map
    if root.agent_instance_id == agent_instance_id:
        root.agent_instance_id = ids[0] if ids else ""


def is_root_enabled_for_agent(root: SourceRoot, agent_instance_id: str) -> bool:
    """per-agent 启用态；缺省回退全局 enabled。"""
    enabled_map = getattr(root, "agent_enabled", None) or {}
    if agent_instance_id in enabled_map:
        return bool(enabled_map[agent_instance_id])
    return bool(root.enabled)


def set_root_enabled_for_agent(root: SourceRoot, agent_instance_id: str, enabled: bool) -> None:
    """共享根不改全局 enabled；单授权时可同步全局开关。"""
    enabled_map = dict(getattr(root, "agent_enabled", None) or {})
    enabled_map[agent_instance_id] = bool(enabled)
    root.agent_enabled = enabled_map
    auth_ids = list(getattr(root, "authorized_agent_ids", None) or [])
    if root.agent_instance_id and root.agent_instance_id not in auth_ids:
        auth_ids = [root.agent_instance_id, *auth_ids]
    if len(auth_ids) <= 1:
        root.enabled = bool(enabled)


def resolve_scoped_roots(
    roots: Iterable[SourceRoot],
    scope: GovernanceScope,
    *,
    enabled_only: bool = True,
) -> tuple[list[SourceRoot], str]:
    """解析当前 scope 可见 SourceRoot。share_group 模式返回空列表（不走原生根）。"""
    ok, err = validate_scope(scope)
    if ok is None:
        return [], err
    if ok.mode == "share_group":
        return [], ""
    out: list[SourceRoot] = []
    for root in roots:
        if not root_authorizes_agent(root, ok.agent_instance_id):
            continue
        if enabled_only and not is_root_enabled_for_agent(root, ok.agent_instance_id):
            continue
        out.append(root)
    return out, ""


def authorized_roots_digest(root_ids: Iterable[str]) -> str:
    return stable_hash("auth-roots", *sorted({r for r in root_ids if r}))


def projection_auth_matches(meta: dict[str, Any] | None, root_ids: Iterable[str]) -> bool:
    """投影 meta 中的授权摘要必须与当前授权根一致，否则视为失效。"""
    if not isinstance(meta, dict):
        return False
    expected = authorized_roots_digest(root_ids)
    got = str(meta.get("authorized_roots_digest", "") or "")
    return bool(got) and got == expected


def derive_publish_target_file(root: SourceRoot) -> Path:
    """服务端从 SourceRoot 派生写回路径；不信任客户端 path。"""
    path = Path(root.path).resolve()
    if path.suffix:
        return path
    return path / "memory.md"


def _object_to_root_map(snapshot: SourceSnapshot | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    return {obj.source_object_id: obj.source_root_id for obj in snapshot.source_objects}


def record_belongs_to_roots(
    rec: MemoryRecord,
    allowed_root_ids: set[str],
    obj_to_root: dict[str, str],
) -> bool:
    """无法从 provenance 推导归属 → False（fail closed）。"""
    if not rec.provenance:
        return False
    for prov in rec.provenance:
        root_id = obj_to_root.get(prov.source_object_id, "")
        if root_id and root_id in allowed_root_ids:
            return True
    return False


def filter_ir_for_agent(
    ir: MemoryIR,
    allowed_root_ids: set[str],
    snapshot: SourceSnapshot | None = None,
    *,
    obj_to_root: dict[str, str] | None = None,
) -> MemoryIR:
    """深拷贝过滤：不修改原 IR；裁剪 duplicate_groups。"""
    mapping = dict(obj_to_root or {})
    if not mapping:
        mapping = _object_to_root_map(snapshot)
    kept: list[MemoryRecord] = []
    kept_ids: set[str] = set()
    for rec in ir.records:
        if rec.status == MemoryStatus.REJECTED:
            continue
        if record_belongs_to_roots(rec, allowed_root_ids, mapping):
            kept.append(copy.deepcopy(rec))
            kept_ids.add(rec.memory_id)
    groups: list[DuplicateGroup] = []
    for grp in ir.duplicate_groups:
        members = [mid for mid in grp.member_ids if mid in kept_ids]
        if len(members) < 2:
            continue
        g = copy.deepcopy(grp)
        g.member_ids = members
        if g.scores and len(g.scores) != len(members):
            g.scores = list(g.scores[: len(members)])
        groups.append(g)
    return MemoryIR(
        records=kept,
        duplicate_groups=groups,
        decisions=list(ir.decisions),
        snapshot_id=ir.snapshot_id,
        created_at=ir.created_at,
    )


def projection_path(workspace: str | Path, mode: str, scope: GovernanceScope) -> Path:
    mode_name = "native" if mode == "native" else "reconstructed"
    if scope.mode == "share_group":
        mode_name = "share_group"
    key = scope_storage_key(scope)
    return Path(workspace).resolve() / ".memoryguard" / "projections" / mode_name / f"{key}.json"


def share_group_projection_path(workspace: str | Path, scope: GovernanceScope) -> Path:
    return projection_path(workspace, "share_group", scope)


_PRODUCT_DISPLAY_NAMES = {
    "claude-code": "Claude Code",
    "claude": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "trae": "Trae",
    "gemini": "Gemini",
    "windsurf": "Windsurf",
    "copilot": "Copilot",
    "aider": "Aider",
}


def _load_instance_product_map(workspace: str | Path) -> dict[str, str]:
    """instance_id -> 可读产品名（优先 discovery/latest.json）。"""
    path = Path(workspace).resolve() / ".memoryguard" / "discovery" / "latest.json"
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    for inst in data.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        iid = str(inst.get("instance_id", "") or "").strip()
        product = str(inst.get("product", "") or "").strip()
        if iid and product and product != "unknown":
            out[iid] = _PRODUCT_DISPLAY_NAMES.get(product, product)
    return out


def _agent_display_name(agent_instance_id: str, product_map: dict[str, str]) -> str:
    aid = (agent_instance_id or "").strip()
    if not aid:
        return "未知 Agent"
    if aid in product_map:
        return product_map[aid]
    # 兼容 id 形如 product/xxx
    if "/" in aid:
        head = aid.split("/", 1)[0]
        return _PRODUCT_DISPLAY_NAMES.get(head, head)
    return _PRODUCT_DISPLAY_NAMES.get(aid, aid)


def share_group_status_meta(workspace: str | Path, share_group_id: str) -> dict[str, Any]:
    """共享组状态条元数据：组名、绑定成员、active 记忆数。"""
    gid = (share_group_id or "").strip()
    bound: list[dict[str, Any]] = []
    active_records = 0
    conflict_count = 0
    quarantine_count = 0
    release_count = 0
    product_map = _load_instance_product_map(workspace)
    try:
        from .agent_binding import AgentBindingStore
        for b in AgentBindingStore(workspace).find_by_group(gid, include_inactive=False):
            aid = str(getattr(b, "agent_instance_id", "") or "")
            display = _agent_display_name(aid, product_map)
            bound.append({
                "agent_instance_id": aid,
                "product": display,
                "display_name": display,
                "status": getattr(getattr(b, "status", None), "value", str(getattr(b, "status", "") or "")),
            })
    except Exception:
        pass
    try:
        from .shared_memory_store import SharedMemoryStore
        store = SharedMemoryStore(workspace, gid, read_only=True)
        active_records = len(store.list_records(status="active"))
        try:
            conflict_count = len(store.list_conflicts())
        except Exception:
            conflict_count = 0
        try:
            quarantine_count = len(store.list_quarantine())
        except Exception:
            quarantine_count = 0
        try:
            release_count = len(store.list_versions()) if hasattr(store, "list_versions") else 0
        except Exception:
            release_count = 0
    except Exception:
        pass
    return {
        "share_group_id": gid,
        "share_group_label": gid or "未命名共享组",
        "bound_agents": bound,
        "agent_count": len(bound),
        "instance_count": len(bound),
        "active_records": active_records,
        "conflict_count": conflict_count,
        "quarantine_count": quarantine_count,
        "release_count": release_count,
        "coverage_status": "complete" if active_records > 0 else ("empty" if gid else "unknown"),
        "drifted": False,
        "agent_instances": [],  # 共享组不用单 Agent 实例条
    }


def share_file_source_key(meta: dict[str, Any] | None) -> str:
    """从导入事件 metadata 生成文件级同源键（同文件多段 → 同一 source_hub）。

    只用 relative_path：共享组内多 Agent 导入同一相对路径时应聚成同一突触。
    """
    if not isinstance(meta, dict):
        return ""
    rel = str(meta.get("relative_path") or "").strip().replace("\\", "/")
    if rel:
        return f"share-file:{rel}"
    return ""


def _rewrite_share_provenance_for_hubs(
    provs: list[Any],
    *,
    event_by_id: dict[str, Any],
    share_group_id: str,
    memory_id: str,
    body_text: str,
    Provenance: Any,
) -> list[Any]:
    """把 event_id 级 provenance 提升为文件级同源键；无事件元数据则保持/合成单键。"""
    hub = ""
    locator = "shared"
    excerpt = stable_hash(body_text)[:16]
    for p in provs:
        oid = str(getattr(p, "source_object_id", "") or "")
        # 已是文件键
        if oid.startswith("share-file:"):
            hub = oid
            locator = str(getattr(p, "locator", "") or locator)
            excerpt = str(getattr(p, "excerpt_hash", "") or excerpt)
            break
        ev = event_by_id.get(oid)
        if ev is None:
            continue
        meta = getattr(ev, "metadata", None) or {}
        file_key = share_file_source_key(meta if isinstance(meta, dict) else {})
        if file_key:
            hub = file_key
            locator = str(meta.get("locator") or getattr(p, "locator", "") or locator)
            excerpt = str(getattr(p, "excerpt_hash", "") or excerpt)
            break
    if hub:
        return [Provenance(source_object_id=hub, locator=locator, excerpt_hash=excerpt)]
    if provs:
        return list(provs)
    return [Provenance(
        source_object_id=f"share:{share_group_id}:{memory_id[:16]}",
        locator="shared",
        excerpt_hash=excerpt,
    )]


def build_shared_memory_graph(
    workspace: str | Path,
    share_group_id: str,
    *,
    status: str = "active",
) -> dict[str, Any]:
    """从 SharedMemoryStore 生成神经图：复用单 Agent ProjectionBuilder 美化衍生。"""
    from .projection import ProjectionBuilder
    from .schema_v3 import (
        Completeness,
        DuplicateDecision,
        MemoryKind,
        Provenance,
    )
    from .shared_memory_store import SharedMemoryStore
    from .source_registry import SourceRegistry

    scope = GovernanceScope(mode="share_group", share_group_id=share_group_id)
    try:
        store = SharedMemoryStore(workspace, share_group_id, read_only=True)
    except FileNotFoundError:
        return {
            "empty": True,
            "reason": "share_group_not_found",
            "projection_kind": "shared_memory_projection",
            "mode": "share_group",
            "scope": scope.to_dict(),
        }
    records = store.list_records(status=status or None)
    # 旧导入：provenance=event_id；用 events.metadata 还原同文件同源键
    event_by_id = {ev.event_id: ev for ev in store.list_events()}
    roots_by_id = {
        root.root_id: root
        for root in SourceRegistry(workspace).list_all_sources()
    }

    def _record_scope(rec) -> str:
        """从入库事件/SourceRoot 恢复来源层级；无导入根才算 MCP 共享写入。"""
        for provenance in list(getattr(rec, "provenance", None) or []):
            event = event_by_id.get(provenance.source_object_id)
            metadata = (
                event.metadata
                if event is not None and isinstance(event.metadata, dict)
                else {}
            )
            root_id = str(metadata.get("source_root_id", "") or "")
            root = roots_by_id.get(root_id)
            if root is not None:
                if str(root.project_ref or "").strip():
                    return "project"
                root_scope = str(root.scope or "").strip().lower()
                if root_scope in {
                    "project", "user", "agent", "session", "share_group",
                }:
                    return root_scope
            category = str(metadata.get("source_category", "") or "")
            if category == "conversation_history":
                return "session"
        return "share_group"

    ir_records: list[MemoryRecord] = []
    for rec in records:
        body_text = (rec.body or "").strip()
        title = body_text.split("\n", 1)[0][:80] if body_text else rec.memory_id[:8]
        raw_kind = getattr(rec.kind, "value", rec.kind) if rec.kind is not None else "fact"
        try:
            kind = rec.kind if isinstance(rec.kind, MemoryKind) else MemoryKind(str(raw_kind or "fact"))
        except ValueError:
            kind = MemoryKind.FACT
        provs = _rewrite_share_provenance_for_hubs(
            list(getattr(rec, "provenance", None) or []),
            event_by_id=event_by_id,
            share_group_id=share_group_id,
            memory_id=rec.memory_id,
            body_text=body_text,
            Provenance=Provenance,
        )
        ir_records.append(MemoryRecord(
            memory_id=rec.memory_id,
            kind=kind,
            title=title,
            body=rec.body or "",
            scope=_record_scope(rec),
            confidence=float(getattr(rec, "confidence", 0.5) or 0.5),
            provenance=provs,
            status=MemoryStatus.CANDIDATE,
            completeness=Completeness.VERIFIABLE,
        ))

    # 轻量相似组 → related 虚线（与单 Agent KEEP_ALL 语义一致）
    dup_groups: list[DuplicateGroup] = []
    token_cache: dict[str, set[str]] = {}
    _tok_re = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|[\u4e00-\u9fff]")

    def _tok(text: str) -> set[str]:
        return set(_tok_re.findall((text or "").lower()))

    # 控制 O(n²)：同 kind 内最多扫前 80 条做相似边；阈值略降以照顾短标题段落
    by_kind_ids: dict[str, list[MemoryRecord]] = {}
    for rec in ir_records:
        by_kind_ids.setdefault(rec.kind.value, []).append(rec)
    for kind_recs in by_kind_ids.values():
        subset = kind_recs[:80]
        for i, a in enumerate(subset):
            ta = token_cache.setdefault(a.memory_id, _tok(f"{a.title} {a.body}"))
            if len(ta) < 2:
                continue
            for b in subset[i + 1:]:
                tb = token_cache.setdefault(b.memory_id, _tok(f"{b.title} {b.body}"))
                if len(tb) < 2:
                    continue
                inter = len(ta & tb)
                union = len(ta | tb) or 1
                if inter / union < 0.42:
                    continue
                dup_groups.append(DuplicateGroup(
                    group_id=stable_hash("share-dup", a.memory_id, b.memory_id)[:16],
                    member_ids=[a.memory_id, b.memory_id],
                    decision=DuplicateDecision.KEEP_ALL,
                ))

    record_ids = sorted(r.memory_id for r in records)
    ir = MemoryIR(
        snapshot_id=stable_hash("share", share_group_id, status, *record_ids),
        records=ir_records,
        duplicate_groups=dup_groups,
        created_at=_now_iso(),
    )
    status_meta = share_group_status_meta(workspace, share_group_id)
    pb = ProjectionBuilder(workspace, "reconstructed")
    proj = pb.build(
        ir,
        meta={
            "projection_mode": "share_group",
            "share_group_id": share_group_id,
            "llm_used": False,
            "derivation_engine": "deterministic_v3_shared",
            "authorized_roots_digest": authorized_roots_digest([f"share:{share_group_id}"]),
            "source_record_ids": record_ids,
            "source_record_digest": stable_hash("share-recs", *record_ids),
            **status_meta,
        },
        root_label="共享胞体",
        root_body=f"共享组 {share_group_id} 的 active 记忆视图；与单 Agent 共用同源突触 / 跨类型连线美化。",
    )
    data = proj.to_dict()
    data["empty"] = len(records) == 0
    if not records:
        data["reason"] = "share_group_empty"
    data["projection_kind"] = "shared_memory_projection"
    data["mode"] = "share_group"
    data["scope"] = scope.to_dict()
    return data
