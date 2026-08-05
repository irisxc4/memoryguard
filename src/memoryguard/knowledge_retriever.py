"""knowledge_retriever：FTS5 + 向量 + 实体关系混合召回（KB1+KB2+KB3）。

KB1: FTS5 检索 + LIKE fallback
KB2: 向量检索（provider 可用时），失败静默降级 FTS
KB3: 实体命中 + graph 关系扩展（最多两跳）
"""

from __future__ import annotations

from typing import Any

from .knowledge_store import KnowledgeStore


def search(store: KnowledgeStore, query: str,
           book_ids: list[str] | None = None,
           top_k: int = 6,
           enable_graph: bool = True,
           enable_vector: bool = True) -> list[dict[str, Any]]:
    """知识库搜索。返回 top_k 个结果。

    每条结果包含：chunk_id, book_id, book_title, chapter, section,
    text, line_start, line_end, relative_path, retrieval_method。

    检索流程（PRD §7.2 + P1-2）：
    1. FTS5 全文召回（trigram 中文），<3 字符短查询用 LIKE fallback
    2. 向量召回（KB2，provider 可用时；失败静默降级）
    3. 实体命中 + graph 关系扩展（最多两跳）
    4. RRF（Reciprocal Rank Fusion）融合三路结果重排
    5. 去重 + 每书/每文件限制
    """
    if not query.strip():
        return []

    # 三路独立检索，各自保持排序，供 RRF 融合用
    fts = store.search_fts(query, book_ids=book_ids, limit=max(top_k * 10, 50))
    if not fts:
        fts = store.search_like(query, book_ids=book_ids, limit=max(top_k * 10, 50))
        for r in fts:
            r["retrieval_method"] = "like"
    else:
        for r in fts:
            r["retrieval_method"] = "fts"

    vec: list[dict[str, Any]] = []
    if enable_vector:
        vec = _vector_results(store, query, book_ids, top_k)

    graph: list[dict[str, Any]] = []
    if enable_graph:
        graph = _graph_results(store, query, book_ids, top_k)

    # RRF 融合三路
    fused = _rrf_fuse([fts, vec, graph])

    if not fused:
        return []

    # 去重 + 每书/每文件限制
    filtered = _apply_limits(fused, max_per_book=4, max_per_file=2)
    return filtered[:top_k]


def _rrf_fuse(lists_ranked: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion：按各检索路的排名倒数求和重排。

    score(doc) = Σ_r 1/(k + rank_r)，k=60 为常见常数。
    每路独立排名，避免不同检索方法分数量纲不一致。
    """
    scores: dict[str, float] = {}
    methods: dict[str, str] = {}
    order: dict[str, int] = {}
    seq = 0
    for lst in lists_ranked:
        for rank, r in enumerate(lst):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
            methods.setdefault(cid, r.get("retrieval_method", "fts"))
            if cid not in order:
                order[cid] = seq
                seq += 1
    by_id: dict[str, dict[str, Any]] = {}
    for lst in lists_ranked:
        for r in lst:
            by_id.setdefault(r["chunk_id"], r)
    fused = []
    for cid, score in sorted(scores.items(), key=lambda x: (-x[1], order[x[0]])):
        r = dict(by_id[cid])
        r["retrieval_method"] = methods[cid]
        r["_rrf_score"] = score
        fused.append(r)
    return fused


def _vector_results(store: KnowledgeStore, query: str,
                    book_ids: list[str] | None,
                    top_k: int) -> list[dict[str, Any]]:
    """向量召回（KB2）。provider 不可用或失败时返回空，不阻断 FTS。

    查询必须使用与入库相同的 embedding_space_id（P0-3），否则向量检索接不上
    存储空间的插头，永远返回空、系统只能靠 FTS。
    """
    try:
        from .provider_api import get_provider, current_embedding_space_id
        backend = get_provider()
        if backend is None:
            return []
        space_id = current_embedding_space_id()
        if not space_id:
            return []
        query_vec = backend.embed(query)
        if not query_vec:
            return []
        results = store.search_vectors(
            query_vec, book_ids=book_ids, limit=max(top_k * 10, 50),
            embedding_space_id=space_id,
        )
        for r in results:
            r["embedding_space_id"] = space_id
        return results
    except Exception:
        # 向量失败不影响 FTS 主流程（PRD §15：向量不可用时回退 FTS）
        return []


def _graph_results(store: KnowledgeStore, query: str,
                   book_ids: list[str] | None,
                   top_k: int) -> list[dict[str, Any]]:
    """实体命中 + graph 关系扩展（KB3）。返回独立结果列表。"""
    try:
        from .knowledge_graph import expand_relations
    except ImportError:
        return []

    # 从 query 找种子实体
    pattern = f"%{query.strip()}%"
    seed_rows = store._conn.execute(
        "SELECT entity_id, name FROM entities WHERE active=1 AND name LIKE ?",
        (pattern,),
    ).fetchall()
    if not seed_rows:
        return []

    seed_ids = [r["entity_id"] for r in seed_rows[:5]]  # 最多 5 个种子

    # 关系扩展
    expansion = expand_relations(store, seed_ids, max_hops=2, max_nodes=20)

    expanded_entity_ids = set(seed_ids)
    for rel in expansion:
        expanded_entity_ids.add(rel["to_entity_id"])

    if not expanded_entity_ids:
        return []

    placeholders = ",".join("?" * len(expanded_entity_ids))
    sql = f"""
        SELECT c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
               c.text, c.summary, c.keywords, c.line_start, c.line_end,
               b.title AS book_title, b.root_path,
               d.relative_path AS relative_path, d.content_role AS content_role,
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

    results: list[dict[str, Any]] = []
    for row in store._conn.execute(sql, params).fetchall():
        d = dict(row)
        d["retrieval_method"] = "graph"
        results.append(d)
    return results


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
