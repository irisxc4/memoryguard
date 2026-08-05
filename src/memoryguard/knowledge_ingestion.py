"""knowledge_ingestion：文件夹扫描和增量更新（KB1）。

扫描文件夹 → 解析文件 → 切片 → 写入 KnowledgeStore。
增量更新：基于 content_hash 判断文件是否变化，只重新处理变化的文件。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .data_home import resolve_data_home
from .knowledge_chunker import chunk_document
from .knowledge_parser import parse_file, SUPPORTED_EXTENSIONS, CODE_EXTENSIONS
from .knowledge_store import Book, Chunk, KnowledgeStore, _stable_hash
from .source_registry import (
    DEFAULT_PROJECT_EXCLUDE,
    INSTRUCTION_FILES,
    ScanBudget,
)

# 知识库扫描默认预算（复用 SourceRegistry 的预算语义，防止失控）
KNOWLEDGE_SCAN_BUDGET = ScanBudget(
    max_files=20000,
    max_total_size=500 * 1024 * 1024,
    max_single_file=5 * 1024 * 1024,
    max_depth=20,
    timeout_seconds=120,
)

# 控制面文件默认排除：指令文件可浏览，但不进入 KAG Bootstrap
CONTROL_SURFACE_FILES = frozenset(INSTRUCTION_FILES)


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

    # 扫描文件（带预算：文件数/大小/深度/超时/符号链接与默认排除）
    all_files, over_budget = _scan_files(
        root, book.include_globs, book.exclude_globs, budget=KNOWLEDGE_SCAN_BUDGET,
    )
    store.create_job(job_id, book_id, len(all_files))

    # 检查已有文档
    existing_docs = {
        row["relative_path"]: row
        for row in store.list_documents(book_id)
    }
    current_paths = set()
    processed = 0
    skipped = 0
    chunks_created = 0

    for file_path in all_files:
        rel = str(file_path.relative_to(root)).replace("\\", "/")
        current_paths.add(rel)

        # 计算 content_hash（稳定读取：stat 前/后 + 只读一次）
        try:
            before = file_path.stat()
            content = file_path.read_bytes()
            after = file_path.stat()
            if before.st_size != after.st_size or before.st_mtime != after.st_mtime:
                skipped += 1
                continue  # 读取过程中文件被修改，本轮跳过
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

        # 控制面内容分类（指令文件只入库标记，不进入 KAG Bootstrap）
        content_role = "knowledge"
        if Path(rel).name in CONTROL_SURFACE_FILES:
            content_role = "control_surface"

        # 切片
        document_id = _stable_hash(book_id, rel)
        chunks = chunk_document(parsed, book_id, document_id)

        # 原子替换：文档哈希与 Chunk 替换在同一事务完成
        store.replace_document_revision(
            document_id, book_id, rel, parsed.media_type,
            content_hash, chunks, content_role=content_role,
        )

        chunks_created += len(chunks)
        processed += 1
        store.update_job(job_id, "running", phase="indexing", processed=processed)

    # 检测已删除的文件
    deleted = 0
    for rel, doc_row in existing_docs.items():
        if rel not in current_paths:
            store.deactivate_document(doc_row["document_id"])
            deleted += 1

    # 从数据库重新统计（不拿"本次处理了什么"冒充"书库目前有什么"）
    stats = _book_stats(store, book_id)
    store.update_book_status(
        book_id, "ready",
        file_count=stats["files"],
        chapter_count=stats["chapters"],
        chunk_count=stats["chunks"],
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

    # KB2 向量索引：provider 可用且（本地或已授权远程）时为 chunk 生成 embedding
    if processed > 0 and book.vector_enabled != "off":
        try:
            generate_embeddings(store, book.book_id, book.remote_embedding_allowed)
        except Exception:
            # embedding 失败不影响入库（FTS 仍可用）
            pass

    store.update_job(job_id, "done", phase="complete", processed=processed)

    return IngestionResult(
        book_id=book_id,
        files_total=len(all_files),
        files_processed=processed,
        files_skipped=skipped,
        files_deleted=deleted,
        chunks_created=chunks_created,
        chapters=set(stats["chapter_list"]),
        error=("扫描超预算，部分文件未处理" if over_budget else ""),
    )


def generate_embeddings(store: KnowledgeStore, book_id: str,
                        remote_allowed: bool = False) -> int:
    """为缺少 embedding 的 chunk 批量生成向量（KB2）。

    返回生成的 embedding 数量。provider 不可用、或远程 provider 未授权时返回 0。
    可单独调用，便于 GUI "重新智能整理" 触发。
    """
    from .provider_api import ProviderConfig, get_provider, _provider_config

    backend = get_provider()
    if backend is None:
        return 0

    # 远程 provider 必须先经用户对每本书显式授权，否则不回传文档内容
    cfg: ProviderConfig | None = _provider_config
    if cfg is not None and not _is_local_base(cfg.api_base) and not remote_allowed:
        return 0

    # 确定 embedding_model 名称与空间 id（模型变化即产生新空间，避免混用）
    model_name = "unknown"
    try:
        if _provider_config is not None:
            model_name = (_provider_config.embedding_model
                          or _provider_config.model
                          or "unknown")
    except Exception:
        pass
    embedding_space_id = f"model:{model_name}"

    # 列出需要 embedding 的 chunk（text_hash 变化则需重建）
    rows = store.list_chunks_without_embedding(book_id, model_name, embedding_space_id)
    if not rows:
        return 0

    chunk_ids = [r["chunk_id"] for r in rows]
    text_hashes = [r["text_hash"] for r in rows]

    # 获取 chunk 文本，拼装 embedding 输入
    # PRD §4.3: embedding 输入 = 书名 + 文件名 + 章节路径 + 正文
    book = store.get_book(book_id)
    book_title = book.title if book else ""
    texts: list[str] = []
    for cid in chunk_ids:
        chunk = store.get_chunk(cid)
        if chunk:
            text = f"{book_title} {chunk.chapter} {chunk.section}\n{chunk.text}"
            texts.append(text)
        else:
            texts.append("")

    try:
        vectors = backend.embed_many(texts)
    except Exception:
        return 0

    if len(vectors) != len(chunk_ids):
        return 0

    count = 0
    for cid, vec, th in zip(chunk_ids, vectors, text_hashes):
        if not vec:
            continue
        store.upsert_embedding(
            chunk_id=cid,
            embedding_model=model_name,
            dimension=len(vec),
            vector=vec,
            text_hash=th,
            embedding_space_id=embedding_space_id,
        )
        count += 1
    return count


def _scan_files(root: Path, include_globs: str, exclude_globs: str,
                budget: ScanBudget | None = None) -> tuple[list[Path], bool]:
    """扫描目录下所有支持的文件（带预算与安全边界）。

    返回 (文件列表, 是否超预算)。复用 SourceRegistry 的默认排除与预算语义：
    文件数、总大小、单文件大小、深度、超时、符号链接逃逸、默认排除目录。
    """
    budget = budget or KNOWLEDGE_SCAN_BUDGET
    include_patterns = [g.strip() for g in include_globs.split(",") if g.strip()] if include_globs else []
    exclude_patterns = [g.strip() for g in exclude_globs.split(",") if g.strip()] if exclude_globs else []
    exclude_patterns = list(DEFAULT_PROJECT_EXCLUDE) + exclude_patterns

    all_supported = set(SUPPORTED_EXTENSIONS.keys()) | set(CODE_EXTENSIONS.keys())
    files: list[Path] = []
    over_budget = False
    total_size = 0
    start = time.time()

    # 解析并缓存真实根路径，用于符号链接逃逸检测
    real_root = root.resolve()

    def _depth(p: Path) -> int:
        return len(p.relative_to(root).parts)

    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() - start > budget.timeout_seconds:
            over_budget = True
            break
        if _depth(Path(dirpath)) > budget.max_depth:
            dirnames[:] = []
            continue
        # 跳过符号链接目录（防逃逸）与排除目录
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and not _dir_excluded(dirpath, d, exclude_patterns)
        ]
        # 符号链接目录不进入（os.walk 默认不跟随 symlink dir，但显式防护）
        for fname in filenames:
            if fname.startswith("."):
                continue
            if len(files) >= budget.max_files:
                over_budget = True
                break
            ext = Path(fname).suffix.lower()
            if ext not in all_supported:
                continue
            file_path = Path(dirpath) / fname
            if file_path.is_symlink():
                continue  # 符号链接文件不扫描
            try:
                if not str(file_path.resolve()).startswith(str(real_root)):
                    continue  # 逃逸出根目录
                size = file_path.stat().st_size
            except OSError:
                continue
            if size > budget.max_single_file:
                over_budget = True
                continue
            if total_size + size > budget.max_total_size:
                over_budget = True
                break
            rel = str(file_path.relative_to(root)).replace("\\", "/")

            # 应用 exclude
            if any(_match_glob(rel, p) for p in exclude_patterns):
                continue

            # 应用 include（如果指定了）
            if include_patterns and not any(_match_glob(rel, p) for p in include_patterns):
                continue

            files.append(file_path)
            total_size += size

    return sorted(files), over_budget


def _dir_excluded(dirpath: str, dirname: str, exclude_patterns: list[str]) -> bool:
    """判断目录是否被排除（按相对路径 glob 匹配）。"""
    full = f"{dirname}/"
    for p in exclude_patterns:
        if p.endswith("/**") and p[:-3] == dirname:
            return True
    return bool(full and any(_match_glob(full, p) for p in exclude_patterns))


def _is_local_base(api_base: str) -> bool:
    """判断 provider base_url 是否为本地（localhost/127.0.0.1/::1）。"""
    base = (api_base or "").strip().lower()
    return any(host in base for host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"))


def _book_stats(store: KnowledgeStore, book_id: str) -> dict[str, object]:
    """从数据库统计一本书当前的真实规模（文件/章节/片段）。"""
    conn = store._conn
    files = conn.execute(
        "SELECT COUNT(*) FROM documents WHERE book_id=? AND status='active'",
        (book_id,),
    ).fetchone()[0]
    chapters = conn.execute(
        "SELECT COUNT(DISTINCT chapter) FROM chunks WHERE book_id=? AND active=1 AND chapter!=''",
        (book_id,),
    ).fetchone()[0]
    chunks = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE book_id=? AND active=1",
        (book_id,),
    ).fetchone()[0]
    chapter_list = [
        row["chapter"] for row in conn.execute(
            "SELECT DISTINCT chapter FROM chunks WHERE book_id=? AND active=1 "
            "AND chapter!='' ORDER BY chapter",
            (book_id,),
        )
    ]
    return {"files": files, "chapters": chapters, "chunks": chunks,
            "chapter_list": chapter_list}


def _match_glob(path: str, pattern: str) -> bool:
    """简单 glob 匹配。"""
    import fnmatch
    return fnmatch.fnmatch(path, pattern)
