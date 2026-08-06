"""knowledge_graph：实体关系存储与扩展（KB3）。

无模型时建立结构化关系（PRD §6.3）：
    文件 -> belongs_to -> 章节
    章节 -> contains -> Chunk
    标题词 -> mentioned_in -> Chunk
    配置 Key -> defined_in -> 文件
    代码符号 -> defined_in -> 文件

有模型时（provider_api 可用）做语义关系抽取（KB3 §6.2，暂留接口）。

检索时支持最多两跳关系扩展（PRD §7.2）。
"""

from __future__ import annotations

from typing import Any

from .knowledge_store import KnowledgeStore, _stable_hash, _now_iso


# 关系谓词
PRED_BELONGS_TO = "belongs_to"
PRED_CONTAINS = "contains"
PRED_MENTIONED_IN = "mentioned_in"
PRED_DEFINED_IN = "defined_in"


def build_structural_relations(
    store: KnowledgeStore,
    book_id: str,
    document_ids: list[str] | None = None,
) -> dict[str, int]:
    """为一本书建立结构化关系（无模型）。

    扫描 chunks/documents/entities，生成 belongs_to/contains/mentioned_in/
    defined_in 关系，写入 relations 表。
    """
    stats = {"relations_created": 0}

    # 清理旧的结构化关系（P1-5）：
    # 1) 以本书 chunk 为 source 的 mentioned_in/defined_in 关系
    # 2) 本书文件实体为 subject 的 belongs_to 关系（source_chunk_id 为 NULL，
    #    仅靠 source_chunk_id 过滤会漏掉，导致重建后残留旧 belongs_to）
    scope_docs = document_ids or [
        r["document_id"] for r in store._conn.execute(
            "SELECT document_id FROM documents WHERE book_id=? AND status='active'",
            (book_id,),
        ).fetchall()
    ]
    doc_placeholders = ",".join("?" * len(scope_docs)) if scope_docs else ""
    chunk_ids = [
        r["chunk_id"] for r in store._conn.execute(
            f"SELECT chunk_id FROM chunks WHERE book_id=? AND active=1"
            f"{' AND document_id IN (' + doc_placeholders + ')' if doc_placeholders else ''}",
            [book_id, *scope_docs],
        ).fetchall()
    ]
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        store._conn.execute(
            f"DELETE FROM relations WHERE relation_source='structural' "
            f"AND source_chunk_id IN ({placeholders})",
            chunk_ids,
        )
    if doc_placeholders:
        store._conn.execute(
            f"DELETE FROM relations WHERE relation_source='structural' "
            f"AND book_id=? AND document_id IN ({doc_placeholders})",
            [book_id, *scope_docs],
        )

    # 文件 -> belongs_to -> 章节（通过 chunk 的 chapter 字段聚合）
    rows = store._conn.execute(
        f"SELECT DISTINCT document_id, chapter FROM chunks WHERE book_id=? AND active=1 "
        f"AND chapter!=''{' AND document_id IN (' + doc_placeholders + ')' if doc_placeholders else ''}",
        [book_id, *scope_docs],
    ).fetchall()
    for row in rows:
        doc_entity = _ensure_file_entity(store, row["document_id"], book_id)
        ch_entity = _ensure_chapter_entity(store, row["chapter"], book_id)
        if doc_entity and ch_entity:
            _add_relation(
                store, doc_entity, PRED_BELONGS_TO, ch_entity, None,
                book_id=book_id, document_id=row["document_id"],
            )
            stats["relations_created"] += 1

    # 章节 -> contains -> Chunk（chunk 作为虚拟实体，用 chunk_id 标识）
    # 实际上 chunk 不是 entity；用 mentioned_in 代替
    rows = store._conn.execute(
        """SELECT ce.entity_id, c.chunk_id FROM chunk_entities ce
           JOIN chunks c ON c.chunk_id=ce.chunk_id
           WHERE c.book_id=? AND c.active=1"""
        + (f" AND c.document_id IN ({doc_placeholders})" if doc_placeholders else ""),
        [book_id, *scope_docs],
    ).fetchall()
    for row in rows:
        # entity mentioned_in chunk（source_chunk_id 作为关系来源）
        _add_relation(
            store, row["entity_id"], PRED_MENTIONED_IN,
            None, row["chunk_id"], confidence=1.0, book_id=book_id,
            document_id=store.get_chunk(row["chunk_id"]).document_id
            if store.get_chunk(row["chunk_id"]) else "",
        )
        stats["relations_created"] += 1

    return stats


