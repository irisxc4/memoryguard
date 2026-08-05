"""knowledge_ingestion：文件夹扫描和增量更新（KB1）。

扫描文件夹 → 解析文件 → 切片 → 写入 KnowledgeStore。
增量更新：基于 content_hash 判断文件是否变化，只重新处理变化的文件。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .data_home import resolve_data_home
from .knowledge_chunker import chunk_document
from .knowledge_parser import parse_file, SUPPORTED_EXTENSIONS, CODE_EXTENSIONS
from .knowledge_store import Book, Chunk, KnowledgeStore, _stable_hash


@dataclass
class IngestionResult:
    """单次入库结果。"""
    book_id: str
    files_total: int
    files_processed: int
    files_skipped: int
    files_deleted: int
    chunks_created: int
    chapters: set[str]
    error: str = ""


def create_book(store: KnowledgeStore, root_path: str, title: str = "",
                include_globs: str = "", exclude_globs: str = "",
                auto_extract_memory: bool = True,
                vector_enabled: str = "auto") -> Book:
    """创建一本书并返回。"""
    root = Path(root_path).resolve()
    if not title:
        title = root.name
    book_id = _stable_hash("book", str(root))
    existing = store.get_book(book_id)
    if existing:
        return existing
    book = Book(
        book_id=book_id,
        title=title,
        root_path=str(root),
        include_globs=include_globs,
        exclude_globs=exclude_globs,
        auto_extract_memory=auto_extract_memory,
        vector_enabled=vector_enabled,
    )
    store.add_book(book)
    return book


def ingest_book(store: KnowledgeStore, book_id: str) -> IngestionResult:
    """扫描并入库一本书。增量更新：只处理变化的文件。"""
    book = store.get_book(book_id)
    if not book:
        return IngestionResult(book_id=book_id, files_total=0, files_processed=0,
                               files_skipped=0, files_deleted=0, chunks_created=0,
                               chapters=set(), error="book not found")

    root = Path(book.root_path)
    if not root.is_dir():
        return IngestionResult(book_id=book_id, files_total=0, files_processed=0,
                               files_skipped=0, files_deleted=0, chunks_created=0,
                               chapters=set(), error="root directory not found")

    # 创建索引任务
    job_id = _stable_hash("job", book_id, str(hashlib.sha256(str(root).encode()).hexdigest()[:8]))

    # 扫描文件
    all_files = _scan_files(root, book.include_globs, book.exclude_globs)
    store.create_job(job_id, book_id, len(all_files))

    # 检查已有文档
    existing_docs = {
        row["relative_path"]: row
        for row in store.list_documents(book_id)
    }
    current_paths = set()
    chapters: set[str] = set()
    processed = 0
    skipped = 0
    chunks_created = 0

    for file_path in all_files:
        rel = str(file_path.relative_to(root)).replace("\\", "/")
        current_paths.add(rel)

        # 计算 content_hash
        try:
            content = file_path.read_bytes()
            content_hash = hashlib.sha256(content).hexdigest()[:16]
        except OSError:
            skipped += 1
            continue

        # 检查是否需要更新
        existing = existing_docs.get(rel)
        if existing and existing["content_hash"] == content_hash:
            skipped += 1
            continue

        # 解析文件
        parsed = parse_file(file_path, root)
        if not parsed:
            skipped += 1
            continue

        # 创建/更新文档
        document_id = _stable_hash(book_id, rel)
        store.upsert_document(document_id, book_id, rel, parsed.media_type, content_hash)

        # 切片
        chunks = chunk_document(parsed, book_id, document_id)
        store.replace_document_chunks(document_id, chunks)

        # 收集章节
        for c in chunks:
            if c.chapter:
                chapters.add(c.chapter)

        chunks_created += len(chunks)
        processed += 1
        store.update_job(job_id, "running", phase="indexing", processed=processed)

    # 检测已删除的文件
    deleted = 0
    for rel, doc_row in existing_docs.items():
        if rel not in current_paths:
            store.deactivate_document(doc_row["document_id"])
            deleted += 1

    # 更新书籍状态
    total_chunks = store.count_chunks(book_id)
    store.update_book_status(
        book_id, "ready",
        file_count=len(all_files),
        chapter_count=len(chapters),
        chunk_count=total_chunks,
    )

    # KB3 基础整理：摘要/关键词/实体/结构化关系（无模型规则化，PRD §6.1 永远执行）
    if processed > 0:
        try:
            from .knowledge_organizer import organize_book
            from .knowledge_graph import build_structural_relations
            organize_book(store, book_id)
            build_structural_relations(store, book_id)
        except Exception:
            # 整理失败不影响入库结果（KB1 核心已完成）
            pass

    store.update_job(job_id, "done", phase="complete", processed=processed)

    return IngestionResult(
        book_id=book_id,
        files_total=len(all_files),
        files_processed=processed,
        files_skipped=skipped,
        files_deleted=deleted,
        chunks_created=chunks_created,
        chapters=chapters,
    )


def _scan_files(root: Path, include_globs: str, exclude_globs: str) -> list[Path]:
    """扫描目录下所有支持的文件。"""
    include_patterns = [g.strip() for g in include_globs.split(",") if g.strip()] if include_globs else []
    exclude_patterns = [g.strip() for g in exclude_globs.split(",") if g.strip()] if exclude_globs else []

    all_supported = set(SUPPORTED_EXTENSIONS.keys()) | set(CODE_EXTENSIONS.keys())
    files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤隐藏目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = Path(fname).suffix.lower()
            if ext not in all_supported:
                continue
            file_path = Path(dirpath) / fname
            rel = str(file_path.relative_to(root)).replace("\\", "/")

            # 应用 exclude
            if any(_match_glob(rel, p) for p in exclude_patterns):
                continue

            # 应用 include（如果指定了）
            if include_patterns and not any(_match_glob(rel, p) for p in include_patterns):
                continue

            files.append(file_path)

    return sorted(files)


def _match_glob(path: str, pattern: str) -> bool:
    """简单 glob 匹配。"""
    import fnmatch
    return fnmatch.fnmatch(path, pattern)
