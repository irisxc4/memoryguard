"""knowledge_gui：知识书库 GUI 页面渲染和 API 处理（KB5 基础版）。

提供：
- render_bookshelf_html() : 书架首页 HTML（书架卡片 + 搜索 + 添加按钮）
- render_book_detail_html(book_id) : 书籍详情页 HTML
- handle_knowledge_api(method, args, workspace) : 处理 knowledge_* API

接入 gui.py：
- do_GET /knowledge → render_bookshelf_html()
- do_GET /knowledge/book/<id> → render_book_detail_html(id)
- do_POST /api/knowledge_* → handle_knowledge_api()

KB5 仅基础版：书架列表、搜索、详情、添加书。美化（书封/小书卡/纸页卡）留后续。
"""

from __future__ import annotations

import json as _json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .data_home import resolve_data_home
from .knowledge_ingestion import create_book, ingest_book
from .knowledge_retriever import get_book_info, list_books, read_chunk, search
from .knowledge_store import KnowledgeStore, _stable_hash, open_shared_knowledge_store


def _get_store(read_only: bool = False) -> KnowledgeStore | None:
    """打开唯一全局共享知识库。"""
    return open_shared_knowledge_store(read_only=read_only)


def _run_ingest_in_thread(root_path: str, book_id: str, job_id: str) -> None:
    """后台线程执行入库，更新 job 状态。"""
    store = open_shared_knowledge_store()
    if store is None:
        return
    try:
        with store:
            store.update_job(job_id, "running", phase="indexing")
            result = ingest_book(store, book_id)
            store.update_job(
                job_id, "done", phase="complete",
                processed=result.files_processed,
                error=result.error,
            )
    except Exception as e:
        # 异常时 store 已随上下文关闭，必须用新连接写回失败状态（P1-8）
        try:
            with open_shared_knowledge_store() as error_store:
                error_store.update_job(job_id, "failed", phase="complete", error=str(e))
        except Exception:
            pass
    finally:
        try:
            store.close()
        except Exception:
            pass


