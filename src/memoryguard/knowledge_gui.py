"""V2 knowledge-surface facade used by the GUI shell.

The GUI itself remains a transport concern.  This module only renders small
native shells and translates the old method names into the V2 knowledge
command/read services.  It never opens the retired ``KnowledgeStore`` or
constructs a legacy task/runtime scope.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .content.store import ContentReadScope
from .knowledge_v2.command import KnowledgeV2CommandError, KnowledgeV2CommandService
from .knowledge_v2.service import KnowledgeV2ReadonlyService, KnowledgeV2ServiceError
from .runtime_v2.group_native import GroupControlError, GroupControlService


def _blocked(code: str = "trusted_v2_context_required") -> dict[str, Any]:
    return {"ok": False, "status": "blocked", "code": code, "error": code}


def _scope(workspace: str | Path, context: Any) -> ContentReadScope | None:
    if not isinstance(context, Mapping):
        return None
    workspace_id = str(context.get("workspace_id") or context.get("workspace") or "").strip()
    agent_id = str(context.get("agent_instance_id") or context.get("agent_id") or "").strip()
    group_id = str(context.get("share_group_id") or context.get("group_id") or "").strip()
    namespace_id = str(context.get("namespace_id") or "").strip()
    provider = str(context.get("provider") or "").strip()
    if not workspace_id or not agent_id or not group_id or not namespace_id or not provider:
        return None
    try:
        if Path(workspace_id).expanduser().resolve() != Path(workspace).expanduser().resolve():
            return None
        return ContentReadScope(
            namespace_id=namespace_id,
            workspace_id=workspace_id,
            agent_instance_id=agent_id,
            project_ref=str(context.get("project_ref") or ""),
            provider=provider,
            share_group_id=group_id,
            sensitivity=str(context.get("sensitivity") or "normal"),
            policy_class=str(context.get("policy_class") or "private"),
        )
    except (TypeError, ValueError, OSError):
        return None


def _native_context(workspace: str | Path, context: Any) -> dict[str, Any] | None:
    if not isinstance(context, Mapping):
        return None
    scope = _scope(workspace, context)
    if scope is None:
        return None
    result = {str(key): value for key, value in context.items()}
    result.update(scope.__dict__)
    result["workspace_id"] = scope.workspace_id
    result["agent_instance_id"] = scope.agent_instance_id
    result["share_group_id"] = scope.share_group_id
    return result


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def render_bookshelf_html() -> str:
    """Render a V2-only shell; data is fetched through the native bridge."""
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemoryGuard 知识书库</title>
<style>body{margin:0;padding:32px;background:#07110e;color:#e4f5ef;font:15px system-ui,sans-serif}main{max-width:960px;margin:auto}a{color:#6ee7c4}.card{padding:18px;margin:14px 0;border:1px solid #21443a;border-radius:14px;background:#0b1a16}</style>
</head><body><main><p><a class="back-link" href="/">← 返回主面板</a></p><h1>知识书库</h1>
<div class="card">V2 知识服务已接管。请通过受信任的原生会话加载书籍与引用。</div>
<script>const token=window.__MG_SESSION__||"";fetch('/api/knowledge_list',{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':token},body:'[]'}).then(r=>r.json()).then(data=>{const root=document.querySelector('main');const books=data.books||data.data?.books||[];if(!books.length)return;root.insertAdjacentHTML('beforeend','<section class="card"><h2>书籍</h2>'+books.map(book=>'<div>'+String(book.title||book.asset_id||'知识资产')+'</div>').join('')+'</section>')}).catch(()=>{});</script>
</main></body></html>"""


