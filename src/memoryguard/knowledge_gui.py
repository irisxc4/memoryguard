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
<style>
:root{color-scheme:dark;--bg:#040b09;--panel:#0a1915;--fg:#e4f5ef;--muted:#78988d;--line:rgba(110,231,196,.18);--strong:rgba(110,231,196,.36);--accent:#6ee7c4;--bright:#bcffeb;--red:#ff7d88}
*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:28px;background:radial-gradient(circle at 14% 10%,rgba(48,170,133,.11),transparent 30rem),var(--bg);color:var(--fg);font:14px/1.55 Inter,system-ui,"PingFang SC","Microsoft YaHei",sans-serif}header,main,.statusbar{max-width:1240px;margin:auto}a{color:var(--muted);text-decoration:none}a:hover{color:var(--bright)}header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding-bottom:22px;border-bottom:1px solid var(--line);flex-wrap:wrap}.back-link{display:inline-block;margin-bottom:12px}h1{font-size:30px;margin:0}.subtitle{margin:6px 0 0;color:var(--muted)}.toolbar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;flex:1 1 650px;justify-content:flex-end}.toolbar input{flex:1 1 260px;max-width:420px}input,select{min-height:40px;padding:9px 12px;border:1px solid var(--strong);border-radius:8px;background:#07130f;color:var(--fg);outline:none}button{min-height:39px;padding:8px 13px;border:1px solid var(--accent);border-radius:8px;background:var(--accent);color:#062019;font-weight:700;cursor:pointer}button.secondary{background:rgba(110,231,196,.04);border-color:var(--strong);color:var(--fg)}button.danger{background:rgba(255,125,136,.07);border-color:rgba(255,125,136,.5);color:var(--red)}.statusbar{margin-top:18px;padding:11px 14px;border:1px solid var(--line);border-radius:10px;background:rgba(10,25,21,.82);color:var(--muted)}.statusbar.error{border-color:rgba(255,125,136,.45);color:#ffd4d8}.bookshelf{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:20px;margin-top:24px}.book-card{position:relative;min-height:245px;padding:18px 16px 16px 25px;border:1px solid rgba(188,255,235,.13);border-radius:5px 10px 10px 5px;display:flex;flex-direction:column;justify-content:flex-end;color:#f5edde;box-shadow:4px 14px 34px rgba(0,0,0,.3);cursor:pointer;overflow:hidden;transition:.18s transform}.book-card:hover{transform:translateY(-5px)}.book-card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:11px;background:rgba(0,0,0,.3)}.book-card.add{align-items:center;justify-content:center;border:1px dashed var(--strong);background:rgba(110,231,196,.025);color:var(--bright);box-shadow:none}.book-card.add:before{display:none}.book-title{position:relative;font-weight:800;font-size:16px}.book-meta{position:relative;margin-top:7px;font-size:11px;color:rgba(245,237,222,.78)}.panel{margin-top:24px;padding:18px;border:1px solid var(--line);border-radius:11px;background:rgba(10,25,21,.88)}.panel.hidden{display:none}.result{padding:13px 14px;margin:9px 0;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;background:var(--panel)}.result-meta{color:var(--muted);font-size:11px}.result-copy{margin-top:6px;white-space:pre-wrap}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.empty{grid-column:1/-1;padding:54px 18px;text-align:center;color:var(--muted)}.modal{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.72);backdrop-filter:blur(8px)}.modal.open{display:grid;place-items:start center;padding-top:80px}.modal-card{width:min(560px,calc(100% - 28px));padding:22px;border:1px solid var(--strong);border-radius:12px;background:#0b1a16}.field{display:grid;gap:6px;margin:13px 0;color:var(--muted)}.field input{width:100%}.reader{max-height:55vh;overflow:auto;white-space:pre-wrap;padding:14px;border:1px solid var(--line);border-radius:8px;background:#06110d}@media(max-width:760px){body{padding:18px 14px}header{align-items:stretch}.toolbar{justify-content:stretch}.toolbar input{max-width:none}.bookshelf{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}.book-card{min-height:218px}}
</style>
</head><body>
<header><div><a class="back-link" href="/">← 返回治理面板</a><h1>知识书库</h1><p class="subtitle">统一管理本地书籍、检索索引、知识片段与长期记忆候选。数据由 V2 Content / Knowledge 平面提供。</p></div><div class="toolbar"><input id="searchInput" placeholder="搜索知识内容" onkeydown="if(event.key==='Enter')doSearch()"><button onclick="doSearch()">搜索</button><button class="secondary" id="candBtn" onclick="openCandidates()">记忆候选</button><button class="secondary" onclick="openDeleted()">最近删除</button><button class="secondary" onclick="openAdd()">+ 添加一本书</button></div></header>
<div id="status" class="statusbar">正在读取 V2 知识书架…</div><main><div id="bookshelf" class="bookshelf"></div><section id="results" class="panel hidden"></section></main>
<div id="addModal" class="modal"><div class="modal-card"><h2>添加一本书</h2><label class="field">来源文件夹<div style="display:flex;gap:8px"><input id="bookPath" readonly placeholder="选择一个知识目录"><button class="secondary" onclick="pickFolder()">选择</button></div></label><label class="field">书名<input id="bookTitle" placeholder="留空使用目录名"></label><div class="actions" style="justify-content:flex-end"><button class="secondary" onclick="closeAdd()">取消</button><button onclick="addBook()">加入书架</button></div></div></div>
<div id="readModal" class="modal"><div class="modal-card"><h2>知识片段</h2><div id="readBody" class="reader">正在读取…</div><div class="actions" style="justify-content:flex-end"><button class="secondary" onclick="closeReader()">关闭</button></div></div></div>
<script>
const TOKEN=window.__MG_SESSION__||"";
const COVERS=["linear-gradient(155deg,#245f46,#123c2c)","linear-gradient(155deg,#365f73,#203d4a)","linear-gradient(155deg,#62643a,#3d4024)","linear-gradient(155deg,#75494d,#482b2e)","linear-gradient(155deg,#505b72,#303747)","linear-gradient(155deg,#476b5c,#294238)"];
function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}
function unpack(raw){const nested=raw&&raw.data&&typeof raw.data==='object'&&!Array.isArray(raw.data)?raw.data:{};return {...raw,...nested,task:raw?.task||nested?.task}}
function errorText(v,f="操作失败"){return typeof v?.error==='string'?v.error:(v?.error?.message||v?.error?.code||v?.code||f)}
async function api(name,args=[]){const response=await fetch('/api/'+name,{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':TOKEN},body:JSON.stringify(args)});const raw=await response.json().catch(()=>({}));if(!response.ok||raw?.ok===false)throw new Error(errorText(raw,name+' 失败'));return unpack(raw)}
function setStatus(text,isError=false){const node=document.getElementById('status');node.textContent=text;node.classList.toggle('error',isError)}
function showResults(title,html){const node=document.getElementById('results');node.classList.remove('hidden');node.innerHTML='<h2>'+esc(title)+'</h2>'+html;document.getElementById('bookshelf').innerHTML=''}
function hideResults(){const node=document.getElementById('results');node.classList.add('hidden');node.innerHTML=''}
async function loadBooks(){try{const data=await api('knowledge_list',['',100]);const books=data.books||[];hideResults();const shelf=document.getElementById('bookshelf');if(!books.length){shelf.innerHTML='<div class="empty"><strong>书架暂时为空</strong><br>点击「添加一本书」选择本地知识目录。</div><article class="book-card add" onclick="openAdd()">+ 添加一本书</article>';setStatus('当前治理范围没有 V2 知识资产');return}let html='';books.forEach((book,index)=>{html+=`<article class="book-card" style="background:${COVERS[index%COVERS.length]}" onclick="location.href='/knowledge/book/${encodeURIComponent(book.book_id)}'"><div class="book-title">${esc(book.title||'未命名知识资产')}</div><div class="book-meta">${Number(book.file_count||0)} 文件 · ${Number(book.chunk_count||0)} 片段<br>${esc(book.updated_at||'')} · ${esc(book.status||'active')}</div></article>`});html+='<article class="book-card add" onclick="openAdd()">+ 添加一本书</article>';shelf.innerHTML=html;setStatus(`已加载 ${books.length} 本书 · V2 knowledge registry`);refreshCandidates()}catch(error){setStatus('知识书架加载失败：'+error.message,true);document.getElementById('bookshelf').innerHTML='<div class="empty">无法读取知识书架。</div>'}}
async function doSearch(){const query=document.getElementById('searchInput').value.trim();if(!query)return loadBooks();try{const data=await api('knowledge_search',[query,50]);const rows=data.results||[];showResults(`搜索结果 · ${rows.length}`,rows.map(row=>`<article class="result"><div class="result-meta">reference-only · ${esc(row.hash||'')}</div><div class="result-copy">${esc(row.summary||'匹配知识片段')}</div><div class="actions"><button class="secondary" onclick="readOccurrence('${esc(row.occurrence_id||'')}')">读取片段</button></div></article>`).join('')||'<div class="empty">没有匹配知识。</div>');setStatus(`“${query}” · ${rows.length} 个匹配`)}catch(error){setStatus('知识搜索失败：'+error.message,true)}}
async function readOccurrence(id){if(!id)return;document.getElementById('readModal').classList.add('open');document.getElementById('readBody').textContent='正在读取…';try{const data=await api('knowledge_read',[id]);document.getElementById('readBody').textContent=data.text||''}catch(error){document.getElementById('readBody').textContent='读取失败：'+error.message}}
function closeReader(){document.getElementById('readModal').classList.remove('open')}
async function refreshCandidates(){try{const data=await api('knowledge_candidates_list',['','pending']);const count=Number(data.total||data.references?.length||0);document.getElementById('candBtn').textContent=count?`记忆候选 (${count})`:'记忆候选'}catch(_){}}
async function openCandidates(){try{const [data,targets]=await Promise.all([api('knowledge_candidates_list',['','pending']),api('knowledge_candidate_targets',[])]);const rows=data.references||data.candidates||[];const groups=targets.groups||[];let targetHtml=groups.length?`<article class="result"><div class="result-meta">同步目标</div><select id="candidateTarget">${groups.map(group=>`<option value="${esc(group.share_group_id)}">${esc(group.label||group.share_group_id)}</option>`).join('')}</select></article>`:'';const list=rows.map(item=>`<article class="result"><div class="result-meta">${esc(item.ref||'knowledge')} · ${esc(item.status||'pending')}</div><div class="result-copy">${esc(item.summary||'')}</div><div class="actions"><button onclick="reviewCandidate('${esc(item.candidate_id)}','approve')">采纳</button><button class="secondary" onclick="reviewCandidate('${esc(item.candidate_id)}','keep')">暂不处理</button><button class="secondary" onclick="reviewCandidate('${esc(item.candidate_id)}','reject')">忽略</button></div></article>`).join('');showResults(`待审核记忆候选 · ${rows.length}`,targetHtml+(list||'<div class="empty">暂无待审核候选。</div>'));setStatus('记忆候选由 V2 引用平面提供')}catch(error){setStatus('候选加载失败：'+error.message,true)}}
async function reviewCandidate(id,decision){const target=document.getElementById('candidateTarget');try{await api('knowledge_candidate_review',[id,decision,target?.value||'']);await openCandidates();await refreshCandidates()}catch(error){setStatus('候选处理失败：'+error.message,true)}}
async function openDeleted(){try{const data=await api('knowledge_deleted_list',[]);const rows=data.items||[];const html=rows.map(item=>`<article class="result"><div class="result-meta">${esc(item.deleted_at||'')}</div><div class="result-copy"><strong>${esc(item.title||item.book_id)}</strong></div><div class="actions"><button onclick="restoreDeleted('${esc(item.deletion_id||'')}')">恢复</button><button class="danger" onclick="purgeDeleted('${esc(item.deletion_id||'')}')">永久清理</button></div></article>`).join('');showResults(`最近删除 · ${rows.length}`,html||'<div class="empty">最近没有已删除书籍。</div>');setStatus('知识回收站')}catch(error){setStatus('删除记录读取失败：'+error.message,true)}}
async function restoreDeleted(id){try{await api('knowledge_restore',[id]);await loadBooks()}catch(error){setStatus('恢复失败：'+error.message,true)}}
async function purgeDeleted(id){if(!confirm('永久清理后无法恢复，是否继续？'))return;try{await api('knowledge_purge_deleted',[id]);await openDeleted()}catch(error){setStatus('永久清理失败：'+error.message,true)}}
function openAdd(){document.getElementById('addModal').classList.add('open')}function closeAdd(){document.getElementById('addModal').classList.remove('open')}
async function pickFolder(){try{const data=await api('pick_path',[false]);if(data.path)document.getElementById('bookPath').value=data.path}catch(error){setStatus('目录选择失败：'+error.message,true)}}
async function waitJob(id){for(let i=0;i<180;i++){const data=await api('knowledge_job_status',[id]);const state=String(data.task?.state||data.status||'').toLowerCase();if(['succeeded','failed','cancelled'].includes(state)){if(state!=='succeeded')throw new Error(errorText(data,'知识整理失败'));return data}await new Promise(resolve=>setTimeout(resolve,500))}throw new Error('知识整理等待超时')}
async function addBook(){const path=document.getElementById('bookPath').value.trim(),title=document.getElementById('bookTitle').value.trim();if(!path)return setStatus('请选择知识目录',true);closeAdd();try{const data=await api('knowledge_add',[path,title]);if(data.job_id){setStatus('知识入库任务已创建，正在整理…');await waitJob(data.job_id)}await loadBooks()}catch(error){setStatus('添加失败：'+error.message,true)}}
loadBooks();
</script></body></html>"""


def render_book_detail_html(book_id: str) -> str:
    """Render a V2-only detail shell for one asset identifier."""
    encoded = json.dumps(str(book_id or ""), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MemoryGuard 知识详情</title>
<style>
:root{{color-scheme:dark;--bg:#040b09;--panel:#0a1915;--fg:#e4f5ef;--muted:#78988d;--line:rgba(110,231,196,.18);--strong:rgba(110,231,196,.36);--accent:#6ee7c4;--bright:#bcffeb;--red:#ff7d88}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 Inter,system-ui,"PingFang SC",sans-serif}}a{{color:var(--muted);text-decoration:none}}button{{min-height:38px;padding:8px 12px;border:1px solid var(--accent);border-radius:7px;background:var(--accent);color:#062019;font-weight:700;cursor:pointer}}button.secondary{{background:rgba(110,231,196,.04);border-color:var(--strong);color:var(--fg)}}button.danger{{background:rgba(255,125,136,.06);border-color:rgba(255,125,136,.5);color:var(--red)}}input{{width:100%;min-height:40px;padding:9px;border:1px solid var(--strong);border-radius:7px;background:#07130f;color:var(--fg)}}.mast{{padding:28px 32px;border-bottom:1px solid var(--line);background:radial-gradient(circle at 15% 10%,rgba(48,170,133,.12),transparent 30rem)}}.mast>div,.layout,.stats{{max-width:1180px;margin:auto}}.back{{display:inline-block;margin-bottom:18px}}.title-row{{display:flex;justify-content:space-between;gap:20px;align-items:flex-end}}.title-row h1{{margin:0;font-size:30px}}.muted{{color:var(--muted)}}.badge{{padding:5px 10px;border-radius:999px;background:rgba(110,231,196,.12);color:var(--bright)}}.stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;padding:18px 24px 0}}.stat,.card{{padding:14px;border:1px solid var(--line);border-radius:9px;background:var(--panel)}}.stat strong{{display:block;font-size:22px;color:var(--bright)}}.layout{{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:28px;padding:24px}}.card{{margin-bottom:14px;padding:18px}}.card h2{{margin:0 0 12px;font-size:17px}}.doc{{padding:12px 0;border-top:1px solid var(--line)}}.doc:first-child{{border-top:0}}.doc-title{{font-weight:700}}.doc-meta{{font-size:11px;color:var(--muted);margin:5px 0}}.chunks,.actions{{display:flex;gap:6px;flex-wrap:wrap}}.chunk{{min-height:30px;padding:5px 9px;background:rgba(110,231,196,.04);border:1px solid var(--strong);color:var(--fg)}}.search{{display:grid;grid-template-columns:1fr auto;gap:8px}}.reader{{max-height:420px;overflow:auto;white-space:pre-wrap;padding:14px;border:1px solid var(--line);border-radius:8px;background:#06110d}}.setting{{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}}.side-actions{{display:grid;gap:8px;margin-top:14px}}.result{{margin-top:9px;padding:12px;border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:7px}}@media(max-width:850px){{.layout{{grid-template-columns:1fr}}.stats{{grid-template-columns:1fr}}.title-row{{align-items:flex-start;flex-direction:column}}}}
</style>
</head><body><header class="mast"><div><a class="back" href="/knowledge">← 返回书架</a><div class="title-row"><div><h1 id="title">知识详情</h1><p id="updated" class="muted">正在读取 V2 知识资产…</p></div><span id="status" class="badge">loading</span></div></div></header>
<div class="stats"><article class="stat"><strong id="files">0</strong><span>文件</span></article><article class="stat"><strong id="chunks">0</strong><span>知识片段</span></article><article class="stat"><strong id="generation">0</strong><span>索引代次</span></article></div>
<main class="layout"><div><section class="card"><h2>搜索本书</h2><div class="search"><input id="q" placeholder="搜索当前知识范围" onkeydown="if(event.key==='Enter')searchBook()"><button onclick="searchBook()">搜索</button></div><div id="results"></div></section><section class="card"><h2>文档与片段</h2><div id="documents" class="muted">正在读取文档…</div></section><section class="card"><h2>片段阅读</h2><div id="reader" class="reader muted">点击文档下的片段编号按需读取原文。</div></section></div><aside><section class="card"><h2>书籍设置</h2><div id="settings"></div><div class="side-actions"><button class="secondary" onclick="reingest()">重新整理</button><button class="secondary" onclick="rebuild()">重建智能索引</button><button class="danger" onclick="removeBook()">移入回收站</button></div></section></aside></main>
<script>const BOOK_ID={encoded},TOKEN=window.__MG_SESSION__||"";let BOOK=null;
function esc(v){{return String(v??"").replace(/[&<>"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]))}}function unpack(raw){{const nested=raw&&raw.data&&typeof raw.data==='object'&&!Array.isArray(raw.data)?raw.data:{{}};return {{...raw,...nested,task:raw?.task||nested?.task}}}}function errorText(v,f='操作失败'){{return typeof v?.error==='string'?v.error:(v?.error?.message||v?.error?.code||v?.code||f)}}
async function api(name,args=[]){{const response=await fetch('/api/'+name,{{method:'POST',headers:{{'Content-Type':'application/json','X-Session-Token':TOKEN}},body:JSON.stringify(args)}});const raw=await response.json().catch(()=>({{}}));if(!response.ok||raw?.ok===false)throw new Error(errorText(raw,name+' 失败'));return unpack(raw)}}
async function load(){{try{{BOOK=await api('knowledge_book',[BOOK_ID,'',100]);document.getElementById('title').textContent=BOOK.title||'知识详情';document.title=(BOOK.title||'知识详情')+' - MemoryGuard';document.getElementById('updated').textContent=BOOK.updated_at||'';document.getElementById('status').textContent=BOOK.status||'active';document.getElementById('files').textContent=BOOK.file_count||0;document.getElementById('chunks').textContent=BOOK.chunk_count||0;document.getElementById('generation').textContent=BOOK.index_generation||0;renderDocs();renderSettings()}}catch(error){{document.getElementById('documents').textContent='加载失败：'+error.message}}}}
function renderDocs(){{const docs=BOOK.documents||[];document.getElementById('documents').innerHTML=docs.map(doc=>`<article class="doc"><div class="doc-title">${{esc(doc.title||doc.relative_path||'文档')}}</div><div class="doc-meta">${{esc(doc.relative_path||'')}} · ${{Number(doc.chunk_count||0)}} 片段 · ${{esc(doc.media_type||'')}}</div><div class="chunks">${{(doc.occurrence_ids||[]).map((id,index)=>`<button class="chunk" onclick="readChunk('${{esc(id)}}')">片段 ${{index+1}}</button>`).join('')}}</div></article>`).join('')||'<div class="muted">暂无可展示文档。</div>'}}
function renderSettings(){{const settings=BOOK.settings||{{}},labels={{remote_embedding_allowed:'远程文档处理',remote_query_embedding_allowed:'远程查询向量',auto_extract_memory:'自动提取记忆候选',vector_enabled:'向量索引'}};document.getElementById('settings').innerHTML=Object.keys(labels).map(key=>`<div class="setting"><span>${{labels[key]}}</span><button class="secondary" onclick="toggleSetting('${{key}}',${{!Boolean(settings[key])}})">${{settings[key]?'已启用':'未启用'}}</button></div>`).join('')}}
async function readChunk(id){{const box=document.getElementById('reader');box.textContent='正在读取…';box.classList.remove('muted');try{{const data=await api('knowledge_read',[id]);box.textContent=data.text||''}}catch(error){{box.textContent='读取失败：'+error.message}}}}
async function searchBook(){{const query=document.getElementById('q').value.trim();if(!query)return;const box=document.getElementById('results');box.innerHTML='<div class="muted">正在检索…</div>';try{{const data=await api('knowledge_search',[query,50]);const rows=data.results||[];box.innerHTML=rows.map(row=>`<article class="result"><div class="muted">reference-only</div><div>${{esc(row.summary||'匹配片段')}}</div><div class="actions"><button class="secondary" onclick="readChunk('${{esc(row.occurrence_id||'')}}')">读取原文</button></div></article>`).join('')||'<div class="muted">未找到匹配片段。</div>'}}catch(error){{box.textContent='搜索失败：'+error.message}}}}
async function toggleSetting(key,value){{try{{await api('knowledge_update_settings',[BOOK_ID,{{[key]:value}}]);await load()}}catch(error){{alert(error.message)}}}}
async function waitJob(id){{for(let index=0;index<180;index++){{const data=await api('knowledge_job_status',[id]);const state=String(data.task?.state||data.status||'').toLowerCase();if(['succeeded','failed','cancelled'].includes(state)){{if(state!=='succeeded')throw new Error(errorText(data,'任务失败'));return}}await new Promise(resolve=>setTimeout(resolve,500))}}throw new Error('任务等待超时')}}
async function runTask(name){{try{{const data=await api(name,[BOOK_ID]);if(data.job_id)await waitJob(data.job_id);await load()}}catch(error){{alert(error.message)}}}}async function reingest(){{return runTask('knowledge_reingest')}}async function rebuild(){{return runTask('knowledge_rebuild_smart')}}async function removeBook(){{if(!confirm('书籍将移入回收站，原始文件不会删除。是否继续？'))return;try{{await api('knowledge_remove',[BOOK_ID]);location.href='/knowledge'}}catch(error){{alert(error.message)}}}}
load();
</script></body></html>"""


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