def expand_relations(store: KnowledgeStore, entity_ids: list[str],
                     max_hops: int = 2, max_nodes: int = 20) -> list[dict[str, Any]]:
    """从种子实体出发，最多 max_hops 跳关系扩展。

    返回扩展到的实体和关系链。PRD §7.2 限制：最多两跳、最多 20 个节点。
    """
    if not entity_ids:
        return []

    visited: set[str] = set(entity_ids)
    frontier: list[str] = list(entity_ids)
    results: list[dict[str, Any]] = []
    nodes_count = len(entity_ids)

    for hop in range(max_hops):
        if not frontier or nodes_count >= max_nodes:
            break
        next_frontier: list[str] = []
        for eid in frontier:
            # 找出 eid 作为 subject 或 object 的关系
            rel_rows = store._conn.execute(
                """SELECT r.relation_id, r.subject_entity_id, r.predicate,
                          r.object_entity_id, r.source_chunk_id, r.confidence,
                          se.name AS subject_name, oe.name AS object_name
                   FROM relations r
                   LEFT JOIN entities se ON se.entity_id=r.subject_entity_id
                   LEFT JOIN entities oe ON oe.entity_id=r.object_entity_id
                   WHERE r.subject_entity_id=? OR r.object_entity_id=?""",
                (eid, eid),
            ).fetchall()
            for row in rel_rows:
                other_id = (row["object_entity_id"] if row["subject_entity_id"] == eid
                            else row["subject_entity_id"])
                if not other_id or other_id in visited:
                    continue
                if nodes_count >= max_nodes:
                    break
                visited.add(other_id)
                next_frontier.append(other_id)
                nodes_count += 1
                results.append({
                    "hop": hop + 1,
                    "from_entity_id": eid,
                    "to_entity_id": other_id,
                    "predicate": row["predicate"],
                    "subject_name": row["subject_name"],
                    "object_name": row["object_name"],
                    "source_chunk_id": row["source_chunk_id"],
                    "confidence": row["confidence"],
                })
        frontier = next_frontier

    return results


def extract_semantic_relations(store: KnowledgeStore, book_id: str,
                               provider: Any = None,
                               remote: bool = False,
                               chunk_ids: list[str] | None = None) -> dict[str, int]:
    """有模型时语义关系抽取（KB3 §6.2）。

    对活跃 chunk 调 provider 抽取语义关系，要求返回 JSON：
    {"relations": [{"subject": "...", "predicate": "...", "object": "..."}]}。
    以 chunk 为 source 写入 relations 表；抽取失败/无 provider 时返回 0。
    remote=True 时跳过敏感 chunk（P0-5 隐私）。
    """
    if provider is None:
        return {"relations_created": 0}

    sql = (
        "SELECT c.*, d.content_role FROM chunks c "
        "JOIN documents d ON d.document_id=c.document_id "
        "WHERE c.book_id=? AND c.active=1"
    )
    params: list[Any] = [book_id]
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        sql += f" AND c.chunk_id IN ({placeholders})"
        params.extend(chunk_ids)
    sql += " ORDER BY c.document_id, c.ordinal"
    rows = store._conn.execute(sql, params).fetchall()
    if not rows:
        return {"relations_created": 0}

    from .knowledge_store import _row_to_chunk
    from .knowledge_organizer import _parse_model_json

    stats = {"relations_created": 0}
    for row in rows:
        chunk = _row_to_chunk(row)
        if (
            row["content_role"] == "control_surface"
            or getattr(chunk, "sensitivity", "normal") == "sensitive"
        ):
            continue
        text = (chunk.text or "").strip()
        if len(text) < 20:
            continue
        system = (
            "你是知识图谱抽取助手。从给定文本抽取实体间语义关系，只返回 JSON，不要额外文字。"
            '格式：{"relations": [{"subject": "主语实体", "predicate": "关系(英文动词或短词)", '
            '"object": "宾语实体"}]}。抽取 0-8 条最有信息量的关系。'
        )
        user = f"章节：{chunk.chapter}\n文本：\n{text[:2000]}"
        try:
            raw = provider.chat(system, user, max_tokens=300)
        except Exception:
            continue
        parsed = _parse_model_json(raw)
        if not parsed:
            continue
        rels = parsed.get("relations", [])
        if not isinstance(rels, list):
            continue
        for rel in rels:
            if not isinstance(rel, dict):
                continue
            subject = str(rel.get("subject", "")).strip()
            predicate = str(rel.get("predicate", "")).strip()
            obj = str(rel.get("object", "")).strip()
            if not subject or not predicate or not obj:
                continue
            subj_id = _ensure_entity(store, subject, "concept")
            obj_id = _ensure_entity(store, obj, "concept")
            if not subj_id or not obj_id:
                continue
            _add_relation(
                store, subj_id, predicate, obj_id, chunk.chunk_id,
                confidence=0.8, book_id=book_id, document_id=chunk.document_id,
                relation_source="semantic",
            )
            stats["relations_created"] += 1
    return stats