def render_bookshelf_html() -> str:
    """渲染书架首页 HTML。"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MemoryGuard 知识书库</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #efe9dd; color: #2c2620; padding: 28px;
         background-image: radial-gradient(circle at 20% 10%, #f7f1e6 0%, #efe9dd 60%, #e6dcc9 100%); }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 28px; }
  h1 { font-size: 26px; color: #4a3520; letter-spacing: 1px; }
  .toolbar { display: flex; gap: 12px; align-items: center; }
  input[type="text"] { padding: 8px 12px; border: 1px solid #c9b8a0; border-radius: 4px;
                       min-width: 280px; background: #fff; }
  button { padding: 8px 16px; background: #8b6f47; color: #fff; border: none;
           border-radius: 4px; cursor: pointer; font-size: 14px; }
  button:hover { background: #6b5236; }
  button.secondary { background: #c9b8a0; }
  button.secondary:hover { background: #a89576; }
  .bookshelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
               gap: 26px; margin-top: 28px; }
  /* 书封：硬皮精装观感，侧边书脊 */
  .book-card { position: relative; height: 240px; border-radius: 4px 10px 10px 4px;
               cursor: pointer; transition: transform 0.18s, box-shadow 0.18s;
               box-shadow: 2px 6px 14px rgba(74,53,32,0.28);
               background: linear-gradient(160deg, #7a5a38, #5c4126);
               color: #f5edde; display: flex; flex-direction: column;
               justify-content: flex-end; padding: 16px 14px; overflow: hidden; }
  .book-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 10px;
                       background: rgba(0,0,0,0.28); border-radius: 4px 0 0 4px; }
  .book-card::after { content: ""; position: absolute; inset: 0;
                      background: radial-gradient(circle at 30% 15%, rgba(255,255,255,0.18), transparent 60%); }
  .book-card:hover { transform: translateY(-6px) rotate(-0.5deg); box-shadow: 4px 12px 22px rgba(74,53,32,0.38); }
  .book-card.add { background: repeating-linear-gradient(135deg, #d9cbb4, #d9cbb4 10px, #e2d5c0 10px, #e2d5c0 20px);
                   color: #6b5236; align-items: center; justify-content: center;
                   border: 2px dashed #b39a78; box-shadow: none; min-height: 240px; }
  .book-card.add::before, .book-card.add::after { display: none; }
  .book-title { font-size: 15px; font-weight: 700; line-height: 1.4; margin-bottom: 6px;
                text-shadow: 0 1px 2px rgba(0,0,0,0.4); position: relative; z-index: 1; }
  .book-meta { font-size: 11px; color: rgba(245,237,222,0.85); position: relative; z-index: 1; }
  .book-status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px;
                 margin-top: 8px; background: rgba(0,0,0,0.25); color: #f5edde; position: relative; z-index: 1; }
  .book-status.ready { background: #3f7d4e; color: #fff; }
  .book-status.indexing { background: #3a6ea5; color: #fff; }
  .book-status.failed { background: #a53a3a; color: #fff; }
  .search-results { margin-top: 28px; }
  .result-item { background: #fff; padding: 16px; border-radius: 6px; margin-bottom: 12px;
                 border-left: 3px solid #8b6f47; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
  .result-meta { font-size: 12px; color: #8a7860; margin-bottom: 8px; }
  .result-text { font-size: 14px; line-height: 1.6; }
  .result-method { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px;
                   background: #e8dcc8; margin-left: 8px; }
  #addModal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
              background: rgba(0,0,0,0.4); z-index: 100; }
  #addModal .modal { background: #fff; padding: 24px; border-radius: 8px; max-width: 480px;
                     margin: 80px auto; }
  #addModal label { display: block; margin: 12px 0 4px; font-size: 13px; color: #5b4636; }
  #addModal input { width: 100%; padding: 8px; border: 1px solid #c9b8a0; border-radius: 4px; }
  .empty { text-align: center; padding: 60px 20px; color: #8a7860; }
  .cand-badge { display: inline-block; margin-left: 10px; padding: 3px 10px; border-radius: 12px;
                font-size: 12px; background: #3a6ea5; color: #fff; cursor: pointer; vertical-align: middle; }
</style>
</head>
<body>
<header>
  <h1>📚 知识书库</h1>
  <div class="toolbar">
    <input type="text" id="searchInput" placeholder="搜索全部书籍..." onkeydown="if(event.key==='Enter')doSearch()">
    <button onclick="doSearch()">搜索</button>
    <button class="secondary" onclick="openCandidates()" id="candBtn">记忆候选</button>
    <button class="secondary" onclick="openAddModal()">+ 添加一本书</button>
  </div>
</header>

<div id="bookshelf" class="bookshelf"></div>
<div id="searchResults" class="search-results"></div>

<div id="addModal">
  <div class="modal">
    <h3 style="margin-bottom:16px;color:#5b4636;">添加一本书</h3>
    <label>文件夹路径</label>
    <input type="text" id="bookPath" placeholder="D:\\docs\\my-project">
    <label>书名（可选）</label>
    <input type="text" id="bookTitle" placeholder="留空使用文件夹名">
    <div style="margin-top:20px;display:flex;gap:8px;justify-content:flex-end;">
      <button class="secondary" onclick="closeAddModal()">取消</button>
      <button onclick="addBook()">加入书架</button>
    </div>
  </div>
</div>

<script>
const TOKEN = window.__MG_SESSION__ || "";

async function api(method, args) {
  const resp = await fetch("/api/" + method, {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-Session-Token": TOKEN},
    body: JSON.stringify(args || []),
  });
  return resp.json();
}

const COVERS = [
  "linear-gradient(160deg,#7a5a38,#5c4126)",
  "linear-gradient(160deg,#3a6ea5,#2c5070)",
  "linear-gradient(160deg,#3f7d4e,#2c5a37)",
  "linear-gradient(160deg,#8b3f3f,#5f2a2a)",
  "linear-gradient(160deg,#5f4a8b,#3f2f5c)",
  "linear-gradient(160deg,#a06a3a,#6e4524)",
];

async function loadBooks() {
  const data = await api("knowledge_list", []);
  const shelf = document.getElementById("bookshelf");
  const results = document.getElementById("searchResults");
  results.innerHTML = "";
  if (!data.books || data.books.length === 0) {
    shelf.innerHTML = '<div class="empty">书架为空，点击「添加一本书」开始</div>';
    return;
  }
  let html = "";
  for (let i = 0; i < data.books.length; i++) {
    const b = data.books[i];
    const statusClass = b.status || "ready";
    const cover = COVERS[i % COVERS.length];
    html += `<div class="book-card" style="background:${cover}" onclick="location.href='/knowledge/book/${b.book_id}'">
      <div class="book-title">${escapeHtml(b.title)}</div>
      <div class="book-meta">${b.file_count||0} 文件 · ${b.chunk_count||0} 片段 · ${b.chapter_count||0} 章节</div>
      <span class="book-status ${statusClass}">${b.status||"ready"}</span>
    </div>`;
  }
  html += '<div class="book-card add" onclick="openAddModal()">+ 添加一本书</div>';
  shelf.innerHTML = html;
  refreshCandCount();
}

async function refreshCandCount() {
  try {
    const data = await api("knowledge_candidates_list", ["", "pending"]);
    const n = data.total || 0;
    const btn = document.getElementById("candBtn");
    if (btn) btn.textContent = n > 0 ? "记忆候选 (" + n + ")" : "记忆候选";
  } catch (e) {}
}

async function openCandidates() {
  const data = await api("knowledge_candidates_list", ["", "pending"]);
  const shelf = document.getElementById("bookshelf");
  const results = document.getElementById("searchResults");
  shelf.innerHTML = "";
  if (!data.candidates || data.candidates.length === 0) {
    results.innerHTML = '<div class="empty">暂无待审核的记忆候选</div>';
    return;
  }
  let html = '<h3 style="margin-bottom:12px;color:#4a3520;">待审核记忆候选（' +
             data.candidates.length + '）</h3>';
  for (const c of data.candidates) {
    html += `<div class="result-item">
      <div class="result-meta">📌 ${c.source||""} · 置信度 ${c.confidence||0}
        <span class="result-method">${c.category||"knowledge"}</span></div>
      <div class="result-text">${escapeHtml(c.content||"")}</div>
      <div style="margin-top:10px;display:flex;gap:8px;">
        <button style="background:#3f7d4e" onclick="review('${c.candidate_id}','approve')">采纳</button>
        <button class="secondary" onclick="review('${c.candidate_id}','reject')">忽略</button>
      </div>
    </div>`;
  }
  results.innerHTML = html;
}

async function review(id, decision) {
  await api("knowledge_candidate_review", [id, decision]);
  openCandidates();
  refreshCandCount();
}

async function doSearch() {
  const q = document.getElementById("searchInput").value.trim();
  if (!q) { loadBooks(); return; }
  const data = await api("knowledge_search", [q]);
  const shelf = document.getElementById("bookshelf");
  const results = document.getElementById("searchResults");
  shelf.innerHTML = "";
  if (!data.results || data.results.length === 0) {
    results.innerHTML = '<div class="empty">未找到匹配的知识片段</div>';
    return;
  }
  let html = `<h3 style="margin-bottom:12px;color:#5b4636;">搜索结果（${data.results.length}）</h3>`;
  for (const r of data.results) {
    html += `<div class="result-item">
      <div class="result-meta">📖 ${escapeHtml(r.book_title||"")} · ${escapeHtml(r.chapter||"")}
        · ${escapeHtml(r.relative_path||"")} : ${r.line_start||0}-${r.line_end||0}
        <span class="result-method">${r.retrieval_method||"fts"}</span></div>
      <div class="result-text">${escapeHtml(r.text||"").slice(0, 300)}...</div>
    </div>`;
  }
  results.innerHTML = html;
}

function openAddModal() { document.getElementById("addModal").style.display = "block"; }
function closeAddModal() { document.getElementById("addModal").style.display = "none"; }

async function addBook() {
  const path = document.getElementById("bookPath").value.trim();
  const title = document.getElementById("bookTitle").value.trim();
  if (!path) { alert("请输入文件夹路径"); return; }
  closeAddModal();
  const data = await api("knowledge_add", [path, title]);
  if (data.error) { alert(data.error); return; }
  alert("已添加: " + (data.title||"") + "\\n正在后台整理...");
  loadBooks();
}

function escapeHtml(s) {
  return String(s||"").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

loadBooks();
</script>
</body>
</html>
"""


