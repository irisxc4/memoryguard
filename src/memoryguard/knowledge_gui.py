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
        try:
            store.update_job(job_id, "failed", phase="complete", error=str(e))
        except Exception:
            pass
    finally:
        store.close()


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
         background: #f5f3ee; color: #2c2620; padding: 24px; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
  h1 { font-size: 24px; color: #5b4636; }
  .toolbar { display: flex; gap: 12px; align-items: center; }
  input[type="text"] { padding: 8px 12px; border: 1px solid #c9b8a0; border-radius: 4px;
                       min-width: 280px; background: #fff; }
  button { padding: 8px 16px; background: #8b6f47; color: #fff; border: none;
           border-radius: 4px; cursor: pointer; font-size: 14px; }
  button:hover { background: #6b5236; }
  button.secondary { background: #c9b8a0; }
  button.secondary:hover { background: #a89576; }
  .bookshelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
               gap: 20px; margin-top: 24px; }
  .book-card { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08);
               cursor: pointer; transition: transform 0.15s; border-left: 6px solid #8b6f47; }
  .book-card:hover { transform: translateY(-3px); box-shadow: 0 4px 16px rgba(0,0,0,0.12); }
  .book-card.add { border-left-color: #c9b8a0; display: flex; align-items: center; justify-content: center;
                   min-height: 120px; color: #8b6f47; font-size: 14px; text-align: center; }
  .book-title { font-size: 16px; font-weight: 600; margin-bottom: 8px; color: #2c2620; }
  .book-meta { font-size: 12px; color: #8a7860; }
  .book-status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px;
                 margin-top: 8px; background: #e8dcc8; color: #5b4636; }
  .book-status.ready { background: #d4edda; color: #155724; }
  .book-status.indexing { background: #cce5ff; color: #004085; }
  .book-status.failed { background: #f8d7da; color: #721c24; }
  .search-results { margin-top: 24px; }
  .result-item { background: #fff; padding: 16px; border-radius: 6px; margin-bottom: 12px;
                 border-left: 3px solid #8b6f47; }
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
</style>
</head>
<body>
<header>
  <h1>📚 知识书库</h1>
  <div class="toolbar">
    <input type="text" id="searchInput" placeholder="搜索全部书籍..." onkeydown="if(event.key==='Enter')doSearch()">
    <button onclick="doSearch()">搜索</button>
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
  for (const b of data.books) {
    const statusClass = b.status || "ready";
    html += `<div class="book-card" onclick="location.href='/knowledge/book/${b.book_id}'">
      <div class="book-title">${escapeHtml(b.title)}</div>
      <div class="book-meta">${b.file_count||0} 文件 · ${b.chunk_count||0} 片段 · ${b.chapter_count||0} 章节</div>
      <span class="book-status ${statusClass}">${b.status||"ready"}</span>
    </div>`;
  }
  shelf.innerHTML = html;
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
         background: #f5f3ee; color: #2c2620; padding: 24px; }}
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
    write = method in {"knowledge_add", "knowledge_reingest", "knowledge_remove"}
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

    return {"error": f"unknown knowledge method: {method}"}


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
})


def is_knowledge_method(method: str) -> bool:
    """判断是否为知识书库 API 方法。"""
    return method in KNOWLEDGE_API_METHODS


def is_knowledge_mutation(method: str) -> bool:
    """判断是否为变更类知识 API（需确认）。"""
    return method in {"knowledge_add", "knowledge_reingest", "knowledge_remove"}
