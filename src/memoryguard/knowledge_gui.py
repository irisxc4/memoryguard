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
from dataclasses import dataclass
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
  .path-row { display: flex; gap: 8px; }
  .path-row input { flex: 1; }
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
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
          background: #f4f5f2; color: #20231f; line-height: 1.55; }}
  a {{ color: inherit; text-decoration: none; }}
  .masthead {{ background: #202b26; color: #f7faf8; padding: 26px 32px 34px; }}
  .masthead-inner {{ max-width: 1180px; margin: 0 auto; }}
  .back {{ display: inline-flex; align-items: center; gap: 8px; color: #b9cac1;
           font-size: 13px; margin-bottom: 22px; }}
  .title-row {{ display: flex; align-items: flex-end; justify-content: space-between;
                gap: 24px; }}
  h1 {{ margin: 0; font-size: 30px; line-height: 1.2; letter-spacing: 0; overflow-wrap: anywhere; }}
  .description {{ max-width: 760px; color: #cfd9d4; margin: 10px 0 0; }}
  .book-status {{ display: inline-flex; align-items: center; min-height: 30px; padding: 5px 10px;
                  border-radius: 6px; font-size: 12px; font-weight: 700; text-transform: uppercase; }}
  .book-status.ready {{ background: #2f7d55; color: #fff; }}
  .book-status.partial, .book-status.indexing {{ background: #d9a441; color: #20231f; }}
  .book-status.failed {{ background: #b84843; color: #fff; }}
  .stats {{ max-width: 1180px; margin: -18px auto 0; padding: 0 24px;
            display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
  .stat {{ min-height: 76px; background: #fff; border: 1px solid #d8ddd9; border-radius: 6px;
           padding: 14px 16px; }}
  .stat strong {{ display: block; font-size: 22px; color: #202b26; }}
  .stat span {{ color: #68716c; font-size: 12px; }}
  .layout {{ max-width: 1180px; margin: 26px auto 60px; padding: 0 24px;
             display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 34px; }}
  section {{ padding: 24px 0; border-bottom: 1px solid #d8ddd9; }}
  section:first-child {{ padding-top: 0; }}
  .section-head {{ display: flex; justify-content: space-between; align-items: center;
                   gap: 16px; margin-bottom: 14px; }}
  h2 {{ margin: 0; font-size: 17px; color: #202b26; letter-spacing: 0; }}
  .section-note {{ color: #7a827e; font-size: 12px; }}
  .search-bar {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }}
  input {{ width: 100%; min-height: 40px; padding: 9px 11px; border: 1px solid #b8c1bc;
           border-radius: 5px; background: #fff; color: #20231f; font: inherit; }}
  button, .button {{ display: inline-flex; align-items: center; justify-content: center;
                     min-height: 40px; padding: 9px 15px; border: 1px solid #245f46;
                     border-radius: 5px; background: #245f46; color: #fff; cursor: pointer;
                     font: inherit; font-weight: 650; }}
  .button.ghost {{ background: #fff; color: #245f46; }}
  .chapter-grid, .entity-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
                                gap: 10px; }}
  .chapter-card, .entity-card, .fragment-card, .document-card, .phase-card {{
    min-width: 0; border: 1px solid #d8ddd9; border-radius: 6px; background: #fff;
  }}
  .chapter-card, .entity-card {{ min-height: 104px; padding: 14px; }}
  .chapter-card h3, .entity-card h3 {{ margin: 0 0 14px; font-size: 14px;
                                      overflow-wrap: anywhere; }}
  .chapter-card span, .entity-card span {{ color: #7a827e; font-size: 12px; }}
  .entity-card {{ border-top: 3px solid #3f6f92; }}
  .entity-type {{ color: #3f6f92; font-size: 10px; font-weight: 750;
                  text-transform: uppercase; margin-bottom: 7px; }}
  .relation-list, .fragment-list, .document-list, .phase-list {{
    display: grid; gap: 8px;
  }}
  .relation-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
                   align-items: center; gap: 10px; padding: 11px 12px; border-left: 3px solid #bd5a48;
                   background: #fff; border-radius: 0 6px 6px 0; }}
  .relation-row strong {{ font-size: 13px; overflow-wrap: anywhere; }}
  .relation-row small {{ grid-column: 1 / -1; color: #7a827e; font-size: 11px; }}
  .predicate {{ color: #9a4436; font-size: 11px; font-weight: 700; }}
  .fragment-card {{ padding: 14px 15px; }}
  .fragment-card p {{ margin: 8px 0 0; color: #3f4541; overflow-wrap: anywhere; }}
  .fragment-meta, .document-meta {{ color: #7a827e; font-size: 11px; }}
  .document-card, .phase-card {{ padding: 12px 13px; }}
  .document-path {{ font-size: 13px; font-weight: 650; overflow-wrap: anywhere; margin-bottom: 5px; }}
  .phase-card > div {{ display: flex; justify-content: space-between; gap: 10px; }}
  .phase-card small {{ display: block; margin-top: 6px; color: #7a827e; }}
  .phase-status {{ font-size: 10px; text-transform: uppercase; }}
  .phase-status.ready {{ color: #2f7d55; }}
  .phase-status.partial {{ color: #9b6a0d; }}
  .phase-status.failed {{ color: #b84843; }}
  .phase-status.unavailable, .phase-status.disabled {{ color: #7a827e; }}
  .settings {{ border: 1px solid #cbd2ce; border-radius: 6px; background: #e9eeeb;
               padding: 16px; }}
  .settings dl {{ margin: 14px 0 0; display: grid; gap: 10px; }}
  .settings div {{ display: grid; gap: 2px; }}
  .settings dt {{ color: #6d7671; font-size: 11px; }}
  .settings dd {{ margin: 0; font-size: 13px; overflow-wrap: anywhere; }}
  .rail section {{ padding-top: 0; }}
  .empty {{ color: #7a827e; padding: 14px 0; }}
  .result {{ background: #fff; padding: 13px 14px; border-radius: 6px; margin-top: 8px;
             border-left: 3px solid #3f6f92; }}
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
          <div><dt>记忆候选</dt><dd>{"自动提取" if info.get('auto_extract_memory') else "已关闭"}</dd></div>
          <div><dt>最近索引</dt><dd>{_escape(info.get('last_indexed_at') or '尚未完成')}</dd></div>
        </dl>
      </div>
    </section>
  </aside>
</main>
<script>
const BOOK_IDS = {book_ids_json};

function escapeHtml(value) {{
  return String(value || "").replace(/[&<>"']/g, ch => (
    {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[ch]
  ));
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
                    return {"error": "candidate not found or cannot be retained"}
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "status": "pending",
                    "synced_memory_id": "",
                }
            if normalized == "rejected":
                if not store.review_memory_candidate(candidate_id, decision):
                    return {"error": "candidate not found or invalid decision"}
                return {
                    "ok": True,
                    "candidate_id": candidate_id,
                    "status": "rejected",
                    "synced_memory_id": "",
                }
            if normalized != "approved":
                return {"error": "invalid decision"}

            # Approval is committed only after the governed memory write
            # succeeds. A failure leaves a retryable sync_failed candidate.
            target_group_id = str(args[2]).strip() if len(args) > 2 and args[2] else ""
            sync = _sync_candidate_to_memory(
                store, candidate_id, workspace, target_group_id=target_group_id,
            )
            if not sync.ok:
                store.mark_candidate_sync_failed(candidate_id, sync.error)
                return {
                    "ok": False,
                    "candidate_id": candidate_id,
                    "status": "sync_failed",
                    "error": sync.error,
                    "synced_memory_id": "",
                }
            if not store.mark_candidate_synced(candidate_id, sync.memory_id):
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


def _sync_candidate_to_memory(
    store, candidate_id: str, workspace, *, target_group_id: str = "",
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

        # A multi-binding workspace must name the intended target explicitly.
        group_id = target_group_id
        actor = ""
        try:
            from .agent_binding import AgentBindingStore, BindingStatus
            binds = AgentBindingStore(ws).list_bindings(include_inactive=False)
            active = [b for b in binds if getattr(b, "status", None) == BindingStatus.ACTIVE]
            if group_id:
                matching = [b for b in active if b.share_group_id == group_id]
                if not matching:
                    return CandidateSyncResult(
                        False, error="target share group is not an active binding",
                    )
                # The group is the user-selected target. Use a deterministic
                # active member only as provenance actor; shared groups may
                # legitimately contain multiple bindings.
                actor = sorted(b.agent_instance_id for b in matching)[0]
            elif len(active) == 1:
                group_id = active[0].share_group_id
                actor = active[0].agent_instance_id
            elif len(active) > 1:
                return CandidateSyncResult(
                    False, error="multiple active bindings; target share group required",
                )
        except Exception:
            if group_id:
                return CandidateSyncResult(False, error="cannot resolve target binding")
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
    "knowledge_search",
    "knowledge_add",
    "knowledge_read",
    "knowledge_book",
    "knowledge_reingest",
    "knowledge_job_status",
    "knowledge_remove",
    "knowledge_candidates_list",
    "knowledge_candidate_targets",
    "knowledge_candidate_review",
})


def is_knowledge_method(method: str) -> bool:
    """判断是否为知识书库 API 方法。"""
    return method in KNOWLEDGE_API_METHODS


def is_knowledge_mutation(method: str) -> bool:
    """判断是否为变更类知识 API（需确认）。"""
    return method in {"knowledge_add", "knowledge_reingest", "knowledge_remove",
                      "knowledge_candidate_review"}
