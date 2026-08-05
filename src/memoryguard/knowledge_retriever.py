"""knowledge_retriever：FTS5 全文检索（KB1）。

KB1 只实现 FTS5 检索。KB2 将增加向量检索。KB3 将增加实体关系扩展。
"""

from __future__ import annotations

from typing import Any

from .knowledge_store import KnowledgeStore


def search(store: KnowledgeStore, query: str,
           book_ids: list[str] | None = None,
           top_k: int = 6) -> list[dict[str, Any]]:
    """知识库搜索。返回 top_k 个结果。

    每条结果包含：chunk_id, book_id, book_title, chapter, section,
    text, line_start, line_end, relative_path, retrieval_method。
    """
    if not query.strip():
        return []

    # KB1: FTS5 优先，短查询/无结果时 LIKE fallback
    fts_results = store.search_fts(query, book_ids=book_ids, limit=max(top_k * 5, 30))

    if not fts_results:
        # trigram 对 <3 字符查询无召回；LIKE 保证短中文查询可用
        fts_results = store.search_like(query, book_ids=book_ids, limit=max(top_k * 5, 30))

    if not fts_results:
        return []

    # 重排 + 去重 + 限制
    ranked = _rank_results(fts_results, query)

    # 每本书最多 4 个，每个文件最多 2 个
    filtered = _apply_limits(ranked, max_per_book=4, max_per_file=2)

    return filtered[:top_k]


def read_chunk(store: KnowledgeStore, chunk_id: str) -> dict[str, Any] | None:
    """读取一个 chunk 及其相邻上下文。"""
    chunk = store.get_chunk(chunk_id)
    if not chunk:
        return None

    # 获取文档信息
    doc_row = store._conn.execute(
        "SELECT relative_path FROM documents WHERE document_id=?",
        (chunk.document_id,),
    ).fetchone()
    relative_path = doc_row["relative_path"] if doc_row else ""

    # 获取书籍信息
    book = store.get_book(chunk.book_id)
    book_title = book.title if book else ""

    # 相邻 chunk
    prev_chunk, next_chunk = store.get_adjacent_chunks(chunk_id)

    return {
        "chunk_id": chunk.chunk_id,
        "book_id": chunk.book_id,
        "book_title": book_title,
        "chapter": chunk.chapter,
        "section": chunk.section,
        "text": chunk.text,
        "summary": chunk.summary,
        "keywords": chunk.keywords,
        "line_start": chunk.line_start,
        "line_end": chunk.line_end,
        "relative_path": relative_path,
        "prev_chunk_id": prev_chunk.chunk_id if prev_chunk else None,
        "prev_text": prev_chunk.text if prev_chunk else None,
        "next_chunk_id": next_chunk.chunk_id if next_chunk else None,
        "next_text": next_chunk.text if next_chunk else None,
    }


def list_books(store: KnowledgeStore) -> list[dict[str, Any]]:
    """列出书架上的所有书。"""
    books = store.list_books()
    return [
        {
            "book_id": b.book_id,
            "title": b.title,
            "status": b.status,
            "file_count": b.file_count,
            "chapter_count": b.chapter_count,
            "chunk_count": b.chunk_count,
            "entity_count": b.entity_count,
            "last_indexed_at": b.last_indexed_at or "",
            "description": b.description,
        }
        for b in books
    ]


def get_book_info(store: KnowledgeStore, book_id: str) -> dict[str, Any] | None:
    """获取一本书的详细信息：目录、摘要、章节。"""
    book = store.get_book(book_id)
    if not book:
        return None

    documents = store.list_documents(book_id)
    # 获取章节列表
    chapter_rows = store._conn.execute(
        """SELECT DISTINCT chapter FROM chunks
           WHERE book_id=? AND active=1 AND chapter != ''
           ORDER BY chapter""",
        (book_id,),
    ).fetchall()
    chapters = [r["chapter"] for r in chapter_rows]

    return {
        "book_id": book.book_id,
        "title": book.title,
        "description": book.description,
        "status": book.status,
        "root_path": book.root_path,
        "file_count": book.file_count,
        "chapter_count": book.chapter_count,
        "chunk_count": book.chunk_count,
        "entity_count": book.entity_count,
        "last_indexed_at": book.last_indexed_at or "",
        "chapters": chapters,
        "documents": [
            {"relative_path": d["relative_path"], "status": d["status"]}
            for d in documents
        ],
    }


def _rank_results(results: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """简单重排：章节标题命中优先，然后 BM25 rank。"""
    query_lower = query.lower().strip()
    for r in results:
        # 章节标题精确命中加分
        chapter = (r.get("chapter") or "").lower()
        section = (r.get("section") or "").lower()
        score = 0.0
        if query_lower in chapter:
            score += 100
        if query_lower in section:
            score += 50
        # BM25 rank（FTS5 rank 越小越好，取负）
        score -= r.get("rank", 0.0)
        r["_score"] = score
    return sorted(results, key=lambda x: x["_score"], reverse=True)


def _apply_limits(results: list[dict[str, Any]],
                  max_per_book: int = 4, max_per_file: int = 2) -> list[dict[str, Any]]:
    """应用每书/每文件限制。"""
    book_counts: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    filtered: list[dict[str, Any]] = []
    for r in results:
        book_id = r.get("book_id", "")
        doc_id = r.get("document_id", "")
        if book_counts.get(book_id, 0) >= max_per_book:
            continue
        if file_counts.get(doc_id, 0) >= max_per_file:
            continue
        book_counts[book_id] = book_counts.get(book_id, 0) + 1
        file_counts[doc_id] = file_counts.get(doc_id, 0) + 1
        filtered.append(r)
    return filtered
