"""knowledge_store：知识书库 SQLite 存储层（KB1）。

表结构遵循 PRD §5.1。FTS5 使用 unicode61 tokenizer 支持中文（每个 CJK 字符作为独立 token）。
所有写入操作线程安全（SQLite 单连接 + check_same_thread=False）。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from .data_home import ensure_dirs, knowledge_db_path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    book_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    root_path TEXT NOT NULL,
    cover_style TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'indexing',
    file_count INTEGER NOT NULL DEFAULT 0,
    chapter_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    entity_count INTEGER NOT NULL DEFAULT 0,
    last_indexed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    include_globs TEXT NOT NULL DEFAULT '',
    exclude_globs TEXT NOT NULL DEFAULT '',
    auto_extract_memory INTEGER NOT NULL DEFAULT 1,
    vector_enabled TEXT NOT NULL DEFAULT 'auto'
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'text/plain',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    updated_at TEXT NOT NULL,
    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_documents_book ON documents(book_id);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    book_id TEXT NOT NULL,
    chapter TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    ordinal INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    line_start INTEGER NOT NULL DEFAULT 0,
    line_end INTEGER NOT NULL DEFAULT 0,
    text_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(document_id) ON DELETE CASCADE,
    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_book ON chunks(book_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_ordinal ON chunks(document_id, ordinal);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'concept',
    description TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(normalized_name);

CREATE TABLE IF NOT EXISTS relations (
    relation_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL,
    source_chunk_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (subject_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE,
    FOREIGN KEY (object_entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_entity_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_entity_id);

CREATE TABLE IF NOT EXISTS chunk_entities (
    chunk_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'mention',
    PRIMARY KEY (chunk_id, entity_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunk_entities_entity ON chunk_entities(entity_id);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT PRIMARY KEY,
    embedding_model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS index_jobs (
    job_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    phase TEXT NOT NULL DEFAULT 'scanning',
    processed_files INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
);

-- FTS5 全文索引：trigram tokenizer 支持中文子串匹配（≥3 字符）。
-- 只索引 chapter/section/text 三列；chunk_id 和 book_title 通过 rowid JOIN 获取。
-- 用 DELETE FROM 代替 FTS5 'delete' 命令（后者在某些 SQLite 版本报 SQL logic error）。
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chapter,
    section,
    text,
    tokenize = 'trigram'
);

-- FTS5 rowid 必须用 chunks.rowid（隐藏整数 rowid），不能用 chunk_id（TEXT）。
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, chapter, section, text)
    VALUES (new.rowid, new.chapter, new.section, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_au AFTER UPDATE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.rowid;
    INSERT INTO chunks_fts(rowid, chapter, section, text)
    VALUES (new.rowid, new.chapter, new.section, new.text);
END;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Book:
    book_id: str
    title: str
    root_path: str
    cover_style: str = ""
    description: str = ""
    status: str = "indexing"
    file_count: int = 0
    chapter_count: int = 0
    chunk_count: int = 0
    entity_count: int = 0
    last_indexed_at: str | None = None
    include_globs: str = ""
    exclude_globs: str = ""
    auto_extract_memory: bool = True
    vector_enabled: str = "auto"


@dataclass
class Chunk:
    chunk_id: str
    document_id: str
    book_id: str
    chapter: str = ""
    section: str = ""
    ordinal: int = 0
    text: str = ""
    summary: str = ""
    keywords: str = ""
    line_start: int = 0
    line_end: int = 0
    text_hash: str = ""
    active: bool = True


class KnowledgeStore:
    """知识书库 SQLite 存储。线程安全单连接。"""

    def __init__(self, data_home: Path | None = None):
        self._data_home = data_home
        self._db_path = knowledge_db_path(data_home)
        ensure_dirs(data_home)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """事务上下文。"""
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self._conn.close()

    # ---- books ----

    def add_book(self, book: Book) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO books (book_id, title, root_path, cover_style, description, status,
                   file_count, chapter_count, chunk_count, entity_count, last_indexed_at,
                   created_at, updated_at, include_globs, exclude_globs, auto_extract_memory, vector_enabled)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (book.book_id, book.title, book.root_path, book.cover_style, book.description,
                 book.status, book.file_count, book.chapter_count, book.chunk_count, book.entity_count,
                 book.last_indexed_at, _now_iso(), _now_iso(), book.include_globs, book.exclude_globs,
                 int(book.auto_extract_memory), book.vector_enabled),
            )

    def get_book(self, book_id: str) -> Book | None:
        row = self._conn.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone()
        if not row:
            return None
        return _row_to_book(row)

    def list_books(self) -> list[Book]:
        rows = self._conn.execute("SELECT * FROM books ORDER BY updated_at DESC").fetchall()
        return [_row_to_book(r) for r in rows]

    def update_book_status(self, book_id: str, status: str, **counts: int) -> None:
        sets = ["status=?", "updated_at=?"]
        vals: list[Any] = [status, _now_iso()]
        for k in ("file_count", "chapter_count", "chunk_count", "entity_count", "last_indexed_at"):
            if k in counts:
                sets.append(f"{k}=?")
                vals.append(counts[k] if k != "last_indexed_at" else _now_iso())
        vals.append(book_id)
        with self._tx() as conn:
            conn.execute(f"UPDATE books SET {', '.join(sets)} WHERE book_id=?", vals)

    def remove_book(self, book_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM books WHERE book_id=?", (book_id,))

    # ---- documents ----

    def upsert_document(self, document_id: str, book_id: str, relative_path: str,
                        media_type: str, content_hash: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO documents (document_id, book_id, relative_path, media_type, content_hash, status, updated_at)
                   VALUES (?,?,?,?,?,'active',?)
                   ON CONFLICT(document_id) DO UPDATE SET content_hash=excluded.content_hash, updated_at=excluded.updated_at, status='active'""",
                (document_id, book_id, relative_path, media_type, content_hash, _now_iso()),
            )

    def get_document_by_path(self, book_id: str, relative_path: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM documents WHERE book_id=? AND relative_path=?",
            (book_id, relative_path),
        ).fetchone()

    def list_documents(self, book_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM documents WHERE book_id=? AND status='active' ORDER BY relative_path",
            (book_id,),
        ).fetchall()

    def deactivate_document(self, document_id: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE documents SET status='deleted', updated_at=? WHERE document_id=?",
                         (_now_iso(), document_id))
            conn.execute("UPDATE chunks SET active=0 WHERE document_id=?", (document_id,))

    # ---- chunks ----

    def add_chunk(self, chunk: Chunk) -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO chunks (chunk_id, document_id, book_id, chapter, section, ordinal,
                   text, summary, keywords, line_start, line_end, text_hash, active, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (chunk.chunk_id, chunk.document_id, chunk.book_id, chunk.chapter, chunk.section,
                 chunk.ordinal, chunk.text, chunk.summary, chunk.keywords,
                 chunk.line_start, chunk.line_end, chunk.text_hash, _now_iso()),
            )

    def replace_document_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """原子替换一个文档的所有 chunk。旧 chunk 先停用再插入新的。"""
        with self._tx() as conn:
            conn.execute("UPDATE chunks SET active=0 WHERE document_id=?", (document_id,))
            for c in chunks:
                conn.execute(
                    """INSERT INTO chunks (chunk_id, document_id, book_id, chapter, section, ordinal,
                       text, summary, keywords, line_start, line_end, text_hash, active, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
                     c.text, c.summary, c.keywords, c.line_start, c.line_end, c.text_hash, _now_iso()),
                )

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        row = self._conn.execute("SELECT * FROM chunks WHERE chunk_id=?", (chunk_id,)).fetchone()
        if not row:
            return None
        return _row_to_chunk(row)

    def get_adjacent_chunks(self, chunk_id: str) -> tuple[Chunk | None, Chunk | None]:
        """返回 (前一个, 后一个)相邻 chunk。"""
        chunk = self.get_chunk(chunk_id)
        if not chunk:
            return None, None
        prev_row = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id=? AND ordinal<? AND active=1 ORDER BY ordinal DESC LIMIT 1",
            (chunk.document_id, chunk.ordinal),
        ).fetchone()
        next_row = self._conn.execute(
            "SELECT * FROM chunks WHERE document_id=? AND ordinal>? AND active=1 ORDER BY ordinal ASC LIMIT 1",
            (chunk.document_id, chunk.ordinal),
        ).fetchone()
        return (_row_to_chunk(prev_row) if prev_row else None, _row_to_chunk(next_row) if next_row else None)

    def count_chunks(self, book_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE book_id=? AND active=1", (book_id,)
        ).fetchone()
        return row[0] if row else 0

    # ---- FTS5 检索 ----

    def search_fts(self, query: str, book_ids: list[str] | None = None,
                   limit: int = 30) -> list[dict[str, Any]]:
        """FTS5 全文检索。返回 chunk + book_title。"""
        # FTS5 MATCH 查询需要转义特殊字符
        safe_query = _sanitize_fts_query(query)
        if not safe_query:
            return []
        sql = """
            SELECT c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
                   c.text, c.summary, c.keywords, c.line_start, c.line_end,
                   b.title AS book_title, b.root_path,
                   d.relative_path AS relative_path,
                   rank FROM chunks_fts f
            JOIN chunks c ON c.rowid = f.rowid
            JOIN books b ON b.book_id = c.book_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ? AND c.active=1
        """
        params: list[Any] = [safe_query]
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND c.book_id IN ({placeholders})"
            params.extend(book_ids)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_like(self, query: str, book_ids: list[str] | None = None,
                    limit: int = 30) -> list[dict[str, Any]]:
        """LIKE 子串检索 fallback。

        trigram tokenizer 要求查询 ≥3 字符；短查询（如中文 2 字"属性""伤害"）
        走 LIKE 保证召回。
        """
        q = (query or "").strip()
        if not q:
            return []
        pattern = f"%{q}%"
        sql = """
            SELECT c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
                   c.text, c.summary, c.keywords, c.line_start, c.line_end,
                   b.title AS book_title, b.root_path,
                   d.relative_path AS relative_path,
                   0.0 AS rank
            FROM chunks c
            JOIN books b ON b.book_id = c.book_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.active=1 AND (c.text LIKE ? OR c.chapter LIKE ? OR c.section LIKE ?)
        """
        params: list[Any] = [pattern, pattern, pattern]
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND c.book_id IN ({placeholders})"
            params.extend(book_ids)
        sql += " ORDER BY c.book_id, c.document_id, c.ordinal LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---- index_jobs ----

    def create_job(self, job_id: str, book_id: str, total_files: int = 0) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO index_jobs (job_id, book_id, status, phase, total_files, started_at) "
                "VALUES (?,?, 'running', 'scanning', ?, ?)",
                (job_id, book_id, total_files, _now_iso()),
            )

    def update_job(self, job_id: str, status: str, phase: str = "",
                   processed: int | None = None, error: str = "") -> None:
        sets = ["status=?"]
        vals: list[Any] = [status]
        if phase:
            sets.append("phase=?")
            vals.append(phase)
        if processed is not None:
            sets.append("processed_files=?")
            vals.append(processed)
        if error:
            sets.append("error=?")
            vals.append(error)
        if status in ("done", "failed"):
            sets.append("finished_at=?")
            vals.append(_now_iso())
        vals.append(job_id)
        with self._tx() as conn:
            conn.execute(f"UPDATE index_jobs SET {', '.join(sets)} WHERE job_id=?", vals)

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM index_jobs WHERE job_id=?", (job_id,)).fetchone()


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        book_id=row["book_id"], title=row["title"], root_path=row["root_path"],
        cover_style=row["cover_style"], description=row["description"], status=row["status"],
        file_count=row["file_count"], chapter_count=row["chapter_count"],
        chunk_count=row["chunk_count"], entity_count=row["entity_count"],
        last_indexed_at=row["last_indexed_at"], include_globs=row["include_globs"],
        exclude_globs=row["exclude_globs"], auto_extract_memory=bool(row["auto_extract_memory"]),
        vector_enabled=row["vector_enabled"],
    )


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"], document_id=row["document_id"], book_id=row["book_id"],
        chapter=row["chapter"], section=row["section"], ordinal=row["ordinal"],
        text=row["text"], summary=row["summary"], keywords=row["keywords"],
        line_start=row["line_start"], line_end=row["line_end"],
        text_hash=row["text_hash"], active=bool(row["active"]),
    )


def _sanitize_fts_query(query: str) -> str:
    """将用户查询转为 FTS5 安全 MATCH 表达式。

    trigram tokenizer 支持 3 字符以上的子串匹配。
    短查询（<3 字符）用前缀匹配；长查询直接引用。
    """
    query = query.strip()
    if not query:
        return ""
    # 按空格拆分，每个部分加引号
    parts = [p.strip() for p in query.split() if p.strip()]
    if not parts:
        return ""
    tokens: list[str] = []
    for part in parts:
        if len(part) >= 3:
            tokens.append(f'"{part}"')
        else:
            tokens.append(f'"{part}"*')
    return " OR ".join(tokens)
