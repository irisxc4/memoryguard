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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data_home import resolve_data_home
from .knowledge_ingestion import create_book, ingest_book, rebuild_smart_indexes
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
  :root {
    color-scheme: dark;
    --bg: #040b09;
    --panel: rgba(10, 25, 21, 0.88);
    --panel-solid: #0b1a16;
    --panel-bright: #10251f;
    --fg: #e4f5ef;
    --muted: #78988d;
    --faint: #48685e;
    --line: rgba(110, 231, 196, 0.16);
    --line-strong: rgba(110, 231, 196, 0.34);
    --accent: #6ee7c4;
    --accent-bright: #bcffeb;
    --red: #ff7d88;
    --orange: #e9bb64;
    --shadow: 0 24px 70px rgba(0, 0, 0, 0.32);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { min-height: 100%; }
  body { position: relative; min-height: 100dvh; padding: 28px;
         font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         background:
           radial-gradient(circle at 14% 12%, rgba(48, 170, 133, 0.10), transparent 30rem),
           radial-gradient(circle at 84% 82%, rgba(78, 150, 125, 0.07), transparent 34rem),
           var(--bg);
         color: var(--fg); line-height: 1.55; }
  body::before { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .42;
                 background-image: linear-gradient(var(--line) 1px, transparent 1px),
                                   linear-gradient(90deg, var(--line) 1px, transparent 1px);
                 background-size: 56px 56px;
                 mask-image: radial-gradient(circle at center, black 0, transparent 78%); }
  header { display: flex; justify-content: space-between; align-items: center;
           gap: 24px; flex-wrap: wrap; max-width: 1240px; margin: 0 auto 28px;
           padding-bottom: 22px; border-bottom: 1px solid var(--line); }
  .page-title { min-width: min(100%, 270px); }
  .back-link { display: inline-flex; align-items: center; min-height: 32px; margin-bottom: 12px;
               color: var(--muted); text-decoration: none; font-size: 12px;
               transition: color .16s ease, transform .16s ease; }
  .back-link:hover { color: var(--accent-bright); transform: translateX(-2px); }
  h1 { font-size: 28px; color: var(--fg); font-weight: 650; letter-spacing: 0; }
  .subtitle { max-width: 46rem; margin-top: 6px; color: var(--muted); font-size: 13px; }
  .toolbar { display: flex; flex: 1 1 680px; min-width: 0; gap: 12px;
             align-items: center; justify-content: flex-end; flex-wrap: wrap; }
  input[type="text"] { min-width: min(280px, 100%); flex: 1 1 280px; min-height: 38px;
                       padding: 8px 12px; border: 1px solid var(--line-strong); border-radius: 7px;
                       background: rgba(4, 13, 10, .82); color: var(--fg); outline: none; }
  input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(110, 231, 196, .09); }
  button { min-height: 38px; padding: 8px 15px; border: 1px solid var(--accent);
           border-radius: 7px; background: var(--accent); color: #062019;
           cursor: pointer; font-size: 13px; font-weight: 700;
           transition: transform .16s ease, border-color .16s ease, background .16s ease; }
  button:hover { transform: translateY(-1px); background: var(--accent-bright); }
  button:active { transform: translateY(0); }
  button:focus-visible, .back-link:focus-visible, .book-card:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 3px;
  }
  button.secondary { border-color: var(--line-strong); background: rgba(110, 231, 196, .04); color: var(--fg); }
  button.secondary:hover { border-color: rgba(110, 231, 196, .62); background: rgba(110, 231, 196, .10); }
  .bookshelf { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
               gap: 22px; max-width: 1240px; margin: 28px auto 0; }
  /* 书封：硬皮精装观感，侧边书脊 */
  .book-card { position: relative; height: 240px; border: 1px solid rgba(188, 255, 235, .13);
               border-radius: 4px 8px 8px 4px;
               cursor: pointer; transition: transform 0.18s, box-shadow 0.18s;
               box-shadow: 2px 8px 22px rgba(0, 0, 0, .28);
               background: linear-gradient(160deg, #7a5a38, #5c4126);
               color: #f5edde; display: flex; flex-direction: column;
               justify-content: flex-end; padding: 16px 14px; overflow: hidden; }
  .book-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 10px;
                       background: rgba(0,0,0,0.28); border-radius: 4px 0 0 4px; }
  .book-card::after { content: ""; position: absolute; inset: 0;
                      background: radial-gradient(circle at 30% 15%, rgba(255,255,255,0.18), transparent 60%); }
  .book-card:hover { transform: translateY(-5px); box-shadow: 4px 16px 32px rgba(0, 0, 0, .38); }
  .book-card.add { background: rgba(110, 231, 196, .035);
                   color: var(--accent-bright); align-items: center; justify-content: center;
                   border: 1px dashed var(--line-strong); box-shadow: none; min-height: 240px; }
  .book-card.add::before, .book-card.add::after { display: none; }
  .book-title { font-size: 15px; font-weight: 700; line-height: 1.4; margin-bottom: 6px;
                text-shadow: 0 1px 2px rgba(0,0,0,0.4); position: relative; z-index: 1; }
  .book-meta { font-size: 11px; color: rgba(245,237,222,0.85); position: relative; z-index: 1; }
  .book-status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px;
                 margin-top: 8px; background: rgba(0,0,0,0.25); color: #f5edde; position: relative; z-index: 1; }
  .book-status.ready { background: rgba(110, 231, 196, .2); color: var(--accent-bright); }
  .book-status.indexing { background: rgba(233, 187, 100, .24); color: #ffe6b2; }
  .book-status.failed { background: rgba(255, 125, 136, .22); color: #ffd4d8; }
  .search-results { max-width: 1240px; margin: 28px auto 0; }
  .search-results h3 { color: var(--fg) !important; }
  .result-item { background: var(--panel); padding: 16px; border: 1px solid var(--line);
                 border-left: 3px solid var(--accent); border-radius: 6px; margin-bottom: 12px;
                 box-shadow: 0 12px 34px rgba(0, 0, 0, .18); }
  .result-meta { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .result-text { font-size: 14px; line-height: 1.6; }
  .result-method { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px;
                   background: rgba(110, 231, 196, .10); color: var(--accent-bright); margin-left: 8px; }
  #addModal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
              background: rgba(0, 0, 0, .68); backdrop-filter: blur(8px); z-index: 100; }
  #addModal .modal { background: var(--panel-solid); padding: 24px; border: 1px solid var(--line-strong);
                     border-radius: 8px; max-width: 480px; margin: 80px auto; box-shadow: var(--shadow); }
  #addModal .modal h3 { color: var(--fg) !important; }
  #addModal label { display: block; margin: 12px 0 4px; font-size: 13px; color: var(--muted); }
  #addModal input { width: 100%; padding: 8px; border: 1px solid var(--line-strong);
                    border-radius: 6px; background: rgba(4, 13, 10, .82); color: var(--fg); }
  .path-row { display: flex; gap: 8px; }
  .path-row input { flex: 1; }
  .empty { text-align: center; padding: 60px 20px; color: var(--muted); }
  .cand-badge { display: inline-block; margin-left: 10px; padding: 3px 10px; border-radius: 12px;
                font-size: 12px; background: rgba(110, 231, 196, .14); color: var(--accent-bright);
                cursor: pointer; vertical-align: middle; }
  @media (max-width: 760px) {
    body { padding: 18px 14px; }
    header { align-items: stretch; gap: 14px; }
    h1 { font-size: 23px; }
    .toolbar { flex-basis: 100%; justify-content: stretch; gap: 8px; }
    .toolbar input { flex-basis: 100%; }
    .toolbar button { flex: 1 1 132px; padding-left: 10px; padding-right: 10px; }
    .bookshelf { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px; }
    .book-card { height: 218px; }
  }