def render_book_detail_html(book_id: str) -> str:
    """渲染书籍详情页 HTML。"""
    store = _get_store(read_only=True)
    if store is None:
        return "<html><body><h1>知识库未初始化</h1><p><a href='/knowledge'>返回书架</a></p></body></html>"
    with store:
        info = get_book_info(store, book_id)
        if not info:
            return "<html><body><h1>书籍不存在</h1><p><a href='/knowledge'>返回书架</a></p></body></html>"
        chapters = info.get("chapters", [])
        documents = info.get("documents", [])
        chapters_html = "".join(f"<li>{_escape(c)}</li>" for c in chapters)
        docs_html = "".join(
            f"<li>{_escape(d['relative_path'])} <span class='status'>({_escape(d['status'])})</span></li>"
            for d in documents
        )
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{_escape(info['title'])} - 知识书库</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #e8dfd0; color: #3a2f23; padding: 28px; }}
  .paper {{ background: #fdf9ef; border-radius: 6px; padding: 28px 32px;
            box-shadow: 0 2px 10px rgba(74,53,32,0.18);
            max-width: 860px; margin: 30px auto; line-height: 1.7;
            background-image: linear-gradient(to bottom, rgba(0,0,0,0.02) 1px, transparent 1px);
            background-size: 100% 28px; }}
  header {{ margin-bottom: 20px; }}
  h1 {{ color: #5b4636; font-size: 22px; }}
  .meta {{ color: #8a7860; font-size: 13px; margin: 8px 0; }}
  section {{ background: #fff; padding: 20px; border-radius: 8px; margin-bottom: 16px;
             box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
  h2 {{ font-size: 16px; color: #5b4636; margin-bottom: 12px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 6px 0; border-bottom: 1px solid #f0ebe2; font-size: 14px; }}
  .status {{ color: #8a7860; font-size: 12px; }}
  .search-bar {{ display: flex; gap: 8px; margin-bottom: 12px; }}
  input {{ flex: 1; padding: 8px; border: 1px solid #c9b8a0; border-radius: 4px; }}
  button {{ padding: 8px 16px; background: #8b6f47; color: #fff; border: none;
           border-radius: 4px; cursor: pointer; }}
  .result {{ background: #faf7f0; padding: 12px; border-radius: 4px; margin-top: 8px;
             border-left: 3px solid #8b6f47; }}
  a {{ color: #8b6f47; text-decoration: none; }}
</style>
</head>
<body>
<div class="paper">
<header>
  <a href="/knowledge">← 返回书架</a>
  <h1>📖 {_escape(info['title'])}</h1>
  <div class="meta">{info.get('file_count',0)} 文件 · {info.get('chunk_count',0)} 片段
      · {info.get('chapter_count',0)} 章节 · {info.get('entity_count',0)} 知识点
      · 状态: {_escape(info.get('status',''))}</div>
  {f'<div class="meta">{_escape(info.get("description",""))}</div>' if info.get('description') else ''}
</header>

<section>
  <h2>搜索本书</h2>
  <div class="search-bar">
    <input type="text" id="q" placeholder="输入关键词搜索本书内容..." onkeydown="if(event.key==='Enter')searchBook()">
    <button onclick="searchBook()">搜索</button>
  </div>
  <div id="results"></div>
</section>

<section>
  <h2>章节目录</h2>
  <ul>{chapters_html or '<li class="status">暂无章节</li>'}</ul>
</section>

<section>
  <h2>文档列表</h2>
  <ul>{docs_html or '<li class="status">暂无文档</li>'}</ul>
</section>

</div>
<script>
async function searchBook() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const resp = await fetch('/api/knowledge_search', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json', 'X-Session-Token': window.__MG_SESSION__||''}},
    body: JSON.stringify([q, {{"book_ids": ["{book_id}"]}}])
  }});
  const data = await resp.json();
  const box = document.getElementById('results');
  if (!data.results || !data.results.length) {{
    box.innerHTML = '<p class="status">未找到匹配片段</p>';
    return;
  }}
  let html = '';
  for (const r of data.results) {{
    html += `<div class="result"><div class="meta">${{r.chapter||''}} · ${{r.relative_path||''}} : ${{r.line_start||0}}-${{r.line_end||0}}</div><div>${{(r.text||'').slice(0,400)}}...</div></div>`;
  }}
  box.innerHTML = html;
}}
</script>
</body>
</html>
"""


def handle_knowledge_api(method: str, args: list[Any],
                         workspace: str | Path = ".") -> dict[str, Any]:
    """处理 knowledge_* API 调用（KB5）。

    方法：
    - knowledge_list() : 列出所有书（只读）
    - knowledge_search(query, opts?) : 搜索（只读）
    - knowledge_add(path, title?) : 添加一本书并入库（写）
    - knowledge_read(chunk_id) : 读取单个 chunk（只读）
    - knowledge_book(book_id) : 获取书籍详情（只读）
    - knowledge_reingest(book_id) : 重新整理一本书（写）
    """
    write = method in {"knowledge_add", "knowledge_reingest", "knowledge_remove",
                   "knowledge_candidate_review"}
    store = _get_store(read_only=not write)
    if store is None:
        return {"error": "不能打开知识库（未初始化）"}

    with store:
        if method == "knowledge_list":
            books = list_books(store)
            return {"books": books, "total": len(books)}

        if method == "knowledge_search":
            if not args:
                return {"error": "query required"}
            query = str(args[0])
            opts = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            book_ids = opts.get("book_ids")
            top_k = int(opts.get("top_k", 6))
            results = search(store, query, book_ids=book_ids, top_k=top_k)
            return {"results": results, "total": len(results), "query": query}

        if method == "knowledge_add":
            if not args:
                return {"error": "path required"}
            root_path = str(args[0])
            title = str(args[1]) if len(args) > 1 and args[1] else ""
            if not Path(root_path).is_dir():
                return {"error": f"path not found: {root_path}"}
            book = create_book(store, root_path, title=title)
            job_id = _stable_hash("gui-job", book.book_id, str(time.time()))
            store.create_job(job_id, book.book_id, total_files=0)
            # 后台线程入库，避免阻塞单线程 HTTP 请求（P0-5 同步阻塞门槛）
            threading.Thread(
                target=_run_ingest_in_thread,
                args=(root_path, book.book_id, job_id),
                daemon=True,
            ).start()
            return {
                "ok": True,
                "book_id": book.book_id,
                "title": book.title,
                "job_id": job_id,
                "status": "indexing",
                "deferred": True,
            }

        if method == "knowledge_read":
            if not args:
                return {"error": "chunk_id required"}
            chunk = read_chunk(store, str(args[0]))
            return chunk if chunk else {"error": "chunk not found"}

        if method == "knowledge_book":
            if not args:
                return {"error": "book_id required"}
            info = get_book_info(store, str(args[0]))
            return info if info else {"error": "book not found"}

        if method == "knowledge_reingest":
            if not args:
                return {"error": "book_id required"}
            book_id = str(args[0])
            job_id = _stable_hash("gui-job", book_id, str(time.time()))
            store.create_job(job_id, book_id, total_files=0)
            threading.Thread(
                target=_run_ingest_in_thread,
                args=(store.get_book(book_id).root_path, book_id, job_id),
                daemon=True,
            ).start()
            return {"ok": True, "job_id": job_id, "status": "indexing", "deferred": True}

        if method == "knowledge_job_status":
            if not args:
                return {"error": "job_id required"}
            job = store.get_job(str(args[0]))
            if not job:
                return {"error": "job not found"}
            return {
                "job_id": job["job_id"],
                "status": job["status"],
                "phase": job["phase"],
                "processed": job["processed_files"],
                "total": job["total_files"],
                "error": job["error"],
            }

        if method == "knowledge_remove":
            if not args:
                return {"error": "book_id required"}
            book_id = str(args[0])
            book = store.get_book(book_id)
            if not book:
                return {"error": "book not found"}
            store.remove_book(book_id)
            return {"ok": True, "book_id": book_id, "title": book.title}

        if method == "knowledge_candidates_list":
            book_id = str(args[0]) if args and args[0] else None
            status = str(args[1]) if len(args) > 1 and args[1] else "pending"
            candidates = store.list_memory_candidates(book_id=book_id, status=status)
            return {"candidates": candidates, "total": len(candidates)}

        if method == "knowledge_candidate_review":
            if len(args) < 2:
                return {"error": "candidate_id and decision required"}
            candidate_id = str(args[0])
            decision = str(args[1])  # approve / reject
            ok = store.review_memory_candidate(candidate_id, decision)
            if not ok:
                return {"error": "candidate not found or invalid decision"}
            # P1-2 候选闭环：批准时同步到长期记忆，并记录 synced_memory_id
            synced = None
            if {"approve": "approved", "reject": "rejected"}.get(decision, decision) == "approved":
                synced = _sync_candidate_to_memory(store, candidate_id, workspace)
            return {
                "ok": True,
                "candidate_id": candidate_id,
                "status": decision,
                "synced_memory_id": synced,
            }

    return {"error": f"unknown knowledge method: {method}"}


def _sync_candidate_to_memory(store, candidate_id: str, workspace) -> str | None:
    """把已批准的候选写入共享长期记忆，返回 memory_id（失败返回 None，不阻塞审核）。

    P1-2 候选闭环：候选经 GovernanceEngine.auto_write 写入共享记忆层，
    以 candidate_id 作幂等键避免重复写入。未配置共享组时回退个人组。
    """
    try:
        cand = store.get_memory_candidate(candidate_id)
        if not cand or not (cand.get("content") or "").strip():
            return None
        body = str(cand["content"]).strip()

        from pathlib import Path
        ws = Path(workspace) if workspace else Path.cwd()

        # 解析目标共享组：优先活跃绑定，否则个人组
        group_id = ""
        try:
            from .agent_binding import AgentBindingStore, BindingStatus, personal_group_id
            binds = AgentBindingStore(ws).list_bindings(include_inactive=False)
            active = [b for b in binds if getattr(b, "status", None) == BindingStatus.ACTIVE]
            if active:
                group_id = active[0].share_group_id
            actor = active[0].agent_instance_id if active else ""
        except Exception:
            actor = ""
        if not group_id:
            try:
                from .agent_binding import personal_group_id as _pg
                group_id = _pg(actor or "knowledge")
            except Exception:
                group_id = "default"
        if not actor:
            actor = "knowledge"

        from .governance_engine import GovernanceEngine
        from .schema_v3 import MemoryEvent, stable_hash, _now_iso

        event = MemoryEvent(
            event_id=stable_hash("mc-event", candidate_id),
            agent_instance_id=actor,
            share_group_id=group_id,
            raw_content=body,
            metadata={
                "source": cand.get("source", ""),
                "category": cand.get("category", "knowledge"),
                "confidence": float(cand.get("confidence", 0.5) or 0.5),
                "knowledge_candidate_id": candidate_id,
                "book_id": cand.get("book_id", ""),
            },
            auto_actions=[],
            created_at=_now_iso(),
        )
        result = GovernanceEngine(ws, group_id).auto_write(
            event,
            kind_override="knowledge",
            write_policy="auto_accept",
            injection_policy="relevant",
            idempotency_key=f"knowledge-candidate:{candidate_id}",
        )
        if not result.get("ok"):
            return None
        memory_id = result.get("memory_id", "")
        if memory_id:
            store.set_candidate_synced(candidate_id, memory_id)
        return memory_id or None
    except Exception:
        return None


def _escape(s: str) -> str:
    """HTML 转义。"""
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;"))


KNOWLEDGE_API_METHODS = frozenset({
    "knowledge_list",
    "knowledge_search",
    "knowledge_add",
    "knowledge_read",
    "knowledge_book",
    "knowledge_reingest",
    "knowledge_job_status",
    "knowledge_remove",
    "knowledge_candidates_list",
    "knowledge_candidate_review",
})


def is_knowledge_method(method: str) -> bool:
    """判断是否为知识书库 API 方法。"""
    return method in KNOWLEDGE_API_METHODS


def is_knowledge_mutation(method: str) -> bool:
    """判断是否为变更类知识 API（需确认）。"""
    return method in {"knowledge_add", "knowledge_reingest", "knowledge_remove",
                      "knowledge_candidate_review"}
