"""knowledge_chunker：分章和切片（KB1）。

按标题边界 → 代码块 → 表格 → 列表 → 段落 → 句子 → 强制长度切分。
目标 600-800 tokens，最大 1000，最小 150，重叠 60-80。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .knowledge_parser import ParsedBlock, ParsedDocument
from .knowledge_store import Chunk, _stable_hash, _text_hash

# 切片参数
TARGET_CHARS = 700  # 近似 token 数（中文 1 字 ≈ 1-2 token，英文 1 word ≈ 1-1.5 token）
MAX_CHARS = 1000
MIN_CHARS = 150
OVERLAP_CHARS = 80


@dataclass
class ChunkDraft:
    """切片草案，尚未分配 chunk_id。"""
    text: str
    chapter: str
    section: str
    line_start: int
    line_end: int


def chunk_document(doc: ParsedDocument, book_id: str, document_id: str) -> list[Chunk]:
    """将解析后的文档切片为 Chunk 列表。"""
    # 1. 构建章节路径
    chapter = ""
    section = ""
    drafts: list[ChunkDraft] = []

    for block in doc.blocks:
        # 更新章节路径
        if block.block_type == "heading":
            if block.heading_level == 1:
                chapter = block.heading_text
                section = ""
            elif block.heading_level == 2:
                section = block.heading_text
            elif block.heading_level >= 3:
                section = block.heading_text
            continue

        # 代码块/表格：整体作为一个 chunk（除非超长）
        if block.block_type in ("code", "table"):
            if len(block.text) <= MAX_CHARS:
                drafts.append(ChunkDraft(
                    text=block.text, chapter=chapter, section=section,
                    line_start=block.line_start, line_end=block.line_end,
                ))
            else:
                for sub in _split_by_length(block.text, MAX_CHARS, OVERLAP_CHARS):
                    drafts.append(ChunkDraft(
                        text=sub.text, chapter=chapter, section=section,
                        line_start=block.line_start, line_end=block.line_end,
                    ))
            continue

        # 列表/段落：尝试合并到目标长度
        if block.block_type in ("list", "paragraph"):
            if len(block.text) <= MAX_CHARS:
                drafts.append(ChunkDraft(
                    text=block.text, chapter=chapter, section=section,
                    line_start=block.line_start, line_end=block.line_end,
                ))
            else:
                # 按句子切分，再合并到目标长度
                sentences = _split_sentences(block.text)
                for sub in _merge_sentences(sentences, TARGET_CHARS, MAX_CHARS, OVERLAP_CHARS):
                    drafts.append(ChunkDraft(
                        text=sub.text, chapter=chapter, section=section,
                        line_start=block.line_start, line_end=block.line_end,
                    ))
            continue

    # 2. 合并过短的 chunk（小于 MIN_CHARS 且有下一个 chunk）
    merged = _merge_short(drafts, MIN_CHARS)

    # 3. 生成 Chunk 对象
    chunks: list[Chunk] = []
    for i, draft in enumerate(merged):
        chunk_id = _stable_hash(document_id, str(i), draft.text[:100])
        chunks.append(Chunk(
            chunk_id=chunk_id,
            document_id=document_id,
            book_id=book_id,
            chapter=draft.chapter,
            section=draft.section,
            ordinal=i,
            text=draft.text,
            summary="",  # KB3 由 organizer 填充
            keywords="",  # KB3 由 organizer 填充
            line_start=draft.line_start,
            line_end=draft.line_end,
            text_hash=_text_hash(draft.text),
        ))

    return chunks


def _split_sentences(text: str) -> list[str]:
    """按句子边界切分。支持中英文标点。"""
    # 中英文句号、问号、感叹号、分号
    parts = re.split(r'(?<=[。！？；;!?])\s*', text)
    return [p.strip() for p in parts if p.strip()]


def _merge_sentences(sentences: list[str], target: int, maximum: int,
                     overlap: int) -> list[ChunkDraft]:
    """将句子合并到目标长度，带重叠。"""
    if not sentences:
        return []
    drafts: list[ChunkDraft] = []
    current = ""
    for sent in sentences:
        if current and len(current) + len(sent) > maximum:
            drafts.append(ChunkDraft(text=current, chapter="", section="",
                                     line_start=0, line_end=0))
            # 重叠：保留尾部
            if overlap > 0 and len(current) > overlap:
                current = current[-overlap:] + sent
            else:
                current = sent
        else:
            current = (current + "\n" + sent).strip() if current else sent
    if current:
        drafts.append(ChunkDraft(text=current, chapter="", section="",
                                 line_start=0, line_end=0))
    return drafts


def _split_by_length(text: str, maximum: int, overlap: int) -> list[ChunkDraft]:
    """强制长度切分，带重叠。"""
    if len(text) <= maximum:
        return [ChunkDraft(text=text, chapter="", section="", line_start=0, line_end=0)]
    drafts: list[ChunkDraft] = []
    start = 0
    while start < len(text):
        end = min(start + maximum, len(text))
        drafts.append(ChunkDraft(text=text[start:end], chapter="", section="",
                                 line_start=0, line_end=0))
        if end >= len(text):
            break
        start = end - overlap if end - overlap > start else end
    return drafts


def _merge_short(drafts: list[ChunkDraft], minimum: int) -> list[ChunkDraft]:
    """合并过短的 chunk 到下一个。"""
    if len(drafts) <= 1:
        return drafts
    result: list[ChunkDraft] = []
    i = 0
    while i < len(drafts):
        current = drafts[i]
        # 如果当前 chunk 太短且有下一个，合并
        while len(current.text) < minimum and i + 1 < len(drafts):
            i += 1
            next_draft = drafts[i]
            current = ChunkDraft(
                text=current.text + "\n" + next_draft.text,
                chapter=current.chapter,
                section=current.section,
                line_start=current.line_start,
                line_end=next_draft.line_end,
            )
        result.append(current)
        i += 1
    return result