def _ensure_file_entity(store: KnowledgeStore, document_id: str, book_id: str) -> str | None:
    """文件作为 entity。"""
    row = store._conn.execute(
        "SELECT relative_path FROM documents WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if not row:
        return None
    name = row["relative_path"]
    return _ensure_scoped_entity(store, book_id, name, "file")


def _ensure_chapter_entity(store: KnowledgeStore, chapter: str, book_id: str) -> str | None:
    """章节作为 entity。"""
    return _ensure_scoped_entity(store, book_id, chapter, "concept")


def _ensure_scoped_entity(
    store: KnowledgeStore, book_id: str, name: str, entity_type: str,
) -> str | None:
    """为文件/章节创建按书隔离的实体，避免同名跨书串图。"""
    if not name or not name.strip():
        return None
    normalized = name.strip().casefold()
    entity_id = _stable_hash("ent", book_id, entity_type, normalized)
    row = store._conn.execute(
        "SELECT entity_id FROM entities WHERE entity_id=?", (entity_id,),
    ).fetchone()
    if row:
        return row["entity_id"]
    try:
        store._conn.execute(
            "INSERT INTO entities(entity_id, name, normalized_name, entity_type, description, aliases, active, created_at) "
            "VALUES(?,?,?,?,?,?,1,?)",
            (entity_id, name.strip(), normalized, entity_type, "", "", _now_iso()),
        )
        return entity_id
    except Exception:
        return None


def _ensure_entity(store: KnowledgeStore, name: str, entity_type: str) -> str | None:
    """确保实体存在，返回 entity_id。"""
    if not name or not name.strip():
        return None
    name = name.strip()
    normalized = name.casefold()
    row = store._conn.execute(
        "SELECT entity_id FROM entities WHERE normalized_name=?", (normalized,)
    ).fetchone()
    if row:
        return row["entity_id"]
    entity_id = _stable_hash("ent", normalized)
    try:
        store._conn.execute(
            "INSERT INTO entities(entity_id, name, normalized_name, entity_type, description, aliases, active, created_at) "
            "VALUES(?,?,?,?,?,?,1,?)",
            (entity_id, name, normalized, entity_type, "", "", _now_iso()),
        )
        return entity_id
    except Exception:
        return None


def _add_relation(store: KnowledgeStore, subject_id: str, predicate: str,
                  object_id: str | None, source_chunk_id: str | None,
                  confidence: float = 1.0, book_id: str = "",
                  document_id: str = "", relation_source: str = "structural") -> None:
    """添加关系。object_id 为空时用 source_chunk_id 作为虚拟 object。"""
    if not subject_id:
        return
    # object_id 为空时，用一个稳定的虚拟 entity_id 代表 chunk
    if not object_id and source_chunk_id:
        # chunk 不作为 entity，但关系需要 object_entity_id
        # 用 source_chunk_id 派生稳定 id，并在 entities 表占位
        chunk_entity_name = f"chunk:{source_chunk_id}"
        object_id = _ensure_scoped_entity(
            store, book_id or "legacy", chunk_entity_name, "chunk",
        )
        if not object_id:
            return
    if not object_id:
        return
    rel_id = _stable_hash(
        "rel", book_id, document_id, subject_id, predicate, object_id,
        source_chunk_id or "",
    )
    try:
        store._conn.execute(
            "INSERT OR IGNORE INTO relations(relation_id, subject_entity_id, predicate, "
            "object_entity_id, source_chunk_id, book_id, document_id, relation_source, "
            "confidence, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (rel_id, subject_id, predicate, object_id, source_chunk_id, book_id,
             document_id, relation_source, confidence, _now_iso()),
        )
    except Exception:
        pass
