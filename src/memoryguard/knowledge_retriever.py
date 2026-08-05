"""knowledge_retriever：FTS5 全文检索 + 实体关系扩展（KB1+KB3）。

KB1: FTS5 检索 + LIKE fallback
KB3: 实体命中 + graph 关系扩展（最多两跳）
KB2: 向量检索（暂留接口，无 provider 时降级 FTS）
"""

from __future__ import annotations

from typing import Any

from .knowledge_store import KnowledgeStore


def search(store: KnowledgeStore, query: str,
           book_ids: list[str] | None = None,
           top_k: int = 6,
           enable_graph: bool = True) -> list[dict[str, Any]]:
    """知识库搜索。返回 top_k 个结果。

    每条结果包含：chunk_id, book_id, book_title, chapter, section,
    text, line_start, line_end, relative_path, retrieval_method。

    检索流程（PRD §7.2）：
    1. FTS5 全文召回（trigram 中文）
    2. LIKE fallback（<3 字符短查询）
    3. 实体命中（entities.name LIKE query）
    4. graph 关系扩展（最多两跳，最多 20 节点）
    5. 重排 + 去重 + 每书/每文件限制
    """
    if not query.strip():
        return []

    # 1. FTS5 优先
    results = store.search_fts(query, book_ids=book_ids, limit=max(top_k * 5, 30))

    # 2. LIKE fallback
    if not results:
        results = store.search_like(query, book_ids=book_ids, limit=max(top_k * 5, 30))

    # 标记检索方式
    for r in results:
        r.setdefault("retrieval_method", "fts" if r.get("rank", 1.0) != 0.0 else "like")

    # 3. 实体命中 + 4. graph 关系扩展
    if enable_graph and results:
        _augment_with_graph(store, query, results, book_ids, top_k)

    if not results:
        return []

    # 5. 重排 + 去重 + 限制
    ranked = _rank_results(results, query)
    filtered = _apply_limits(ranked, max_per_book=4, max_per_file=2)

    return filtered[:top_k]


def _augment_with_graph(store: KnowledgeStore, query: str,
                        results: list[dict[str, Any]],
                        book_ids: list[str] | None,
                        top_k: int) -> None:
    """实体命中 + graph 关系扩展。直接追加到 results。"""
    try:
        from .knowledge_graph import expand_relations
    except ImportError:
        return

    # 从 query 找种子实体
    pattern = f"%{query.strip()}%"
    seed_rows = store._conn.execute(
        "SELECT entity_id, name FROM entities WHERE active=1 AND name LIKE ?",
        (pattern,),
    ).fetchall()
    if not seed_rows:
        return

    seed_ids = [r["entity_id"] for r in seed_rows[:5]]  # 最多 5 个种子

    # 关系扩展
    expansion = expand_relations(store, seed_ids, max_hops=2, max_nodes=20)

    # 收集扩展到的实体关联的 chunk
    expanded_entity_ids = set(seed_ids)
    for rel in expansion:
        expanded_entity_ids.add(rel["to_entity_id"])

    if not expanded_entity_ids:
        return

    # 找这些实体关联的 chunk
    placeholders = ",".join("?" * len(expanded_entity_ids))
    sql = f"""
        SELECT c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
               c.text, c.summary, c.keywords, c.line_start, c.line_end,
               b.title AS book_title, b.root_path,
               d.relative_path AS relative_path,
               0.0 AS rank
        FROM chunk_entities ce
        JOIN chunks c ON c.chunk_id=ce.chunk_id
        JOIN books b ON b.book_id=c.book_id
        JOIN documents d ON d.document_id=c.document_id
        WHERE ce.entity_id IN ({placeholders}) AND c.active=1
    """
    params: list[Any] = list(expanded_entity_ids)
    if book_ids:
        ph = ",".join("?" * len(book_ids))
        sql += f" AND c.book_id IN ({ph})"
        params.extend(book_ids)
    sql += " LIMIT ?"
    params.append(top_k * 3)

    existing_ids = {r["chunk_id"] for r in results}
    for row in store._conn.execute(sql, params).fetchall():
        d = dict(row)
        if d["chunk_id"] in existing_ids:
            continue
        d["retrieval_method"] = "graph"
        results.append(d)
        existing_ids.add(d["chunk_id"])


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
