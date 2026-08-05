"""knowledge_mcp：MCP 工具定义和处理（KB1）。

提供 4 个只读 MCP 工具：
- memoryguard_knowledge_list
- memoryguard_knowledge_search
- memoryguard_knowledge_read
- memoryguard_knowledge_book

管理操作（添加/删除文件夹）不暴露给 MCP Agent，只在 GUI 执行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .data_home import resolve_data_home
from .knowledge_ingestion import create_book, ingest_book
from .knowledge_retriever import get_book_info, list_books, read_chunk, search
from .knowledge_store import KnowledgeStore

# MCP 工具定义
KNOWLEDGE_TOOL_DEFINITIONS = [
    {
        "name": "memoryguard_knowledge_list",
        "description": (
            "List all books on the shared knowledge bookshelf. "
            "All MCP agents sharing the same workspace see the same books. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
        },
    },
    {
        "name": "memoryguard_knowledge_search",
        "description": (
            "Search the shared knowledge bookshelf by full-text query. "
            "Returns chunks with book title, chapter, file path, and line numbers. "
            "book_ids empty = search all books."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query (supports Chinese and English)"},
                "book_ids": {"type": "array", "items": {"type": "string"}, "description": "limit to specific books (empty = all)"},
                "top_k": {"type": "integer", "description": "max results (default 6)", "default": 6},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memoryguard_knowledge_read",
        "description": (
            "Read a single knowledge chunk by chunk_id, including adjacent context. "
            "Returns the full text, source file, line numbers, and neighbor chunks."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "chunk_id": {"type": "string", "description": "chunk id from search results"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["chunk_id"],
        },
    },
    {
        "name": "memoryguard_knowledge_book",
        "description": (
            "Get detailed info about a single book: table of contents, chapters, documents. "
            "Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "book_id": {"type": "string", "description": "book id"},
                "workspace": {"type": "string", "description": "workspace path (default: .)"},
            },
            "required": ["book_id"],
        },
    },
]


def handle_knowledge_tool(name: str, args: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """处理知识书库 MCP 工具调用。返回 None 表示工具名不匹配。"""
    store = _get_store(workspace)
    if store is None:
        return _error("knowledge store unavailable")

    try:
        if name == "memoryguard_knowledge_list":
            return _handle_list(store)
        if name == "memoryguard_knowledge_search":
            return _handle_search(store, args)
        if name == "memoryguard_knowledge_read":
            return _handle_read(store, args)
        if name == "memoryguard_knowledge_book":
            return _handle_book(store, args)
    except Exception as e:
        return _error(str(e))
    finally:
        store.close()

    return None


def _handle_list(store: KnowledgeStore) -> dict[str, Any]:
    books = list_books(store)
    if not books:
        return _text("书架上还没有书籍。在 GUI 中添加文件夹即可创建第一本书。")
    lines = [f"知识书架：共 {len(books)} 本书\n"]
    for b in books:
        lines.append(
            f"  《{b['title']}》  状态={b['status']}  "
            f"文件={b['file_count']}  章节={b['chapter_count']}  片段={b['chunk_count']}"
        )
    return _text("\n".join(lines))


def _handle_search(store: KnowledgeStore, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return _error("query is required")
    book_ids = args.get("book_ids")
    if book_ids and not isinstance(book_ids, list):
        return _error("book_ids must be an array")
    top_k = int(args.get("top_k", 6))
    if not 1 <= top_k <= 30:
        top_k = 6

    results = search(store, query, book_ids=book_ids or None, top_k=top_k)
    if not results:
        return _text(f"未找到与「{query}」相关的知识片段。")

    lines = [f"搜索「{query}」找到 {len(results)} 个片段：\n"]
    for i, r in enumerate(results, 1):
        lines.append(
            f"{i}. 《{r.get('book_title', '')}》"
            f"{r.get('chapter', '')} > {r.get('section', '')}\n"
            f"   {r.get('relative_path', '')}  第 {r.get('line_start', 0)}-{r.get('line_end', 0)} 行\n"
            f"   {r.get('text', '')[:200]}...\n"
            f"   chunk_id: {r.get('chunk_id', '')}"
        )
    return _text("\n".join(lines))


def _handle_read(store: KnowledgeStore, args: dict[str, Any]) -> dict[str, Any]:
    chunk_id = str(args.get("chunk_id", "")).strip()
    if not chunk_id:
        return _error("chunk_id is required")
    result = read_chunk(store, chunk_id)
    if not result:
        return _error(f"chunk not found: {chunk_id}")

    lines = [
        f"《{result['book_title']}》{result['chapter']} > {result['section']}",
        f"来源：{result['relative_path']}  第 {result['line_start']}-{result['line_end']} 行",
        f"chunk_id: {result['chunk_id']}",
        "",
        result["text"],
    ]
    if result.get("prev_text"):
        lines.insert(0, f"--- 上一片段 ---\n{result['prev_text'][:150]}...\n")
    if result.get("next_text"):
        lines.append(f"\n--- 下一片段 ---\n{result['next_text'][:150]}...")
    return _text("\n".join(lines))


def _handle_book(store: KnowledgeStore, args: dict[str, Any]) -> dict[str, Any]:
    book_id = str(args.get("book_id", "")).strip()
    if not book_id:
        return _error("book_id is required")
    info = get_book_info(store, book_id)
    if not info:
        return _error(f"book not found: {book_id}")

    lines = [
        f"《{info['title']}》",
        f"状态：{info['status']}",
        f"文件：{info['file_count']}  章节：{info['chapter_count']}  片段：{info['chunk_count']}",
        f"最近整理：{info['last_indexed_at']}",
        "",
        "目录：",
    ]
    for ch in info["chapters"]:
        lines.append(f"  - {ch}")
    lines.append("")
    lines.append("文件列表：")
    for d in info["documents"]:
        lines.append(f"  {d['relative_path']}  ({d['status']})")
    return _text("\n".join(lines))


# ---- 管理接口（仅 GUI 调用，不暴露给 MCP Agent） ----

def add_book_gui(root_path: str, title: str = "", include_globs: str = "",
                 exclude_globs: str = "", data_home: Path | None = None) -> dict[str, Any]:
    """GUI 调用：添加一本书并立即索引。"""
    store = KnowledgeStore(data_home)
    try:
        book = create_book(store, root_path, title, include_globs, exclude_globs)
        result = ingest_book(store, book.book_id)
        return {
            "ok": True,
            "book_id": book.book_id,
            "title": book.title,
            "files_total": result.files_total,
            "files_processed": result.files_processed,
            "files_skipped": result.files_skipped,
            "files_deleted": result.files_deleted,
            "chunks_created": result.chunks_created,
            "chapters": sorted(result.chapters),
            "error": result.error,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        store.close()


def remove_book_gui(book_id: str, data_home: Path | None = None) -> dict[str, Any]:
    """GUI 调用：移除一本书（只删索引，不删原文件）。"""
    store = KnowledgeStore(data_home)
    try:
        book = store.get_book(book_id)
        if not book:
            return {"ok": False, "error": "book not found"}
        store.remove_book(book_id)
        return {"ok": True, "book_id": book_id, "title": book.title}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        store.close()


def reindex_book_gui(book_id: str, data_home: Path | None = None) -> dict[str, Any]:
    """GUI 调用：重新索引一本书。"""
    store = KnowledgeStore(data_home)
    try:
        book = store.get_book(book_id)
        if not book:
            return {"ok": False, "error": "book not found"}
        result = ingest_book(store, book_id)
        return {
            "ok": True,
            "book_id": book_id,
            "title": book.title,
            "files_total": result.files_total,
            "files_processed": result.files_processed,
            "files_skipped": result.files_skipped,
            "files_deleted": result.files_deleted,
            "chunks_created": result.chunks_created,
            "chapters": sorted(result.chapters),
            "error": result.error,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        store.close()


# ---- 辅助 ----

_store_cache: dict[str, KnowledgeStore] = {}


def _get_store(workspace: Path) -> KnowledgeStore | None:
    """获取或创建 KnowledgeStore（缓存在进程内）。"""
    key = str(workspace)
    if key in _store_cache:
        return _store_cache[key]
    try:
        store = KnowledgeStore(workspace)
        _store_cache[key] = store
        return store
    except Exception:
        return None


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}
