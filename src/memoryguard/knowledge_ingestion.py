"""knowledge_ingestion：文件夹扫描和增量更新（KB1）。

扫描文件夹 → 解析文件 → 切片 → 写入 KnowledgeStore。
增量更新：基于 content_hash 判断文件是否变化，只重新处理变化的文件。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .knowledge_chunker import chunk_document
from .knowledge_parser import parse_content, SUPPORTED_EXTENSIONS, CODE_EXTENSIONS
from .knowledge_store import Book, Chunk, KnowledgeStore, _stable_hash
from .knowledge_policy import KnowledgeAccessPolicy
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

# 每本书的入库锁：阻止同一本书并发重建（P1-9）
_book_locks: dict[str, threading.Lock] = {}
_book_locks_guard = threading.Lock()


def _book_lock(book_id: str) -> threading.Lock:
    global _book_locks
    with _book_locks_guard:
        lock = _book_locks.get(book_id)
        if lock is None:
            lock = threading.Lock()
            _book_locks[book_id] = lock
        return lock


@dataclass
class ScanTruncation:
    """一次扫描截断的原因。"""
    reason: str  # max_files / max_total_size / max_single_file / timeout / depth
    detail: str = ""


@dataclass
class KnowledgeScanResult:
    """扫描结果：除了文件列表，还告知扫描是否完整及截断原因。

    只有 complete=True 时才能推断"未扫到=已删除"，否则禁止删除旧文档（P0-1）。
    """
    files: list[Path] = field(default_factory=list)
    complete: bool = True
    truncations: list[ScanTruncation] = field(default_factory=list)
    scanned_count: int = 0
    total_size: int = 0
    # 已通过扩展名和 include/exclude 过滤、且在目录遍历中出现过的路径。
    seen_paths: set[str] = field(default_factory=set)
    # 暂时不可读取的文件或目录，必须阻止删除对应旧索引。
    unreadable_entries: list[ScanTruncation] = field(default_factory=list)


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
    status: str = "ready"  # ready / partial / failed
    truncations: list[ScanTruncation] = field(default_factory=list)


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
    """扫描并入库一本书（每本书加锁，阻止并发重建 P1-9）。"""
    with _book_lock(book_id):
        return _ingest_book_unlocked(store, book_id)


def _ingest_book_unlocked(store: KnowledgeStore, book_id: str) -> IngestionResult:
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
    scan = _scan_files(
        root, book.include_globs, book.exclude_globs, budget=KNOWLEDGE_SCAN_BUDGET,
    )
    # 外部扫描实现或旧测试桩可能给出自相矛盾的结果；任何不可读条目都必须
    # 失败封闭，并进入统一截断账本。
    for unreadable in scan.unreadable_entries:
        if unreadable not in scan.truncations:
            scan.truncations.append(unreadable)
    if scan.unreadable_entries:
        scan.complete = False
    store.create_job(job_id, book_id, len(scan.files))

    # 检查已有文档
    existing_docs = {
        row["relative_path"]: row
        for row in store.list_documents(book_id)
    }
    current_paths = set(scan.seen_paths)
    processed = 0
    skipped = 0
    chunks_created = 0
    changed_chunk_ids: list[str] = []
    changed_document_ids: list[str] = []

    for file_path in scan.files:
        rel = str(file_path.relative_to(root)).replace("\\", "/")
        current_paths.add(rel)

        # 计算 content_hash（稳定读取：stat 前/后 + 只读一次）
        try:
            before = file_path.stat()
            content = file_path.read_bytes()
            after = file_path.stat()
            if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                skipped += 1
                continue  # 读取过程中文件被修改，本轮跳过
            content_hash = hashlib.sha256(content).hexdigest()[:16]
        except OSError:
            skipped += 1
            unreadable = ScanTruncation("unreadable_file", rel)
            scan.truncations.append(unreadable)
            scan.unreadable_entries.append(unreadable)
            scan.complete = False
            continue

        # 检查是否需要更新（内容哈希未变则跳过）
        existing = existing_docs.get(rel)
        if existing and existing["content_hash"] == content_hash:
            skipped += 1
            continue

        # P0-2 索引一致性：哈希与解析基于同一份字节缓冲
        ext = file_path.suffix.lower()
        media_type = (SUPPORTED_EXTENSIONS.get(ext) or CODE_EXTENSIONS.get(ext) or "text/plain")
        parsed = parse_content(content, relative_path=rel, media_type=media_type)
        if not parsed:
            skipped += 1
            continue

        # 控制面内容分类（指令文件只入库标记，不进入 KAG Bootstrap）
        content_role = "knowledge"
        if Path(rel).name in CONTROL_SURFACE_FILES:
            content_role = "control_surface"

        # P0-5 敏感内容检测：对同一份已读内容运行敏感模式，命中即标记 sensitive
        sensitivity = "sensitive" if detect_sensitive_content(parsed_text_of(parsed)) else "normal"

        # 切片
        document_id = _stable_hash(book_id, rel)
        chunks = chunk_document(parsed, book_id, document_id)

        # 原子替换：文档哈希与 Chunk 替换在同一事务完成
        store.replace_document_revision(
            document_id, book_id, rel, parsed.media_type,
            content_hash, chunks, content_role=content_role,
            sensitivity=sensitivity,
        )

        chunks_created += len(chunks)
        changed_document_ids.append(document_id)
        changed_chunk_ids.extend(c.chunk_id for c in chunks)
        processed += 1
        store.update_job(job_id, "running", phase="indexing", processed=processed)

    # 检测已删除的文件：仅当扫描完整时才推断"未扫到=已删除"（P0-1）
    # 扫描被预算/超时截断时，未扫到的旧文档必须保留，否则会误删半本书。
    deleted = 0
    if scan.complete:
        for rel, doc_row in existing_docs.items():
            if rel not in current_paths:
                store.deactivate_document(doc_row["document_id"])
                deleted += 1

    # 从数据库重新统计（不拿"本次处理了什么"冒充"书库目前有什么"）
    stats = _book_stats(store, book_id)

    # 书籍状态：扫描不完整 → partial，并记录截断原因
    if scan.complete:
        status = "ready"
    else:
        status = "partial"
    # P1-10 分阶段状态：lexical(切片) / organized(整理) / vector(向量)
    phases = dict(book.build_phases or {})
    phases["lexical"] = {
        "status": "ready" if scan.complete else "partial",
        "indexed": stats["chunks"],
        "processed": processed,
        "skipped": skipped,
        "deleted": deleted,
    }
    store.update_book_status(
        book_id, status,
        file_count=stats["files"],
        chapter_count=stats["chapters"],
        chunk_count=stats["chunks"],
        build_phases=phases,
    )

    # KB3 smart indexes are independently versioned from lexical ingestion.
    # A provider/model change rebuilds unchanged chunks; otherwise only changed
    # chunks are organized.
    try:
        from .knowledge_organizer import ORGANIZER_VERSION, organize_book
        from .knowledge_graph import build_structural_relations
        from . import provider_api

        enhance_provider = _authorized_provider(book.remote_embedding_allowed)
        _, cfg = provider_api.get_provider_state()
        remote_enhance = bool(
            enhance_provider and provider_api.is_remote_provider_config(cfg)
        )
        previous_organized = phases.get("organized", {})
        previous_organizer_id = (
            str(previous_organized.get("organizer_id", ""))
            if isinstance(previous_organized, dict)
            else ""
        )
        if enhance_provider is not None:
            descriptor = provider_api.describe_embedding_backend(
                enhance_provider,
                cfg,
            )
            organizer_id = (
                f"v{ORGANIZER_VERSION}:"
                f"{descriptor.provider_type}|{descriptor.endpoint_identity}|"
                f"{descriptor.model}"
            )
        else:
            organizer_id = previous_organizer_id or f"rule:v{ORGANIZER_VERSION}"
        full_smart_rebuild = organizer_id != previous_organizer_id
        organize_chunk_ids = None if full_smart_rebuild else changed_chunk_ids
        if changed_chunk_ids or full_smart_rebuild:
            organize_stats = organize_book(
                store, book_id, provider=enhance_provider, remote=remote_enhance,
                chunk_ids=organize_chunk_ids,
            )
            build_structural_relations(
                store,
                book_id,
                document_ids=None if full_smart_rebuild else changed_document_ids,
            )
        else:
            organize_stats = {
                "chunks_organized": 0,
                "model_calls": 0,
                "relations_created": 0,
                "budget_exhausted": 0,
            }
        phases["organized"] = {
            "status": (
                "partial" if organize_stats.get("budget_exhausted") else "ready"
            ),
            "processed": organize_stats.get("chunks_organized", 0),
            "failed": 0,
            "model_calls": organize_stats.get("model_calls", 0),
            "relations": organize_stats.get("relations_created", 0),
            "organizer_id": organizer_id,
            "full_rebuild": full_smart_rebuild,
        }
        if organize_stats.get("budget_exhausted"):
            status = "partial"
        store.update_book_status(book_id, status, build_phases=phases)
    except Exception:
        # Smart-index failure does not invalidate the lexical index.
        phases["organized"] = {
            "status": "failed",
            "processed": 0,
            "failed": len(changed_chunk_ids),
        }
        store.update_book_status(book_id, status, build_phases=phases)

    # KB2 向量索引：provider 可用且（本地或已授权远程）时为 chunk 生成 embedding
    if book.vector_enabled == "off":
        phases["vector"] = {"status": "disabled", "indexed": 0}
        store.update_book_status(book_id, status, build_phases=phases)
    else:
        try:
            embedded = generate_embeddings(
                store, book.book_id, book.remote_embedding_allowed,
            )
            from .provider_api import current_embedding_space_id
            embedding_space_id = current_embedding_space_id() or ""
            indexed = (
                store.count_embeddings(book.book_id, embedding_space_id)
                if embedding_space_id
                else 0
            )
            phases["vector"] = {
                "status": "ready" if indexed > 0 else "unavailable",
                "indexed": indexed,
                "generated": embedded,
                "embedding_space_id": embedding_space_id,
            }
        except Exception:
            # embedding 失败不影响入库（FTS 仍可用）
            phases["vector"] = {"status": "failed", "indexed": 0}
        store.update_book_status(book_id, status, build_phases=phases)

    store.update_job(job_id, "done", phase="complete", processed=processed)

    error = ""
    if scan.truncations:
        error = "扫描不完整：" + "；".join(t.reason for t in scan.truncations)

    return IngestionResult(
        book_id=book_id,
        files_total=len(scan.files),
        files_processed=processed,
        files_skipped=skipped,
        files_deleted=deleted,
        chunks_created=chunks_created,
        chapters=set(stats["chapter_list"]),
        error=error,
        status=status,
        truncations=scan.truncations,
    )


def generate_embeddings(store: KnowledgeStore, book_id: str,
                        remote_allowed: bool = False) -> int:
    """为缺少 embedding 的 chunk 批量生成向量（KB2）。

    返回生成的 embedding 数量。provider 不可用、或远程 provider 未授权时返回 0。
    可单独调用，便于 GUI "重新智能整理" 触发。
    """
    from . import provider_api

    backend, cfg = provider_api.get_provider_state()
    if backend is None:
        return 0

    # 远程 provider 必须先经用户对每本书显式授权，否则不回传文档内容
    is_remote = provider_api.is_remote_provider_config(cfg)
    if is_remote and not remote_allowed:
        return 0

    # 唯一向量空间 ID：入库与查询必须在同一空间（P0-3）
    from .provider_api import current_embedding_space_id, describe_embedding_backend
    space_id = current_embedding_space_id()
    if not space_id:
        return 0
    descriptor = describe_embedding_backend(backend, cfg)
    model_name = descriptor.embedding_model or descriptor.model
    embedding_space_id = descriptor.space_id

    # 列出需要 embedding 的 chunk（text_hash 变化则需重建）
    rows = store.list_chunks_without_embedding(book_id, model_name, embedding_space_id)
    if not rows:
        return 0

    # P0-5 隐私：远程 provider 永不接收敏感/控制面片段（即使整本书已授权远程）
    if is_remote:
        rows = _filter_out_sensitive(store, rows)

    chunk_ids = [r["chunk_id"] for r in rows]
    text_hashes = [r["text_hash"] for r in rows]
    if not chunk_ids:
        return 0

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


def rebuild_smart_indexes(
    store: KnowledgeStore,
    book_id: str,
) -> dict[str, object]:
    """Rebuild organizer, graph, and embeddings without reparsing source files."""
    with _book_lock(book_id):
        book = store.get_book(book_id)
        if not book:
            return {"ok": False, "book_id": book_id, "error": "book not found"}

        from . import provider_api
        from .knowledge_graph import build_structural_relations
        from .knowledge_organizer import ORGANIZER_VERSION, organize_book

        phases = dict(book.build_phases or {})
        enhance_provider = _authorized_provider(book.remote_embedding_allowed)
        _, cfg = provider_api.get_provider_state()
        remote_enhance = bool(
            enhance_provider and provider_api.is_remote_provider_config(cfg)
        )
        if enhance_provider is not None:
            descriptor = provider_api.describe_embedding_backend(
                enhance_provider,
                cfg,
            )
            organizer_id = (
                f"v{ORGANIZER_VERSION}:"
                f"{descriptor.provider_type}|{descriptor.endpoint_identity}|"
                f"{descriptor.model}"
            )
        else:
            organizer_id = f"rule:v{ORGANIZER_VERSION}"

        organize_stats = organize_book(
            store,
            book_id,
            provider=enhance_provider,
            remote=remote_enhance,
            chunk_ids=None,
        )
        relation_stats = build_structural_relations(
            store,
            book_id,
            document_ids=None,
        )
        phases["organized"] = {
            "status": (
                "partial" if organize_stats.get("budget_exhausted") else "ready"
            ),
            "processed": organize_stats.get("chunks_organized", 0),
            "failed": 0,
            "model_calls": organize_stats.get("model_calls", 0),
            "relations": (
                organize_stats.get("relations_created", 0)
                + relation_stats.get("relations_created", 0)
            ),
            "organizer_id": organizer_id,
            "full_rebuild": True,
        }

        if book.vector_enabled == "off":
            phases["vector"] = {"status": "disabled", "indexed": 0}
        else:
            embedding_provider = _authorized_provider(
                book.remote_embedding_allowed,
            )
            embedding_space_id = (
                provider_api.current_embedding_space_id()
                if embedding_provider is not None
                else ""
            ) or ""
            if embedding_space_id:
                with store._tx() as conn:
                    conn.execute(
                        "DELETE FROM embeddings WHERE embedding_space_id=? "
                        "AND chunk_id IN "
                        "(SELECT chunk_id FROM chunks WHERE book_id=?)",
                        (embedding_space_id, book_id),
                    )
            generated = generate_embeddings(
                store,
                book_id,
                book.remote_embedding_allowed,
            )
            indexed = (
                store.count_embeddings(book_id, embedding_space_id)
                if embedding_space_id
                else 0
            )
            phases["vector"] = {
                "status": "ready" if indexed > 0 else "unavailable",
                "indexed": indexed,
                "generated": generated,
                "embedding_space_id": embedding_space_id,
                "full_rebuild": True,
            }

        status = (
            "partial"
            if organize_stats.get("budget_exhausted")
            else book.status
        )
        store.update_book_status(book_id, status, build_phases=phases)
        return {
            "ok": True,
            "book_id": book_id,
            "status": status,
            "organized": phases["organized"],
            "vector": phases["vector"],
        }


def _scan_files(root: Path, include_globs: str, exclude_globs: str,
                budget: ScanBudget | None = None) -> KnowledgeScanResult:
    """扫描目录下所有支持的文件（带预算与安全边界）。

    返回 KnowledgeScanResult：含文件列表、是否完整、截断原因。
    复用 SourceRegistry 的默认排除与预算语义：文件数、总大小、单文件大小、
    深度、超时、符号链接逃逸、默认排除目录。
    """
    budget = budget or KNOWLEDGE_SCAN_BUDGET
    include_patterns = [g.strip() for g in include_globs.split(",") if g.strip()] if include_globs else []
    exclude_patterns = [g.strip() for g in exclude_globs.split(",") if g.strip()] if exclude_globs else []
    exclude_patterns = list(DEFAULT_PROJECT_EXCLUDE) + exclude_patterns

    all_supported = set(SUPPORTED_EXTENSIONS.keys()) | set(CODE_EXTENSIONS.keys())
    files: list[Path] = []
    truncations: list[ScanTruncation] = []
    unreadable_entries: list[ScanTruncation] = []
    seen_paths: set[str] = set()
    total_size = 0
    start = time.time()
    stop_scan = False

    # 解析并缓存真实根路径，用于符号链接逃逸检测
    real_root = root.resolve()

    def _depth(p: Path) -> int:
        return len(p.relative_to(root).parts)

    def _relative_path(path_value: str | os.PathLike[str] | None) -> str:
        """把 walk 错误路径转成稳定的书内相对路径。"""
        if not path_value:
            return "."
        candidate = Path(path_value)
        try:
            return str(candidate.relative_to(root)).replace("\\", "/") or "."
        except ValueError:
            try:
                return str(candidate.resolve().relative_to(real_root)).replace("\\", "/") or "."
            except (OSError, ValueError):
                return str(path_value).replace("\\", "/")

    def _record_unreadable(reason: str, path_value: str | os.PathLike[str] | None) -> None:
        entry = ScanTruncation(reason, _relative_path(path_value))
        truncations.append(entry)
        unreadable_entries.append(entry)

    def _on_walk_error(error: OSError) -> None:
        _record_unreadable("unreadable_directory", getattr(error, "filename", None))

    for dirpath, dirnames, filenames in os.walk(root, onerror=_on_walk_error):
        if time.time() - start > budget.timeout_seconds:
            truncations.append(ScanTruncation("timeout", "扫描超过预算时长，停止扫描"))
            break
        # 跳过符号链接目录（防逃逸）与排除目录
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".")
            and not _dir_excluded(dirpath, d, exclude_patterns)
        ]
        depth = _depth(Path(dirpath))
        if depth >= budget.max_depth and dirnames:
            relative_dir = str(
                Path(dirpath).relative_to(root),
            ).replace("\\", "/") or "."
            truncations.append(ScanTruncation("max_depth", relative_dir))
            dirnames[:] = []
        # 符号链接目录不进入（os.walk 默认不跟随 symlink dir，但显式防护）
        for fname in filenames:
            if fname.startswith("."):
                continue
            if len(files) >= budget.max_files:
                truncations.append(ScanTruncation(
                    "max_files", f"超过最大文件数 {budget.max_files}，停止扫描"))
                stop_scan = True
                break
            ext = Path(fname).suffix.lower()
            if ext not in all_supported:
                continue
            file_path = Path(dirpath) / fname
            if file_path.is_symlink():
                continue  # 符号链接文件不扫描
            rel = str(file_path.relative_to(root)).replace("\\", "/")

            # 应用 exclude
            if any(_match_glob(rel, p) for p in exclude_patterns):
                continue

            # 应用 include（如果指定了）
            if include_patterns and not any(_match_glob(rel, p) for p in include_patterns):
                continue

            # 先记录目录遍历中出现的候选路径，再尝试读取元数据；这样瞬时
            # stat/read 失败时仍不会把已有文档误判为已删除。
            seen_paths.add(rel)
            try:
                if not str(file_path.resolve()).startswith(str(real_root)):
                    continue  # 逃逸出根目录
                size = file_path.stat().st_size
            except OSError:
                _record_unreadable("unreadable_file", rel)
                continue
            if size > budget.max_single_file:
                continue  # 超单文件上限：跳过该文件，不视为整体截断
            if total_size + size > budget.max_total_size:
                truncations.append(ScanTruncation(
                    "max_total_size",
                    f"超过总大小上限 {budget.max_total_size}，停止扫描"))
                stop_scan = True
                break

            files.append(file_path)
            total_size += size
        if stop_scan:
            break

    return KnowledgeScanResult(
        files=sorted(files),
        complete=not truncations,
        truncations=truncations,
        scanned_count=len(files),
        total_size=total_size,
        seen_paths=seen_paths,
        unreadable_entries=unreadable_entries,
    )


def _dir_excluded(dirpath: str, dirname: str, exclude_patterns: list[str]) -> bool:
    """判断目录是否被排除（按相对路径 glob 匹配）。"""
    full = f"{dirname}/"
    for p in exclude_patterns:
        if p.endswith("/**") and p[:-3] == dirname:
            return True
    return bool(full and any(_match_glob(full, p) for p in exclude_patterns))


def detect_sensitive_content(text: str) -> bool:
    """检测文本是否含敏感内容（密钥/令牌/私钥等）。

    复用 auto_organizer.SECRET_PATTERNS。命中即标记 sensitive，用于：
    - 远程 provider 永不接收敏感片段（P0-5 隐私）；
    - Bootstrap 默认不注入敏感片段。
    """
    if not text:
        return False
    from .sensitive_content import contains_sensitive_content
    return contains_sensitive_content(text)


def parsed_text_of(parsed) -> str:
    """将解析后的文档块拼接为纯文本（用于敏感检测/摘要）。"""
    try:
        return "\n".join(b.text for b in parsed.blocks)
    except Exception:
        return ""


def _filter_out_sensitive(store: KnowledgeStore, rows: list) -> list:
    """从待 embedding 的 chunk 中剔除敏感/控制面片段（P0-5 隐私）。

    仅用于远程 provider 路径：敏感片段绝不上传远端，即使整本书已授权远程。
    """
    if not rows:
        return rows
    chunk_ids = [r["chunk_id"] for r in rows]
    placeholders = ",".join("?" * len(chunk_ids))
    metadata = {
        row["chunk_id"]: (row["sensitivity"], row["content_role"])
        for row in store._conn.execute(
            f"SELECT c.chunk_id, c.sensitivity, d.content_role "
            f"FROM chunks c JOIN documents d ON d.document_id=c.document_id "
            f"WHERE c.chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
    }
    policy = KnowledgeAccessPolicy()
    return [
        r for r in rows
        if policy.allows({
            "sensitivity": metadata.get(r["chunk_id"], ("normal", "knowledge"))[0],
            "content_role": metadata.get(r["chunk_id"], ("normal", "knowledge"))[1],
        })
    ]


def _is_local_base(api_base: str) -> bool:
    """判断 provider base_url 是否为本地（localhost/回环 IP）。

    用 urlparse 提取 hostname + ipaddress 判断回环，杜绝字符串包含被域名欺骗
    （如 `https://localhost.attacker.example` 被误判为本地）（P1-1）。
    """
    from .provider_api import is_local_provider_url
    return is_local_provider_url(api_base)


def _authorized_provider(remote_allowed: bool):
    """返回可用的 provider；远程 provider 未授权时返回 None（原本地 provider 直接可用）。

    既用于 KB2 embedding，也用于 P1-3 模型增强：远程 provider 必须经用户对每本书
    显式授权，否则不把文档内容发往远程。
    """
    from . import provider_api
    backend, cfg = provider_api.get_provider_state()
    if backend is None:
        return None
    if provider_api.is_remote_provider_config(cfg) and not remote_allowed:
        return None
    return backend


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
