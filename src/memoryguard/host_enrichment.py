"""宿主 AI 异步整理队列。

闭环:build_projection/normalize 后入队 -> 宿主 AI 通过 MCP 拉取任务 -> 在对话中完成 classify/translate -> 回写 IR。

设计:
- 队列存储: .memoryguard/enrichments/pending.jsonl (每行一个 task)
- 幂等:同 memory_id + 同 content fingerprint 不重复 pending
- 写回:更新 IR 记录的 title/body/kind/confidence/localization_mode
- 治理隔离:task 绑定 scope,跨 agent 不可见
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema_v3 import stable_hash, _now_iso
from .memory_ir import looks_english_text, MemoryNormalizer


# ---------------------------------------------------------------------------
# 队列存储
# ---------------------------------------------------------------------------

_PENDING_FILE = "enrichments/pending.jsonl"
_APPLIED_DIR = "enrichments/applied"


def _pending_path(workspace: str | Path) -> Path:
    return Path(workspace) / ".memoryguard" / _PENDING_FILE


def _applied_dir(workspace: str | Path) -> Path:
    return Path(workspace) / ".memoryguard" / _APPLIED_DIR


def _content_fingerprint(title: str, body: str) -> str:
    return stable_hash(title, body)


# ---------------------------------------------------------------------------
# 入队
# ---------------------------------------------------------------------------


def enqueue_from_ir(
    workspace: str | Path,
    ir,
    scope: dict | None = None,
    *,
    reason: str = "post_normalize",
) -> int:
    """对仍值得宿主整理的 IR 记录入队。

    入队条件:
    - localization_mode != "model"
    - 且满足其一: looks_english_text / confidence < 0.6 / kind 为弱默认 fact
    不做: 已 model 且原文未变的记录

    返回:新入队数量。
    """
    ppath = _pending_path(workspace)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    _applied_dir(workspace).mkdir(parents=True, exist_ok=True)

    # 加载已有 pending 的 fingerprint,防重复
    existing_fps: set[str] = set()
    existing_mids: set[str] = set()
    if ppath.exists():
        for line in ppath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if item.get("status") == "pending":
                    existing_fps.add(item.get("content_fp", ""))
                    existing_mids.add(item.get("memory_id", ""))
            except json.JSONDecodeError:
                continue

    agent_instance_id = (scope or {}).get("agent_instance_id", "")
    share_group_id = (scope or {}).get("share_group_id", "")
    count = 0
    with ppath.open("a", encoding="utf-8") as f:
        for rec in ir.records:
            # 已 model 且原文未变 -> 跳过
            if getattr(rec, "localization_mode", "") == "model":
                continue
            title = getattr(rec, "title", "") or ""
            body = getattr(rec, "body", "") or ""
            text = f"{title} {body}"
            kind_hint = getattr(rec.kind, "value", str(getattr(rec, "kind", "fact")))
            conf = float(getattr(rec, "confidence", 0.5) or 0.5)
            # 弱默认 fact：启发式默认分类且置信不高
            weak_default_fact = kind_hint == "fact" and conf < 0.7 and not looks_english_text(text)
            needs_enrich = (
                looks_english_text(text)
                or conf < 0.6
                or weak_default_fact
            )
            if not needs_enrich:
                continue

            fp = _content_fingerprint(title, body)
            # 幂等:同 memory_id + 同 fingerprint 不重复
            if rec.memory_id in existing_mids and fp in existing_fps:
                continue

            task = {
                "task_id": "enr-" + stable_hash("enr", rec.memory_id, fp)[:16],
                "memory_id": rec.memory_id,
                "scope": {
                    "agent_instance_id": agent_instance_id,
                    "share_group_id": share_group_id,
                    "mode": "share_group" if share_group_id else "agent",
                },
                "ops": ["classify", "translate"],
                "input": {
                    "title": title or body[:40],
                    "body": body[:500],
                    "kind_hint": kind_hint,
                    "original_title": getattr(rec, "original_title", "") or "",
                    "original_body": (getattr(rec, "original_body", "") or "")[:500],
                },
                "status": "pending",
                "content_fp": fp,
                "reason": reason,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "result": None,
            }
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
            existing_mids.add(rec.memory_id)
            existing_fps.add(fp)
            count += 1

    return count


def enqueue_from_shared_store(
    workspace: str | Path,
    share_group_id: str,
    *,
    reason: str = "share_group_rebuild",
) -> int:
    """将 SharedMemoryStore active 记录入队，供多 Agent AI 整理。"""
    from .shared_memory_store import SharedMemoryStore
    from types import SimpleNamespace

    if not share_group_id:
        return 0
    store = SharedMemoryStore(workspace, share_group_id)
    records = store.list_records(status="active")
    # 适配 enqueue_from_ir 所需字段
    adapted = []
    for rec in records:
        body = (rec.body or "").strip()
        title = body.split("\n", 1)[0][:80] if body else rec.memory_id[:8]
        adapted.append(SimpleNamespace(
            memory_id=rec.memory_id,
            title=title,
            body=body,
            kind=rec.kind,
            confidence=float(rec.confidence or 0.5),
            localization_mode="",
            original_title="",
            original_body="",
        ))
    fake_ir = SimpleNamespace(records=adapted)
    return enqueue_from_ir(
        workspace,
        fake_ir,
        scope={"mode": "share_group", "share_group_id": share_group_id},
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def list_pending(
    workspace: str | Path,
    limit: int = 50,
    agent_instance_id: str = "",
    share_group_id: str = "",
) -> list[dict[str, Any]]:
    """返回 pending 任务列表。

    治理隔离:如果指定 agent_instance_id / share_group_id,只返回该 scope 的 task。
    """
    ppath = _pending_path(workspace)
    if not ppath.exists():
        return []

    results: list[dict[str, Any]] = []
    for line in ppath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("status") != "pending":
            continue
        scope = item.get("scope") or {}
        if share_group_id:
            if scope.get("share_group_id", "") != share_group_id:
                continue
        elif agent_instance_id:
            task_agent = scope.get("agent_instance_id", "")
            if task_agent and task_agent != agent_instance_id:
                continue
            # 排除纯 share_group 任务，避免单 Agent 视图串台
            if scope.get("mode") == "share_group" and scope.get("share_group_id"):
                continue
        results.append(item)
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# 应用结果
# ---------------------------------------------------------------------------


def apply_results(
    workspace: str | Path,
    results: list[dict[str, Any]],
    agent_instance_id: str = "",
    share_group_id: str = "",
) -> dict[str, Any]:
    """宿主 AI 回写整理结果。

    每条 result: {task_id, kind, title, body, confidence, rationale?}
    - agent scope: 写回 IR
    - share_group scope: 写回 SharedMemoryStore
    - 标记 task 为 applied

    返回: {applied: N, rejected: N, errors: [...], rebuild_suggested: bool}
    """
    from .policies import _VALID_KINDS
    from .schema_v3 import MemoryKind

    ppath = _pending_path(workspace)
    if not ppath.exists():
        return {"applied": 0, "rejected": 0, "errors": ["no pending file"], "rebuild_suggested": False}

    all_lines = ppath.read_text(encoding="utf-8").splitlines()
    tasks_by_id: dict[str, dict] = {}
    for i, line in enumerate(all_lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            if item.get("status") == "pending":
                tasks_by_id[item.get("task_id", "")] = {"item": item, "line_idx": i}
        except json.JSONDecodeError:
            continue

    shared_store = None
    ir = None
    ir_map: dict = {}
    if share_group_id:
        from .shared_memory_store import SharedMemoryStore
        shared_store = SharedMemoryStore(workspace, share_group_id)
    else:
        norm = MemoryNormalizer(workspace)
        ir = norm.load()
        if ir is None:
            return {"applied": 0, "rejected": 0, "errors": ["IR not found"], "rebuild_suggested": False}
        ir_map = {r.memory_id: r for r in ir.records}

    applied = 0
    rejected = 0
    errors: list[str] = []
    updated_lines = list(all_lines)

    for res in results:
        task_id = res.get("task_id", "")
        if task_id not in tasks_by_id:
            errors.append(f"task_id {task_id} not found or not pending")
            rejected += 1
            continue

        task_info = tasks_by_id[task_id]
        task = task_info["item"]
        task_scope = task.get("scope") or {}

        if share_group_id:
            if task_scope.get("share_group_id", "") != share_group_id:
                errors.append(f"task {task_id} belongs to different share group")
                rejected += 1
                continue
        elif agent_instance_id:
            task_agent = task_scope.get("agent_instance_id", "")
            if task_agent and task_agent != agent_instance_id:
                errors.append(f"task {task_id} belongs to different agent")
                rejected += 1
                continue

        kind_str = res.get("kind", "")
        if kind_str not in _VALID_KINDS:
            errors.append(f"task {task_id}: invalid kind '{kind_str}'")
            rejected += 1
            continue

        title = (res.get("title") or "").strip()
        if not title:
            errors.append(f"task {task_id}: empty title")
            rejected += 1
            continue

        body = (res.get("body") or "").strip()
        confidence = res.get("confidence", 0.5)
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, TypeError):
            confidence = 0.5

        memory_id = task.get("memory_id", "")
        if shared_store is not None:
            rec = shared_store.get_record(memory_id)
            if rec is None:
                errors.append(f"task {task_id}: memory_id {memory_id} not in shared store")
                rejected += 1
                continue
            # SharedMemoryRecord 无 title 字段：合并为正文
            new_body = body if body.startswith(title) else f"{title}\n\n{body}".strip()
            try:
                rec.kind = MemoryKind(kind_str)
            except (ValueError, TypeError):
                pass
            rec.body = new_body
            rec.confidence = confidence
            shared_store.update_record(rec)
        else:
            rec = ir_map.get(memory_id)
            if rec is None:
                errors.append(f"task {task_id}: memory_id {memory_id} not in IR")
                rejected += 1
                continue
            if not rec.original_title and rec.title:
                rec.original_title = rec.title
            if not rec.original_body and rec.body:
                rec.original_body = rec.body
            try:
                rec.kind = MemoryKind(kind_str)
            except (ValueError, TypeError):
                pass
            rec.title = title
            rec.body = body
            rec.confidence = confidence
            source = res.get("source", "model")
            rec.localization_mode = "heuristic" if source == "heuristic" else "model"
            rec.display_language = "zh"

        task["status"] = "applied"
        task["updated_at"] = _now_iso()
        task["result"] = {
            "kind": kind_str, "title": title, "body": body[:200],
            "confidence": confidence,
            "rationale": res.get("rationale", ""),
        }
        updated_lines[task_info["line_idx"]] = json.dumps(task, ensure_ascii=False)
        applied += 1

    if applied > 0 and ir is not None:
        MemoryNormalizer(workspace).save(ir)

    ppath.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    return {
        "applied": applied,
        "rejected": rejected,
        "errors": errors,
        "rebuild_suggested": applied > 0,
    }


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def get_status(
    workspace: str | Path,
    agent_instance_id: str = "",
    share_group_id: str = "",
) -> dict[str, Any]:
    """返回队列状态摘要。"""
    ppath = _pending_path(workspace)
    if not ppath.exists():
        return {"pending": 0, "applied": 0, "total": 0}

    pending_count = 0
    applied_count = 0
    for line in ppath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        scope = item.get("scope") or {}
        if share_group_id:
            if scope.get("share_group_id", "") != share_group_id:
                continue
        elif agent_instance_id:
            task_agent = scope.get("agent_instance_id", "")
            if task_agent and task_agent != agent_instance_id:
                continue
            if scope.get("mode") == "share_group" and scope.get("share_group_id"):
                continue
        if item.get("status") == "pending":
            pending_count += 1
        elif item.get("status") == "applied":
            applied_count += 1

    return {
        "pending": pending_count,
        "applied": applied_count,
        "total": pending_count + applied_count,
    }