def render_book_detail_html(book_id: str) -> str:
    """Render a V2-only detail shell for one asset identifier."""
    encoded = json.dumps(str(book_id or ""), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemoryGuard 知识详情</title>
<style>body{{margin:0;padding:32px;background:#07110e;color:#e4f5ef;font:15px system-ui,sans-serif}}main{{max-width:960px;margin:auto}}a{{color:#6ee7c4}}.card{{padding:18px;margin:14px 0;border:1px solid #21443a;border-radius:14px;background:#0b1a16}}pre{{white-space:pre-wrap}}</style>
</head><body><main><p><a href="/knowledge">← 返回书架</a></p><h1>知识详情</h1><div class="card" id="detail">正在通过 V2 服务加载…</div>
<script>const id={encoded};const token=window.__MG_SESSION__||"";fetch('/api/knowledge_book',{{method:'POST',headers:{{'Content-Type':'application/json','X-Session-Token':token}},body:JSON.stringify([id])}}).then(r=>r.json()).then(data=>{{document.getElementById('detail').innerHTML='<pre>'+String(data.title||data.error||'知识资产不可用').replace(/[&<>]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]))+'</pre>'}}).catch(()=>{{}});</script>
</main></body></html>"""


def _candidate_targets(workspace: str | Path) -> dict[str, Any]:
    try:
        bindings = GroupControlService(Path(workspace).resolve()).list_bindings(include_inactive=False).get("bindings", [])
    except (GroupControlError, OSError, ValueError):
        return {"groups": [], "total": 0, "error": "group_scope_unavailable"}
    grouped: dict[str, list[str]] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping):
            continue
        group_id = str(binding.get("share_group_id") or "")
        agent_id = str(binding.get("agent_instance_id") or "")
        if group_id and agent_id:
            grouped.setdefault(group_id, []).append(agent_id)
    groups = [
        {"share_group_id": group_id, "members": sorted(set(members)), "label": group_id}
        for group_id, members in sorted(grouped.items())
    ]
    return {"groups": groups, "total": len(groups)}


def handle_knowledge_api(
    method: str,
    args: list[Any],
    workspace: str | Path = ".",
    *,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch GUI knowledge methods through V2 services only."""
    workspace_path = Path(workspace).expanduser().resolve()
    trusted = _native_context(workspace_path, context)
    scope = _scope(workspace_path, trusted)
    if scope is None or trusted is None:
        return _blocked()
    name = str(method or "").strip()
    values = list(args or [])
    try:
        if name == "knowledge_candidate_targets":
            return _candidate_targets(workspace_path)
        if name == "knowledge_search":
            query = str(values[0] if values else "")
            options = values[1] if len(values) > 1 and isinstance(values[1], Mapping) else {}
            rows = KnowledgeV2ReadonlyService(workspace_path).book(
                scope,
                query=query,
                limit=int(options.get("top_k", 20) or 20),
            )
            return {"ok": True, "status": "succeeded", "results": list(rows), "total": len(rows), "query": query}
        if name == "knowledge_candidates_list":
            service = KnowledgeV2ReadonlyService(workspace_path)
            rows = service.candidates(scope, status=str(values[1] if len(values) > 1 else "pending"))
            return {"ok": True, "status": "succeeded", "candidates": list(rows), "total": len(rows)}

        service = KnowledgeV2CommandService(workspace_path)
        try:
            if name == "knowledge_list":
                return service.list_books(scope=scope)
            if name == "knowledge_book":
                return service.book_info(str(values[0] if values else ""), scope=scope)
            if name == "knowledge_read":
                return service.read_occurrence(str(values[0] if values else ""), scope=scope)
            if name == "knowledge_deleted_list":
                return service.deleted(scope=scope)
            if name == "knowledge_candidate_targets":
                return service.candidate_targets(scope=scope)
            payload: dict[str, Any] = {}
            if name in {"knowledge_add"}:
                payload = {"path": values[0] if values else "", "title": values[1] if len(values) > 1 else ""}
            elif name in {"knowledge_reingest", "knowledge_rebuild_smart", "knowledge_book"}:
                payload = {"book_id": values[0] if values else ""}
            elif name in {"knowledge_remove"}:
                payload = {"book_id": values[0] if values else ""}
            elif name in {"knowledge_restore", "knowledge_purge_deleted"}:
                payload = {"deletion_id": values[0] if values else ""}
            elif name == "knowledge_update_settings":
                payload = {"book_id": values[0] if values else "", "settings": values[1] if len(values) > 1 else {}}
            elif name == "knowledge_candidate_review":
                payload = {
                    "candidate_id": values[0] if values else "",
                    "decision": values[1] if len(values) > 1 else "",
                    "target_group_id": values[2] if len(values) > 2 else scope.share_group_id,
                }
            elif name == "knowledge_job_status":
                payload = {"run_id": values[0] if values else ""}
                return service.task_status(payload, scope=scope, context=trusted)
            else:
                return _blocked("unknown_knowledge_method")
            operation = {
                "knowledge_add": "knowledge_source_add",
                "knowledge_reingest": "knowledge_reingest",
                "knowledge_rebuild_smart": "knowledge_rebuild_smart",
                "knowledge_remove": "knowledge_remove",
                "knowledge_restore": "knowledge_restore",
                "knowledge_purge_deleted": "knowledge_purge_deleted",
                "knowledge_update_settings": "knowledge_update_settings",
                "knowledge_candidate_review": "knowledge_candidate_review",
            }.get(name, name)
            return service.dispatch(operation, payload, scope=scope, context=trusted)
        finally:
            service.close()
    except (KnowledgeV2CommandError, KnowledgeV2ServiceError, GroupControlError, OSError, ValueError) as exc:
        return _blocked(str(getattr(exc, "code", "knowledge_v2_unavailable")))


def _escape(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


KNOWLEDGE_API_METHODS = frozenset({
    "knowledge_list", "knowledge_deleted_list", "knowledge_search",
    "knowledge_add", "knowledge_read", "knowledge_book", "knowledge_reingest",
    "knowledge_rebuild_smart", "knowledge_job_status", "knowledge_remove",
    "knowledge_restore", "knowledge_purge_deleted", "knowledge_update_settings",
    "knowledge_candidates_list", "knowledge_candidate_targets",
    "knowledge_candidate_review",
})
KNOWLEDGE_MUTATION_METHODS = frozenset({
    "knowledge_add", "knowledge_reingest", "knowledge_rebuild_smart",
    "knowledge_remove", "knowledge_restore", "knowledge_purge_deleted",
    "knowledge_update_settings", "knowledge_candidate_review",
})


def is_knowledge_method(method: str) -> bool:
    return str(method or "") in KNOWLEDGE_API_METHODS


def is_knowledge_mutation(method: str) -> bool:
    return str(method or "") in KNOWLEDGE_MUTATION_METHODS


__all__ = [
    "KNOWLEDGE_API_METHODS", "KNOWLEDGE_MUTATION_METHODS", "handle_knowledge_api",
    "is_knowledge_method", "is_knowledge_mutation", "render_book_detail_html",
    "render_bookshelf_html",
]
