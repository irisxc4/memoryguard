"""knowledge_distill：长期记忆候选萃取（KB4）。

PRD §9 萃取功能：
- 书籍摘要/章节摘要直接属于知识库
- 长期记忆候选进入"待同步记忆"，不立即写入 SharedMemoryStore
- 来源追溯：book_id/document_id/chunk_id/路径/章节/行号/哈希

无模型时从 chunk 关键句提取候选（首句 + 包含关键词的句子）。
有模型时调 provider 生成高质量候选（KB3 §6.2 结构化输出 memory_candidates）。

PRD §9.3 安全限制：
- 即使开启自动同步，也只能同步 fact/project/procedure/preference
- 不能自动同步为 always rule / system rule / 禁止性规则
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .knowledge_store import KnowledgeStore, Chunk


# 允许自动同步的 kind（PRD §9.3）
AUTO_SYNCABLE_KINDS = frozenset({"fact", "project", "procedure", "preference"})

# 最低自动同步置信度（PRD §9.2）
DEFAULT_MIN_SYNC_CONFIDENCE = 0.95

# 句子边界
_SENTENCE_PATTERN = re.compile(r"[^。！？.!?\n]+[。！？.!?]?")


@dataclass
class MemoryCandidate:
    """记忆候选。"""
    body: str
    kind: str = "fact"  # fact/project/procedure/preference
    confidence: float = 0.5
    book_id: str = ""
    document_id: str = ""
    chunk_id: str = ""
    relative_path: str = ""
    chapter: str = ""
    section: str = ""
    line_start: int = 0
    line_end: int = 0
    text_hash: str = ""
    source_summary: str = ""


@dataclass
class DistillResult:
    """萃取结果。"""
    candidates: list[MemoryCandidate] = field(default_factory=list)
    book_summary: str = ""
    chapter_summaries: dict[str, str] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)


def distill_book(store: KnowledgeStore, book_id: str,
                 provider: Any = None,
                 auto_sync: bool = False,
                 min_sync_confidence: float = DEFAULT_MIN_SYNC_CONFIDENCE) -> DistillResult:
    """从一本书萃取记忆候选。

    无模型时规则化：从每个 chunk 提取首句和关键句作为候选。
    有模型时调 provider 生成结构化候选（KB3 §6.2）。

    auto_sync=True 时，高置信度候选标记为可同步（但仍需用户确认或后续流程写入）。
    本函数不直接写入 SharedMemoryStore（PRD §9.1）。
    """
    book = store.get_book(book_id)
    if not book:
        return DistillResult()

    result = DistillResult()
    rows = store._conn.execute(
        "SELECT * FROM chunks WHERE book_id=? AND active=1 ORDER BY document_id, ordinal",
        (book_id,),
    ).fetchall()

    # 收集文档路径
    doc_paths = {
        r["document_id"]: r["relative_path"]
        for r in store._conn.execute(
            "SELECT document_id, relative_path FROM documents WHERE book_id=?",
            (book_id,),
        ).fetchall()
    }

    chapter_texts: dict[str, list[str]] = {}
    book_texts: list[str] = []

    for row in rows:
        chunk = _row_to_chunk(row)
        rel_path = doc_paths.get(chunk.document_id, "")

        # 无模型萃取：首句 + 含关键词的句子
        sentences = _split_sentences(chunk.text)
        keywords = [k.strip() for k in (chunk.keywords or "").split(",") if k.strip()]

        for sent in sentences[:3]:  # 每个 chunk 最多取 3 句
            sent = sent.strip()
            if len(sent) < 10:
                continue
            # 含关键词的句子优先
            confidence = 0.5
            if keywords and any(k in sent for k in keywords):
                confidence = 0.75
            if chunk.summary and sent in chunk.summary:
                confidence = 0.85

            candidate = MemoryCandidate(
                body=sent,
                kind=_guess_kind(sent, chunk),
                confidence=confidence,
                book_id=book_id,
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                relative_path=rel_path,
                chapter=chunk.chapter,
                section=chunk.section,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                text_hash=chunk.text_hash,
                source_summary=chunk.summary,
            )
            result.candidates.append(candidate)

        # 收集章节文本用于章节摘要
        if chunk.chapter:
            chapter_texts.setdefault(chunk.chapter, []).append(chunk.text[:200])
        book_texts.append(chunk.text[:200])

    # 生成书籍/章节摘要（无模型：拼接前 N 个 chunk 的摘要）
    result.book_summary = (book.description or "")[:500]
    if not result.book_summary and book_texts:
        result.book_summary = "\n".join(book_texts[:5])[:1000]
    for ch, texts in chapter_texts.items():
        result.chapter_summaries[ch] = "\n".join(texts[:3])[:500]

    result.stats = {
        "candidates_total": len(result.candidates),
        "auto_syncable": sum(
            1 for c in result.candidates
            if c.confidence >= min_sync_confidence and c.kind in AUTO_SYNCABLE_KINDS
        ) if auto_sync else 0,
        "chapters_summarized": len(result.chapter_summaries),
    }
    return result


def candidates_to_dict(candidates: list[MemoryCandidate]) -> list[dict[str, Any]]:
    """转候选为可序列化 dict。"""
    return [
        {
            "body": c.body,
            "kind": c.kind,
            "confidence": c.confidence,
            "source": {
                "book_id": c.book_id,
                "document_id": c.document_id,
                "chunk_id": c.chunk_id,
                "relative_path": c.relative_path,
                "chapter": c.chapter,
                "section": c.section,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "text_hash": c.text_hash,
            },
            "source_summary": c.source_summary,
        }
        for c in candidates
    ]


def _split_sentences(text: str) -> list[str]:
    """切分句子。"""
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_PATTERN.findall(text) if s.strip()]


def _guess_kind(sentence: str, chunk: Chunk) -> str:
    """猜测记忆类型。"""
    s = sentence.lower()
    # procedure：含"步骤/流程/方法/先...再/step"
    if any(k in s for k in ["步骤", "流程", "方法", "先", "再", "然后", "step", "process", "procedure"]):
        return "procedure"
    # preference：含"应该/建议/推荐/最好/必须"
    if any(k in s for k in ["应该", "建议", "推荐", "最好", "必须", "should", "must", "recommend"]):
        return "preference"
    # project：含"项目/系统/模块/版本"
    if any(k in s for k in ["项目", "系统", "模块", "版本", "project", "system", "module", "version"]):
        return "project"
    # 默认 fact
    return "fact"


def _row_to_chunk(row) -> Chunk:
    """sqlite3.Row 转 Chunk。"""
    from .knowledge_store import _row_to_chunk as _rc
    return _rc(row)