</style>
</head>
<body>
<header>
  <div class="page-title">
    <a class="back-link" href="/">← 返回主面板</a>
    <h1>知识书库</h1>
    <p class="subtitle">统一管理本地书籍、检索索引、知识关系与长期记忆候选。</p>
  </div>
  <div class="toolbar">
    <input type="text" id="searchInput" placeholder="搜索全部书籍..." onkeydown="if(event.key==='Enter')doSearch()">
    <button onclick="doSearch()">搜索</button>
    <button class="secondary" onclick="openCandidates()" id="candBtn">记忆候选</button>
    <button class="secondary" onclick="openDeletedBooks()">最近删除</button>
    <button class="secondary" onclick="openAddModal()">+ 添加一本书</button>
  </div>
</header>

<div id="bookshelf" class="bookshelf"></div>
<div id="searchResults" class="search-results"></div>

<div id="addModal">
  <div class="modal">
    <h3 style="margin-bottom:16px;color:#5b4636;">添加一本书</h3>
    <label>文件夹路径</label>
    <div class="path-row">
      <input type="text" id="bookPath" placeholder="请选择文件夹" readonly>
      <button type="button" class="secondary" onclick="pickBookFolder()">选择</button>
    </div>
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
  "linear-gradient(160deg,#245f46,#123c2c)",
  "linear-gradient(160deg,#365f73,#203d4a)",
  "linear-gradient(160deg,#62643a,#3d4024)",
  "linear-gradient(160deg,#75494d,#482b2e)",
  "linear-gradient(160deg,#505b72,#303747)",
  "linear-gradient(160deg,#476b5c,#294238)",
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
    const data = await api("knowledge_candidates_list", ["", "actionable"]);
    const n = data.total || 0;
    const btn = document.getElementById("candBtn");
    if (btn) btn.textContent = n > 0 ? "记忆候选 (" + n + ")" : "记忆候选";
  } catch (e) {}
}

async function openCandidates() {
  const [data, targets] = await Promise.all([
    api("knowledge_candidates_list", ["", "actionable"]),
    api("knowledge_candidate_targets", []),
  ]);
  const shelf = document.getElementById("bookshelf");
  const results = document.getElementById("searchResults");
  shelf.innerHTML = "";
  if (!data.candidates || data.candidates.length === 0) {
    results.innerHTML = '<div class="empty">暂无待审核的记忆候选</div>';
    return;
  }
  let html = '<h3 style="margin-bottom:12px;color:#4a3520;">待审核记忆候选（' +
             data.candidates.length + '）</h3>';
  const groups = targets.groups || [];
  if (groups.length > 0) {
    html += '<div class="result-item"><div class="result-meta">同步目标</div>' +
      '<select id="candidateTarget" style="width:100%;padding:8px">' +
      groups.map(g => `<option value="${escapeHtml(g.share_group_id)}">${escapeHtml(g.label)}</option>`).join('') +
      '</select></div>';
  }
  for (const c of data.candidates) {
    const syncError = c.sync_error
      ? `<div class="result-meta" style="color:#a53a3a;margin-top:6px;">上次同步失败：${escapeHtml(c.sync_error)}</div>`
      : "";
    html += `<div class="result-item">
      <div class="result-meta">📌 ${escapeHtml(c.source||"")} · 置信度 ${c.confidence||0}
        <span class="result-method">${escapeHtml(c.status||"pending")}</span></div>
      <div class="result-text">${escapeHtml(c.content||"")}</div>
      ${syncError}
      <div style="margin-top:10px;display:flex;gap:8px;">
        <button style="background:#3f7d4e" onclick="review('${c.candidate_id}','approve')">采纳</button>
        <button class="secondary" onclick="review('${c.candidate_id}','keep')">暂不处理</button>
        <button class="secondary" onclick="review('${c.candidate_id}','reject')">忽略</button>
      </div>
    </div>`;
  }
  results.innerHTML = html;
}

