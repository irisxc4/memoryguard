"""knowledge_organizer：摘要、关键词、实体抽取（KB3）。

无模型时规则化降级（PRD §6.1 永远执行的基础整理 + §6.3 无模型结构化关系）：
- 摘要：取 chunk 前 N 字符或首句
- 关键词：词频统计 + 标题词
- 实体：从章节标题、代码符号、配置 key 提取

有模型时（provider_api 可用）调 provider 增强（KB3 §6.2）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .knowledge_store import Chunk, KnowledgeStore


# 实体类型（PRD §5.1）
ENTITY_TYPES = frozenset({
    "concept", "person", "organization", "technology",
    "module", "file", "function", "configuration",
})

# 中文停用词（高频虚词）
_CN_STOPWORDS = frozenset("的一是不了在人有我他这中大来上个国和也子时道说".split())
_EN_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "this", "that", "these", "those", "it",
    "its", "they", "them", "their", "we", "you", "he", "she", "his", "her",
})

_HAN_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}")
_TITLE_PATTERN = re.compile(r"^#+\s+(.+)$", re.MULTILINE)
_CODE_DEF_PATTERN = re.compile(
    r"^\s*(?:def|class|function|func|fn|const|let|var|public|private|static)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_CONFIG_KEY_PATTERN = re.compile(r'^\s*"?([A-Za-z_][\w.-]*)"?\s*[:=]', re.MULTILINE)


@dataclass
class OrganizeResult:
    """单 chunk 整理结果。"""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    entities: list[dict[str, str]] = field(default_factory=list)


@dataclass
class EntityRecord:
    """实体记录（待入库）。"""
    name: str
    entity_type: str = "concept"
    aliases: list[str] = field(default_factory=list)
    description: str = ""


def organize_chunk(chunk: Chunk, book_title: str = "",
                   provider: Any = None) -> OrganizeResult:
    """整理单个 chunk。

    无 provider 时规则化：摘要取首句/前 N 字，关键词词频，实体从结构提取。
    有 provider 时调模型增强（KB3 §6.2，暂留接口）。
    """
    if provider is not None:
        # KB3 §6.2 模型增强：未来实现
        # result = provider.organize(chunk.text, book_title, chunk.chapter)
        # return OrganizeResult(summary=result.summary, ...)
        pass

    return _organize_rule_based(chunk, book_title)


def organize_book(store: KnowledgeStore, book_id: str,
                  provider: Any = None) -> dict[str, int]:
    """整理一本书的所有 chunk。返回统计。

    无模型时：生成摘要/关键词/实体，写入 chunks 表和 entities/chunk_entities 表。
    """
    book = store.get_book(book_id)
    book_title = book.title if book else ""

    # 收集所有 chunk
    rows = store._conn.execute(
        "SELECT * FROM chunks WHERE book_id=? AND active=1 ORDER BY document_id, ordinal",
        (book_id,),
    ).fetchall()

    stats = {"chunks_organized": 0, "entities_extracted": 0, "keywords_set": 0}

    for row in rows:
        chunk = _row_to_chunk(row)
        result = organize_chunk(chunk, book_title, provider)

        # 更新 chunk 摘要和关键词
        store._conn.execute(
            "UPDATE chunks SET summary=?, keywords=? WHERE chunk_id=?",
            (result.summary, ",".join(result.keywords[:10]), chunk.chunk_id),
        )

        # 入库实体
        for ent in result.entities:
            entity_id = _ensure_entity(store, ent["name"], ent.get("type", "concept"))
            if entity_id:
                store._conn.execute(
                    "INSERT OR IGNORE INTO chunk_entities(chunk_id, entity_id, role) VALUES(?,?,?)",
                    (chunk.chunk_id, entity_id, "mention"),
                )
                stats["entities_extracted"] += 1

        stats["chunks_organized"] += 1
        stats["keywords_set"] += 1

    # 更新书籍 entity_count
    ent_count = store._conn.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM chunk_entities ce "
        "JOIN chunks c ON c.chunk_id=ce.chunk_id WHERE c.book_id=?",
        (book_id,),
    ).fetchone()[0]
    from .knowledge_store import _now_iso
    store._conn.execute(
        "UPDATE books SET entity_count=?, updated_at=? WHERE book_id=?",
        (ent_count, _now_iso(), book_id),
    )

    return stats


def _organize_rule_based(chunk: Chunk, book_title: str) -> OrganizeResult:
    """无模型规则化整理。"""
    text = chunk.text or ""
    summary = _extract_summary(text)
    keywords = _extract_keywords(text, chunk.chapter, chunk.section)
    entities = _extract_entities(text, chunk.chapter, chunk.section)

    # 书名作为实体
    if book_title:
        entities.insert(0, {"name": book_title, "type": "concept"})

    return OrganizeResult(
        summary=summary,
        keywords=keywords[:10],
        entities=entities[:20],
    )


def _extract_summary(text: str, max_len: int = 120) -> str:
    """取首句或前 max_len 字符作为摘要。"""
    if not text:
        return ""
    text = text.strip()
    # 首句边界：中文句号/问号/叹号，英文句点
    m = re.search(r"[。！？.!?\n]", text)
    if m and m.start() <= max_len:
        return text[:m.start() + 1].strip()
    return text[:max_len].strip() + ("..." if len(text) > max_len else "")


def _extract_keywords(text: str, chapter: str = "", section: str = "") -> list[str]:
    """词频统计提取关键词。"""
    if not text:
        return []
    counts: dict[str, int] = {}

    # 中文 bigram
    for run in _HAN_PATTERN.findall(text):
        if len(run) >= 2:
            for i in range(len(run) - 1):
                bg = run[i:i + 2]
                if bg not in _CN_STOPWORDS:
                    counts[bg] = counts.get(bg, 0) + 1

    # 英文单词
    for w in _WORD_PATTERN.findall(text):
        wl = w.lower()
        if wl not in _EN_STOPWORDS:
            counts[w] = counts.get(w, 0) + 1

    # 标题词加权
    for title_part in [chapter, section]:
        if title_part:
            for run in _HAN_PATTERN.findall(title_part):
                if len(run) >= 2:
                    for i in range(len(run) - 1):
                        bg = run[i:i + 2]
                        counts[bg] = counts.get(bg, 0) + 3
            for w in _WORD_PATTERN.findall(title_part):
                counts[w] = counts.get(w, 0) + 3

    # 按频率排序
    sorted_kw = sorted(counts.items(), key=lambda x: -x[1])
    return [kw for kw, _ in sorted_kw[:10]]


def _extract_entities(text: str, chapter: str = "", section: str = "") -> list[dict[str, str]]:
    """从结构提取实体。"""
    entities: list[dict[str, str]] = []

    # 章节标题作为 concept 实体
    if chapter:
        entities.append({"name": chapter, "type": "concept"})
    if section and section != chapter:
        entities.append({"name": section, "type": "concept"})

    # 代码定义（function/class）
    for m in _CODE_DEF_PATTERN.finditer(text):
        name = m.group(1)
        if name and len(name) >= 2:
            # 猜测类型
            etype = "function" if m.group(0).strip().startswith(("def", "function", "func", "fn")) else "concept"
            if "class" in m.group(0):
                etype = "concept"
            entities.append({"name": name, "type": etype})

    # 配置 key
    for m in _CONFIG_KEY_PATTERN.finditer(text):
        name = m.group(1)
        if name and len(name) >= 3 and not name.startswith(("http", "www")):
            entities.append({"name": name, "type": "configuration"})

    # 去重
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for e in entities:
        key = (e["name"], e["type"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _ensure_entity(store: KnowledgeStore, name: str, entity_type: str) -> str | None:
    """确保实体存在，返回 entity_id。"""
    if not name or not name.strip():
        return None
    name = name.strip()
    normalized = name.casefold()
    # 查找已有
    row = store._conn.execute(
        "SELECT entity_id FROM entities WHERE normalized_name=?", (normalized,)
    ).fetchone()
    if row:
        return row["entity_id"]
    # 新建
    from .knowledge_store import _stable_hash, _now_iso
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


def _row_to_chunk(row) -> Chunk:
    """sqlite3.Row 转 Chunk。"""
    from .knowledge_store import _row_to_chunk as _rc
    return _rc(row)