async function openDeletedBooks() {
  const data = await api("knowledge_deleted_list", []);
  const shelf = document.getElementById("bookshelf");
  const results = document.getElementById("searchResults");
  shelf.innerHTML = "";
  if (!data.deleted_books || data.deleted_books.length === 0) {
    results.innerHTML = '<div class="empty">最近没有已删除书籍</div>';
    return;
  }
  let html = '<h3 style="margin-bottom:12px;color:#4a3520;">最近删除</h3>';
  for (const item of data.deleted_books) {
    html += `<div class="result-item">
      <div class="result-meta">${escapeHtml(item.deleted_at || "")}</div>
      <div class="result-text"><strong>${escapeHtml(item.title || "")}</strong><br>${escapeHtml(item.root_path || "")}</div>
      <div style="margin-top:10px;display:flex;gap:8px;">
        <button onclick="restoreDeletedBook('${item.deletion_id}')">恢复</button>
        <button class="secondary" onclick="purgeDeletedBook('${item.deletion_id}')">永久清理</button>
      </div>
    </div>`;
  }
  results.innerHTML = html;
}

async function restoreDeletedBook(deletionId) {
  const result = await api("knowledge_restore", [deletionId]);
  if (result.error) { alert(result.error); return; }
  if (result.deferred) { alert("恢复请求已提交"); return; }
  loadBooks();
}

async function purgeDeletedBook(deletionId) {
  if (!confirm("永久清理后无法恢复，是否继续？")) return;
  const result = await api("knowledge_purge_deleted", [deletionId]);
  if (result.error) { alert(result.error); return; }
  if (result.deferred) { alert("永久清理请求已提交"); return; }
  openDeletedBooks();
}

async function review(id, decision) {
  const target = document.getElementById("candidateTarget");
  const result = await api(
    "knowledge_candidate_review",
    [id, decision, target ? target.value : ""],
  );
  if (result.error) {
    alert(result.error);
  }
  if (decision === "keep" && !result.error) {
    loadBooks();
    return;
  }
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

async function pickBookFolder() {
  const result = await api("pick_path", [false]);
  if (result.path) document.getElementById("bookPath").value = result.path;
}

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
        chapter_items = info.get("chapter_items", [])
        documents = info.get("documents", [])
        entities = info.get("entities", [])
        relations = info.get("relations", [])
        fragments = info.get("fragments", [])
        chapters_html = "".join(
            "<article class='chapter-card'>"
            f"<h3>{_escape(item.get('chapter', ''))}</h3>"
            f"<span>{int(item.get('chunk_count', 0) or 0)} 个片段</span>"
            "</article>"
            for item in chapter_items
        )
        docs_html = "".join(
            "<article class='document-card'>"
            f"<div class='document-path'>{_escape(item.get('relative_path', ''))}</div>"
            f"<div class='document-meta'>{int(item.get('chunk_count', 0) or 0)} 个片段"
            f"<span class='dot'>·</span>{_escape(item.get('status', ''))}</div>"
            "</article>"
            for item in documents
        )
        entities_html = "".join(
            "<article class='entity-card'>"
            f"<div class='entity-type'>{_escape(item.get('entity_type', 'concept'))}</div>"
            f"<h3>{_escape(item.get('name', ''))}</h3>"
            f"<span>{int(item.get('mention_count', 0) or 0)} 次关联</span>"
            "</article>"
            for item in entities
        )
        relations_html = "".join(
            "<article class='relation-row'>"
            f"<strong>{_escape(item.get('subject', ''))}</strong>"
            f"<span class='predicate'>{_escape(item.get('predicate', 'related_to'))}</span>"
            f"<strong>{_escape(item.get('object', ''))}</strong>"
            f"<small>{_escape(item.get('relation_source', 'structural'))}"
            f"<span class='dot'>·</span>{_escape(item.get('relative_path', ''))}</small>"
            "</article>"
            for item in relations
        )
        fragments_html = "".join(
            "<article class='fragment-card'>"
            f"<div class='fragment-meta'>{_escape(item.get('chapter', '') or '未分章')}"
            f"<span class='dot'>·</span>{_escape(item.get('relative_path', ''))}"
            f"<span class='dot'>·</span>{int(item.get('line_start', 0) or 0)}"
            f"-{int(item.get('line_end', 0) or 0)}</div>"
            f"<p>{_escape(item.get('summary') or item.get('text', ''))[:360]}</p>"
            "</article>"
            for item in fragments
        )

        phase_labels = (
            ("lexical", "文本索引"),
            ("organized", "知识整理"),
            ("vector", "向量索引"),
        )
        phase_cards = []
        build_phases = info.get("build_phases", {})
        for key, label in phase_labels:
            phase = build_phases.get(key)
            if isinstance(phase, dict):
                phase_status = str(phase.get("status", "unavailable"))
                details = []
                for field, field_label in (
                    ("indexed", "已索引"),
                    ("processed", "已处理"),
                    ("model_calls", "模型调用"),
                    ("relations", "关系"),
                ):
                    if field in phase:
                        details.append(f"{field_label} {phase.get(field, 0)}")
                phase_detail = " · ".join(details) or "暂无统计"
            elif phase is True:
                phase_status = "ready"
                phase_detail = "已完成"
            else:
                phase_status = "unavailable"
                phase_detail = "尚不可用"
            status_class = (
                phase_status
                if phase_status in {
                    "ready", "partial", "failed", "unavailable", "disabled",
                }
                else "unavailable"
            )
            phase_cards.append(
                "<article class='phase-card'>"
                f"<div><span>{label}</span><strong class='phase-status {status_class}'>"
                f"{_escape(phase_status)}</strong></div>"
                f"<small>{_escape(phase_detail)}</small>"
                "</article>"
            )
        phases_html = "".join(phase_cards)
        book_ids_json = _json.dumps([book_id], ensure_ascii=False)
        book_status = str(info.get("status", ""))
        status_class = (
            book_status
            if book_status in {"ready", "partial", "failed", "indexing"}
            else "partial"
        )
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_escape(info['title'])} - 知识书库</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #040b09;
    --panel: rgba(10, 25, 21, 0.88);
    --panel-solid: #0b1a16;
    --panel-bright: #10251f;
    --fg: #e4f5ef;
    --muted: #78988d;
    --faint: #48685e;
    --line: rgba(110, 231, 196, 0.16);
    --line-strong: rgba(110, 231, 196, 0.34);
    --accent: #6ee7c4;
    --accent-bright: #bcffeb;
    --red: #ff7d88;
    --orange: #e9bb64;
    --shadow: 0 24px 70px rgba(0, 0, 0, 0.32);
  }}
  * {{ box-sizing: border-box; }}
  html {{ min-height: 100%; }}
  body {{ position: relative; min-height: 100dvh; margin: 0;
          font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
          background:
            radial-gradient(circle at 14% 12%, rgba(48, 170, 133, 0.10), transparent 30rem),
            radial-gradient(circle at 84% 82%, rgba(78, 150, 125, 0.07), transparent 34rem),
            var(--bg);
          color: var(--fg); line-height: 1.55; }}
  body::before {{ content: ""; position: fixed; inset: 0; pointer-events: none; opacity: .42;
                  background-image: linear-gradient(var(--line) 1px, transparent 1px),
                                    linear-gradient(90deg, var(--line) 1px, transparent 1px);
                  background-size: 56px 56px;
                  mask-image: radial-gradient(circle at center, black 0, transparent 78%); }}
  a {{ color: inherit; text-decoration: none; }}
  .masthead {{ position: relative; border-bottom: 1px solid var(--line);
               background: rgba(4, 11, 9, .72); color: var(--fg); padding: 26px 32px 34px;
               backdrop-filter: blur(14px); }}
  .masthead-inner {{ max-width: 1180px; margin: 0 auto; }}
  .back {{ display: inline-flex; align-items: center; gap: 8px; color: var(--muted);
           font-size: 13px; margin-bottom: 22px; transition: color .16s ease, transform .16s ease; }}
  .back:hover {{ color: var(--accent-bright); transform: translateX(-2px); }}
  .title-row {{ display: flex; align-items: flex-end; justify-content: space-between;
                gap: 24px; }}
  h1 {{ margin: 0; font-size: 30px; line-height: 1.2; letter-spacing: 0; overflow-wrap: anywhere; }}
  .description {{ max-width: 760px; color: var(--muted); margin: 10px 0 0; }}
  .book-status {{ display: inline-flex; align-items: center; min-height: 30px; padding: 5px 10px;
                  border-radius: 6px; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
  .book-status.ready {{ background: rgba(110, 231, 196, .2); color: var(--accent-bright); }}
  .book-status.partial, .book-status.indexing {{ background: rgba(233, 187, 100, .24); color: #ffe6b2; }}
  .book-status.failed {{ background: rgba(255, 125, 136, .22); color: #ffd4d8; }}
  .stats {{ position: relative; max-width: 1180px; margin: 18px auto 0; padding: 0 24px;
            display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
  .stat {{ min-height: 76px; background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
           padding: 14px 16px; box-shadow: 0 12px 34px rgba(0, 0, 0, .16); }}
  .stat strong {{ display: block; font-size: 22px; color: var(--accent-bright); }}
  .stat span {{ color: var(--muted); font-size: 12px; }}
  .layout {{ position: relative; max-width: 1180px; margin: 26px auto 60px; padding: 0 24px;
             display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 34px; }}
  section {{ padding: 24px 0; border-bottom: 1px solid var(--line); }}
  section:first-child {{ padding-top: 0; }}
  .section-head {{ display: flex; justify-content: space-between; align-items: center;
                   gap: 16px; margin-bottom: 14px; }}
  h2 {{ margin: 0; font-size: 17px; color: var(--fg); letter-spacing: 0; }}
  .section-note {{ color: var(--muted); font-size: 12px; }}
  .search-bar {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }}
  input {{ width: 100%; min-height: 40px; padding: 9px 11px; border: 1px solid var(--line-strong);
           border-radius: 5px; background: rgba(4, 13, 10, .82); color: var(--fg); font: inherit; }}
  input:focus {{ border-color: var(--accent); outline: none; box-shadow: 0 0 0 3px rgba(110, 231, 196, .09); }}
  button, .button {{ display: inline-flex; align-items: center; justify-content: center;
                     min-height: 40px; padding: 9px 15px; border: 1px solid var(--accent);
                     border-radius: 5px; background: var(--accent); color: #062019; cursor: pointer;
                     font: inherit; font-weight: 650; }}
  button:hover, .button:hover {{ background: var(--accent-bright); }}
  button:focus-visible, .button:focus-visible, .back:focus-visible {{
    outline: 2px solid var(--accent); outline-offset: 3px;
  }}
  .button.ghost, button.secondary {{ border-color: var(--line-strong); background: rgba(110, 231, 196, .04); color: var(--fg); }}
  .button.ghost:hover, button.secondary:hover {{ background: rgba(110, 231, 196, .10); }}
  button.danger {{ border-color: rgba(255, 125, 136, .55); background: rgba(255, 125, 136, .10); color: var(--red); }}
  button.danger:hover {{ background: rgba(255, 125, 136, .18); }}
  .chapter-grid, .entity-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
                                gap: 10px; }}
  .chapter-card, .entity-card, .fragment-card, .document-card, .phase-card {{
    min-width: 0; border: 1px solid var(--line); border-radius: 6px; background: var(--panel);
  }}
  .chapter-card, .entity-card {{ min-height: 104px; padding: 14px; }}
  .chapter-card h3, .entity-card h3 {{ margin: 0 0 14px; font-size: 14px;
                                      overflow-wrap: anywhere; }}
  .chapter-card span, .entity-card span {{ color: var(--muted); font-size: 12px; }}
  .entity-card {{ border-top: 3px solid var(--accent); }}
  .entity-type {{ color: var(--accent); font-size: 10px; font-weight: 750;
                  text-transform: uppercase; margin-bottom: 7px; }}
  .relation-list, .fragment-list, .document-list, .phase-list {{
    display: grid; gap: 8px;
  }}
  .relation-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
                   align-items: center; gap: 10px; padding: 11px 12px; border: 1px solid var(--line);
                   border-left: 3px solid var(--orange); background: var(--panel); border-radius: 0 6px 6px 0; }}
  .relation-row strong {{ font-size: 13px; overflow-wrap: anywhere; }}
  .relation-row small {{ grid-column: 1 / -1; color: var(--muted); font-size: 11px; }}
  .predicate {{ color: var(--orange); font-size: 11px; font-weight: 700; }}
  .fragment-card {{ padding: 14px 15px; }}
  .fragment-card p {{ margin: 8px 0 0; color: var(--fg); overflow-wrap: anywhere; }}
  .fragment-meta, .document-meta {{ color: var(--muted); font-size: 11px; }}
  .document-card, .phase-card {{ padding: 12px 13px; }}
  .document-path {{ font-size: 13px; font-weight: 650; overflow-wrap: anywhere; margin-bottom: 5px; }}
  .phase-card > div {{ display: flex; justify-content: space-between; gap: 10px; }}
  .phase-card small {{ display: block; margin-top: 6px; color: var(--muted); }}
  .phase-status {{ font-size: 10px; text-transform: uppercase; }}
  .phase-status.ready {{ color: var(--accent); }}
  .phase-status.partial {{ color: var(--orange); }}
  .phase-status.failed {{ color: var(--red); }}
  .phase-status.unavailable, .phase-status.disabled {{ color: var(--muted); }}
  .settings {{ border: 1px solid var(--line-strong); border-radius: 6px; background: var(--panel-bright);
               padding: 16px; }}
  .settings dl {{ margin: 14px 0 0; display: grid; gap: 10px; }}
  .settings div {{ display: grid; gap: 2px; }}
  .settings dt {{ color: var(--muted); font-size: 11px; }}
  .settings dd {{ margin: 0; font-size: 13px; overflow-wrap: anywhere; }}
  .settings-actions {{ display: grid; gap: 8px; margin-top: 16px; }}
  .rail section {{ padding-top: 0; }}
  .empty {{ color: var(--muted); padding: 14px 0; }}
  .result {{ background: var(--panel); padding: 13px 14px; border: 1px solid var(--line);
             border-left: 3px solid var(--accent); border-radius: 6px; margin-top: 8px; }}
  .result p {{ margin: 7px 0 0; overflow-wrap: anywhere; }}
  .dot {{ padding: 0 5px; }}
  @media (max-width: 900px) {{
    .layout {{ grid-template-columns: 1fr; }}
    .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .chapter-grid, .entity-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .title-row {{ align-items: flex-start; flex-direction: column; }}
  }}
  @media (max-width: 560px) {{
    .masthead {{ padding: 22px 18px 30px; }}
    .stats, .layout {{ padding-left: 14px; padding-right: 14px; }}
    .chapter-grid, .entity-grid {{ grid-template-columns: 1fr; }}
    .relation-row {{ grid-template-columns: 1fr; }}
    .relation-row small {{ grid-column: auto; }}
    .search-bar {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-inner">
    <a class="back" href="/knowledge">← 返回书架</a>
    <div class="title-row">
      <div>
        <h1>{_escape(info['title'])}</h1>
        <p class="description">{_escape(info.get("description") or "已纳入 MemoryGuard 的本地只读知识来源。")}</p>
      </div>
      <span class="book-status {status_class}">{_escape(book_status)}</span>
    </div>
  </div>
</header>

<div class="stats">
  <article class="stat"><strong>{int(info.get('file_count', 0) or 0)}</strong><span>文件</span></article>
  <article class="stat"><strong>{int(info.get('chapter_count', 0) or 0)}</strong><span>章节</span></article>
  <article class="stat"><strong>{int(info.get('chunk_count', 0) or 0)}</strong><span>知识片段</span></article>
  <article class="stat"><strong>{int(info.get('entity_count', 0) or 0)}</strong><span>实体</span></article>
</div>

<main class="layout">
  <div class="content">
    <section>
      <div class="section-head">
        <h2>搜索本书</h2>
        <span class="section-note">FTS、向量与图关系融合检索</span>
      </div>
      <div class="search-bar">
        <input type="text" id="q" placeholder="输入关键词" onkeydown="if(event.key==='Enter')searchBook()">
        <button type="button" onclick="searchBook()">搜索</button>
      </div>
      <div id="results"></div>
    </section>

    <section>
      <div class="section-head"><h2>章节</h2><span class="section-note">按活跃片段统计</span></div>
      <div class="chapter-grid">{chapters_html or '<p class="empty">暂无章节</p>'}</div>
    </section>

    <section>
      <div class="section-head"><h2>知识片段</h2><span class="section-note">仅显示可访问内容</span></div>
      <div class="fragment-list">{fragments_html or '<p class="empty">暂无可展示片段</p>'}</div>
    </section>

    <section>
      <div class="section-head"><h2>实体</h2><span class="section-note">按片段关联次数排序</span></div>
      <div class="entity-grid">{entities_html or '<p class="empty">暂无实体</p>'}</div>
    </section>

    <section>
      <div class="section-head">
        <h2>关系</h2>
        <a class="button ghost" href="/">打开主图谱</a>
      </div>
      <div class="relation-list">{relations_html or '<p class="empty">暂无关系</p>'}</div>
    </section>
  </div>

  <aside class="rail">
    <section>
      <div class="section-head"><h2>构建状态</h2></div>
      <div class="phase-list">{phases_html}</div>
    </section>

    <section>
      <div class="section-head"><h2>文档</h2></div>
      <div class="document-list">{docs_html or '<p class="empty">暂无文档</p>'}</div>
    </section>

    <section>
      <div class="settings">
        <div class="section-head"><h2>书籍设置</h2></div>
        <dl>
          <div><dt>根目录</dt><dd>{_escape(info.get('root_path', ''))}</dd></div>
          <div><dt>向量策略</dt><dd>{_escape(info.get('vector_enabled', 'auto'))}</dd></div>
          <div><dt>远程 Embedding</dt><dd>{"已授权" if info.get('remote_embedding_allowed') else "未授权"}</dd></div>
          <div><dt>远程查询向量</dt><dd>{"已授权" if info.get('remote_query_embedding_allowed') else "未授权"}</dd></div>
          <div><dt>记忆候选</dt><dd>{"自动提取" if info.get('auto_extract_memory') else "已关闭"}</dd></div>
          <div><dt>最近索引</dt><dd>{_escape(info.get('last_indexed_at') or '尚未完成')}</dd></div>
        </dl>
        <div class="settings-actions">
          <button class="secondary" type="button" onclick="toggleRemoteEmbedding()">
            {"关闭远程文档处理" if info.get('remote_embedding_allowed') else "授权远程文档处理"}
          </button>
          <button class="secondary" type="button" onclick="toggleRemoteQuery()">
            {"关闭远程查询向量" if info.get('remote_query_embedding_allowed') else "授权远程查询向量"}
          </button>
          <button class="secondary" type="button" onclick="rebuildSmart()">重建智能索引</button>
          <button class="danger" type="button" onclick="removeBook()">移入回收站</button>
        </div>
      </div>
    </section>
  </aside>
</main>
<script>
const BOOK_IDS = {book_ids_json};
const BOOK_ID = BOOK_IDS[0];
const REMOTE_EMBEDDING_ALLOWED = {_json.dumps(bool(info.get('remote_embedding_allowed')))};
const REMOTE_QUERY_ALLOWED = {_json.dumps(bool(info.get('remote_query_embedding_allowed')))};

function escapeHtml(value) {{
  return String(value || "").replace(/[&<>"']/g, ch => (
    {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[ch]
  ));
}}

async function detailApi(method, args) {{
  const resp = await fetch('/api/' + method, {{
    method: 'POST',
    headers: {{'Content-Type':'application/json', 'X-Session-Token': window.__MG_SESSION__||''}},
    body: JSON.stringify(args || [])
  }});
  return resp.json();
}}

async function applySetting(settings) {{
  const data = await detailApi('knowledge_update_settings', [BOOK_ID, settings]);
  if (data.error) {{ alert(data.error); return; }}
  if (data.deferred) {{ alert('设置请求已提交'); return; }}
  location.reload();
}}

function toggleRemoteEmbedding() {{
  applySetting({{remote_embedding_allowed: !REMOTE_EMBEDDING_ALLOWED}});
}}

function toggleRemoteQuery() {{
  if (!REMOTE_QUERY_ALLOWED && !REMOTE_EMBEDDING_ALLOWED) {{
    alert('请先授权远程文档处理并重建智能索引');
    return;
  }}
  applySetting({{remote_query_embedding_allowed: !REMOTE_QUERY_ALLOWED}});
}}

async function rebuildSmart() {{
  const data = await detailApi('knowledge_rebuild_smart', [BOOK_ID]);
  if (data.error) {{ alert(data.error); return; }}
  if (data.deferred) {{ alert('智能索引重建请求已提交'); return; }}
  location.reload();
}}

async function removeBook() {{
  if (!confirm('书籍将移入回收站，原始文件夹不会被删除。是否继续？')) return;
  const data = await detailApi('knowledge_remove', [BOOK_ID]);
  if (data.error) {{ alert(data.error); return; }}
  if (data.deferred) {{ alert('删除请求已提交'); return; }}
  location.href = '/knowledge';
}}

async function searchBook() {{
  const q = document.getElementById('q').value.trim();
  if (!q) return;
  const box = document.getElementById('results');
  box.innerHTML = '<p class="empty">正在检索...</p>';
  const resp = await fetch('/api/knowledge_search', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json', 'X-Session-Token': window.__MG_SESSION__||''}},
    body: JSON.stringify([q, {{"book_ids": BOOK_IDS}}])
  }});
  const data = await resp.json();
  if (!data.results || !data.results.length) {{
    box.innerHTML = '<p class="empty">未找到匹配片段</p>';
    return;
  }}
  let html = '';
  for (const r of data.results) {{
    html += `<article class="result">
      <div class="fragment-meta">${{escapeHtml(r.chapter || '未分章')}}
        <span class="dot">·</span>${{escapeHtml(r.relative_path || '')}}
        <span class="dot">·</span>${{r.line_start || 0}}-${{r.line_end || 0}}
        <span class="dot">·</span>${{escapeHtml(r.retrieval_method || '')}}</div>
      <p>${{escapeHtml((r.text || '').slice(0, 400))}}</p>
    </article>`;
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
    write = method in {
        "knowledge_add",
        "knowledge_reingest",
        "knowledge_rebuild_smart",
        "knowledge_remove",
        "knowledge_restore",
        "knowledge_purge_deleted",
        "knowledge_update_settings",
        "knowledge_candidate_review",
    }
    store = _get_store(read_only=not write)
    if store is None:
        return {"error": "不能打开知识库（未初始化）"}

    with store:
        if method == "knowledge_list":
            books = list_books(store)
            return {"books": books, "total": len(books)}

        if method == "knowledge_deleted_list":
            deleted = store.list_deleted_books()
            return {"deleted_books": deleted, "total": len(deleted)}

        if method == "knowledge_search":
            if not args:
                return {"error": "query required"}
            query = str(args[0])
            opts = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
            book_ids = opts.get("book_ids")
            top_k = int(opts.get("top_k", 6))
            results = search(
                store,
                query,
                book_ids=book_ids,
                top_k=top_k,
                allow_remote_vector_query=True,
            )
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

        if method == "knowledge_rebuild_smart":
            if not args:
                return {"error": "book_id required"}
            return rebuild_smart_indexes(store, str(args[0]))

        if method == "knowledge_update_settings":
            if len(args) < 2 or not isinstance(args[1], dict):
                return {"error": "book_id and settings required"}
            book_id = str(args[0])
            settings = args[1]
            allowed = {
                "remote_embedding_allowed",
                "remote_query_embedding_allowed",
                "auto_extract_memory",
                "vector_enabled",
            }
            unknown = set(settings) - allowed
            if unknown:
                return {
                    "error": "unknown settings: " + ", ".join(sorted(unknown)),
                }
            if not store.update_book_settings(book_id, **settings):
                return {"error": "book not found"}
            return {
                "ok": True,
                "book_id": book_id,
                "settings": settings,
            }

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
            return store.remove_book(book_id)

        if method == "knowledge_restore":
            if not args:
                return {"error": "deletion_id required"}
            return store.restore_book(str(args[0]))

        if method == "knowledge_purge_deleted":
            if not args:
                return {"error": "deletion_id required"}
            deletion_id = str(args[0])
            if not store.purge_deleted_book(deletion_id):
                return {"error": "deleted book not found"}
            return {"ok": True, "deletion_id": deletion_id}

        if method == "knowledge_candidates_list":
            book_id = str(args[0]) if args and args[0] else None
            status = str(args[1]) if len(args) > 1 and args[1] else "pending"
            candidates = store.list_memory_candidates(book_id=book_id, status=status)
            return {"candidates": candidates, "total": len(candidates)}

        if method == "knowledge_candidate_targets":
            try:
                from .agent_binding import AgentBindingStore, BindingStatus
                active = [
                    b for b in AgentBindingStore(Path(workspace)).list_bindings(
                        include_inactive=False,
                    )
                    if getattr(b, "status", None) == BindingStatus.ACTIVE
                ]
                grouped: dict[str, list[str]] = {}
                for binding in active:
                    grouped.setdefault(binding.share_group_id, []).append(
                        binding.agent_instance_id,
                    )
                groups = [
                    {
                        "share_group_id": group_id,
                        "members": members,
                        "label": (
                            f"{group_id} ({', '.join(members)})"
                            if members else group_id
                        ),
                    }
                    for group_id, members in sorted(grouped.items())
                ]
                return {"groups": groups, "total": len(groups)}
            except Exception as exc:
                return {"groups": [], "total": 0, "error": str(exc)}

        if method == "knowledge_candidate_review":
            if len(args) < 2:
                return {"error": "candidate_id and decision required"}
            candidate_id = str(args[0])
            decision = str(args[1])  # approve / reject
            normalized = {"approve": "approved", "reject": "rejected"}.get(
                decision, decision,
            )
            if decision == "keep":
                normalized = "pending"
            if normalized == "pending":
                if not store.keep_memory_candidate(candidate_id):
                    return {
                        "ok": False,
                        "error": "candidate not found or cannot be retained",
                    }
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "status": "pending",
                    "synced_memory_id": "",
                }
            if normalized == "rejected":
                if not store.review_memory_candidate(candidate_id, decision):
                    return {
                        "ok": False,
                        "error": "candidate not found or invalid decision",
                    }
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "status": "rejected",
                    "synced_memory_id": "",
                }
            if normalized != "approved":
                return {"error": "invalid decision"}

            target_group_id = str(args[2]).strip() if len(args) > 2 and args[2] else ""
            target = _resolve_candidate_target(workspace, target_group_id)
            if not target.ok:
                return {
                    "ok": False,
                    "candidate_id": candidate_id,
                    "status": "sync_failed",
                    "error": target.error,
                    "synced_memory_id": "",
                }
            sync_attempt_id = uuid.uuid4().hex
            claim = store.begin_candidate_sync(
                candidate_id,
                target.group_id,
                sync_attempt_id,
            )
            if not claim.get("ok"):
                return {
                    "ok": False,
                    "candidate_id": candidate_id,
                    "status": claim.get("status", "sync_failed"),
                    "error": claim.get("error", "candidate sync claim failed"),
                    "synced_memory_id": "",
                }
            if claim.get("state") == "already_synced":
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "status": "synced",
                    "synced_memory_id": claim.get("memory_id", ""),
                }

            sync = _sync_candidate_to_memory(
                store,
                candidate_id,
                workspace,
                target_group_id=target.group_id,
                actor=target.actor,
            )
            if not sync.ok:
                store.fail_candidate_sync(
                    candidate_id,
                    sync.error,
                    target.group_id,
                    sync_attempt_id,
                )
                return {
                    "ok": False,
                    "candidate_id": candidate_id,
                    "status": "sync_failed",
                    "error": sync.error,
                    "synced_memory_id": "",
                }
            if not store.complete_candidate_sync(
                candidate_id,
                sync.memory_id,
                target.group_id,
                sync_attempt_id,
            ):
                return {
                    "ok": False,
                    "candidate_id": candidate_id,
                    "status": "sync_failed",
                    "error": "candidate state changed before sync commit",
                    "synced_memory_id": "",
                }
            return {
                "ok": True,
                "candidate_id": candidate_id,
                "status": "synced",
                "synced_memory_id": sync.memory_id,
            }

    return {"error": f"unknown knowledge method: {method}"}


@dataclass(frozen=True)
class CandidateSyncResult:
    ok: bool
    memory_id: str = ""
    error: str = ""


@dataclass(frozen=True)
class CandidateSyncTarget:
    ok: bool
    group_id: str = ""
    actor: str = ""
    error: str = ""


def _resolve_candidate_target(
    workspace: str | Path,
    target_group_id: str = "",
) -> CandidateSyncTarget:
    """Resolve an active target group without inventing an unreachable group."""
    ws = Path(workspace) if workspace else Path.cwd()
    try:
        from .agent_binding import AgentBindingStore, BindingStatus

        active = [
            binding
            for binding in AgentBindingStore(ws).list_bindings(
                include_inactive=False,
            )
            if getattr(binding, "status", None) == BindingStatus.ACTIVE
        ]
    except Exception as exc:
        return CandidateSyncTarget(
            False,
            error=f"cannot resolve target binding: {exc}",
        )

    groups: dict[str, list[str]] = {}
    for binding in active:
        groups.setdefault(binding.share_group_id, []).append(
            binding.agent_instance_id,
        )
    group_id = str(target_group_id or "").strip()
    if group_id:
        members = groups.get(group_id, [])
        if not members:
            return CandidateSyncTarget(
                False,
                error="target share group is not an active binding",
            )
    elif len(groups) == 1:
        group_id, members = next(iter(groups.items()))
    elif not groups:
        return CandidateSyncTarget(
            False,
            error="no active binding; create an agent binding first",
        )
    else:
        return CandidateSyncTarget(
            False,
            error="multiple active share groups; target share group required",
        )
    return CandidateSyncTarget(
        True,
        group_id=group_id,
        actor=sorted(members)[0],
    )


def _sync_candidate_to_memory(
    store,
    candidate_id: str,
    workspace,
    *,
    target_group_id: str = "",
    actor: str = "",
) -> CandidateSyncResult:
    """把候选写入共享长期记忆；失败时不改变候选为已批准。

    P1-2 候选闭环：候选经 GovernanceEngine.auto_write 写入共享记忆层，
    以 candidate_id 作幂等键避免重复写入。未配置共享组时回退个人组。
    """
    try:
        cand = store.get_memory_candidate(candidate_id)
        if not cand or not (cand.get("content") or "").strip():
            return CandidateSyncResult(False, error="candidate not found or empty")
        body = str(cand["content"]).strip()
        kind = str(cand.get("kind") or "").strip()
        if kind not in {"fact", "project", "procedure", "preference"}:
            return CandidateSyncResult(False, error=f"invalid memory kind: {kind}")

        ws = Path(workspace) if workspace else Path.cwd()
        group_id = str(target_group_id or "").strip()
        if not group_id or not actor:
            target = _resolve_candidate_target(ws, group_id)
            if not target.ok:
                return CandidateSyncResult(False, error=target.error)
            group_id = target.group_id
            actor = target.actor

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
            kind_override=kind,
            write_policy="auto_accept",
            injection_policy="relevant",
            idempotency_key=f"knowledge-candidate:{candidate_id}",
        )
        if not result.get("ok"):
            return CandidateSyncResult(
                False,
                error=str(
                    result.get("blocked_reason")
                    or result.get("error")
                    or "governance write blocked"
                ),
            )
        memory_id = result.get("memory_id", "")
        if not memory_id:
            return CandidateSyncResult(False, error="governance write returned no memory_id")
        return CandidateSyncResult(True, memory_id=memory_id)
    except Exception as exc:
        return CandidateSyncResult(False, error=str(exc))


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
    "knowledge_deleted_list",
    "knowledge_search",
    "knowledge_add",
    "knowledge_read",
    "knowledge_book",
    "knowledge_reingest",
    "knowledge_rebuild_smart",
    "knowledge_job_status",
    "knowledge_remove",
    "knowledge_restore",
    "knowledge_purge_deleted",
    "knowledge_update_settings",
    "knowledge_candidates_list",
    "knowledge_candidate_targets",
    "knowledge_candidate_review",
})


def is_knowledge_method(method: str) -> bool:
    """判断是否为知识书库 API 方法。"""
    return method in KNOWLEDGE_API_METHODS


def is_knowledge_mutation(method: str) -> bool:
    """判断是否为变更类知识 API（需确认）。"""
    return method in {
        "knowledge_add",
        "knowledge_reingest",
        "knowledge_rebuild_smart",
        "knowledge_remove",
        "knowledge_restore",
        "knowledge_purge_deleted",
        "knowledge_update_settings",
        "knowledge_candidate_review",
    }
