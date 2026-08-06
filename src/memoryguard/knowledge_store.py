"""knowledge_store：知识书库 SQLite 存储层（KB1）。

表结构遵循 PRD §5.1。FTS5 使用 unicode61 tokenizer 支持中文（每个 CJK 字符作为独立 token）。
所有写入操作线程安全（SQLite 单连接 + check_same_thread=False）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator

from .data_home import ensure_dirs, knowledge_db_path, resolve_data_home
from .knowledge_policy import KnowledgeAccessPolicy, policy_sql

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
    vector_enabled TEXT NOT NULL DEFAULT 'auto',
    remote_embedding_allowed INTEGER NOT NULL DEFAULT 0,
    remote_query_embedding_allowed INTEGER NOT NULL DEFAULT 0,
    build_phases TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL DEFAULT 'text/plain',
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    content_role TEXT NOT NULL DEFAULT 'knowledge',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
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
    sensitivity TEXT NOT NULL DEFAULT 'normal',
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
    book_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    relation_source TEXT NOT NULL DEFAULT 'structural',
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
    chunk_id TEXT NOT NULL,
    embedding_space_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, embedding_space_id),
    FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_candidates (
    candidate_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    chunk_id TEXT,
    document_id TEXT NOT NULL DEFAULT '',
    source_text_hash TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'knowledge',
    kind TEXT NOT NULL DEFAULT 'fact',
    confidence REAL NOT NULL DEFAULT 0.5,
    status TEXT NOT NULL DEFAULT 'pending',
    sync_error TEXT NOT NULL DEFAULT '',
    synced_memory_id TEXT NOT NULL DEFAULT '',
    target_group_id TEXT NOT NULL DEFAULT '',
    sync_started_at TEXT NOT NULL DEFAULT '',
    sync_attempt_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mc_status ON memory_candidates(status);

CREATE TABLE IF NOT EXISTS deleted_books (
    deletion_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    title TEXT NOT NULL,
    root_path TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'deleted',
    deleted_at TEXT NOT NULL,
    restored_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_deleted_books_status
    ON deleted_books(status, deleted_at);

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


def _ensure_schema_compat(conn: sqlite3.Connection) -> None:
    """为已存在的旧 knowledge.db 补齐新增列（幂等）。"""
    for table, column, ddl in (
        ("documents", "content_role",
         "ALTER TABLE documents ADD COLUMN content_role TEXT NOT NULL DEFAULT 'knowledge'"),
        ("documents", "sensitivity",
         "ALTER TABLE documents ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'normal'"),
        ("chunks", "sensitivity",
         "ALTER TABLE chunks ADD COLUMN sensitivity TEXT NOT NULL DEFAULT 'normal'"),
        ("books", "remote_embedding_allowed",
         "ALTER TABLE books ADD COLUMN remote_embedding_allowed INTEGER NOT NULL DEFAULT 0"),
        ("books", "remote_query_embedding_allowed",
         "ALTER TABLE books ADD COLUMN remote_query_embedding_allowed INTEGER NOT NULL DEFAULT 0"),
        ("books", "build_phases",
         "ALTER TABLE books ADD COLUMN build_phases TEXT NOT NULL DEFAULT '{}'"),
        ("relations", "book_id",
         "ALTER TABLE relations ADD COLUMN book_id TEXT NOT NULL DEFAULT ''"),
        ("relations", "document_id",
         "ALTER TABLE relations ADD COLUMN document_id TEXT NOT NULL DEFAULT ''"),
        ("relations", "relation_source",
         "ALTER TABLE relations ADD COLUMN relation_source TEXT NOT NULL DEFAULT 'structural'"),
        ("memory_candidates", "kind",
         "ALTER TABLE memory_candidates ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'"),
        ("memory_candidates", "sync_error",
         "ALTER TABLE memory_candidates ADD COLUMN sync_error TEXT NOT NULL DEFAULT ''"),
        ("memory_candidates", "target_group_id",
         "ALTER TABLE memory_candidates ADD COLUMN target_group_id TEXT NOT NULL DEFAULT ''"),
        ("memory_candidates", "sync_started_at",
         "ALTER TABLE memory_candidates ADD COLUMN sync_started_at TEXT NOT NULL DEFAULT ''"),
        ("memory_candidates", "sync_attempt_id",
         "ALTER TABLE memory_candidates ADD COLUMN sync_attempt_id TEXT NOT NULL DEFAULT ''"),
    ):
        try:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if column not in existing:
            try:
                conn.execute(ddl)
            except sqlite3.Error:
                pass

    # embeddings 表结构升级：旧表以 chunk_id 为主键、无 embedding_space_id。
    # 无法靠 ALTER 改变主键，直接重建（embedding 可重新生成，丢失可接受）。
    try:
        emb_cols = {row["name"] for row in conn.execute("PRAGMA table_info(embeddings)")}
    except sqlite3.Error:
        return
    if "embedding_space_id" not in emb_cols:
        try:
            conn.execute("DROP TABLE IF EXISTS embeddings")
            conn.execute("""CREATE TABLE embeddings (
                chunk_id TEXT NOT NULL,
                embedding_space_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                text_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chunk_id, embedding_space_id),
                FOREIGN KEY (chunk_id) REFERENCES chunks(chunk_id) ON DELETE CASCADE
            )""")
        except sqlite3.Error:
            pass

    # memory_candidates 表：旧库首次升级时创建
    try:
        mc_existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_candidates'"
        ).fetchone()
        if not mc_existing:
            conn.execute("""CREATE TABLE memory_candidates (
                candidate_id TEXT PRIMARY KEY,
                book_id TEXT NOT NULL,
                chunk_id TEXT,
                document_id TEXT NOT NULL DEFAULT '',
                source_text_hash TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'knowledge',
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending',
                sync_error TEXT NOT NULL DEFAULT '',
                synced_memory_id TEXT NOT NULL DEFAULT '',
                target_group_id TEXT NOT NULL DEFAULT '',
                sync_started_at TEXT NOT NULL DEFAULT '',
                sync_attempt_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mc_status ON memory_candidates(status)")
    except sqlite3.Error:
        pass

    # 旧 memory_candidates 表补齐 provenance 列（P1-4）
    try:
        mc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_candidates)")}
    except sqlite3.Error:
        mc_cols = set()
    for col, ddl in (
        ("document_id", "ALTER TABLE memory_candidates ADD COLUMN document_id TEXT NOT NULL DEFAULT ''"),
        ("source_text_hash", "ALTER TABLE memory_candidates ADD COLUMN source_text_hash TEXT NOT NULL DEFAULT ''"),
        ("synced_memory_id", "ALTER TABLE memory_candidates ADD COLUMN synced_memory_id TEXT NOT NULL DEFAULT ''"),
        ("kind", "ALTER TABLE memory_candidates ADD COLUMN kind TEXT NOT NULL DEFAULT 'fact'"),
        ("sync_error", "ALTER TABLE memory_candidates ADD COLUMN sync_error TEXT NOT NULL DEFAULT ''"),
        ("target_group_id", "ALTER TABLE memory_candidates ADD COLUMN target_group_id TEXT NOT NULL DEFAULT ''"),
        ("sync_started_at", "ALTER TABLE memory_candidates ADD COLUMN sync_started_at TEXT NOT NULL DEFAULT ''"),
        ("sync_attempt_id", "ALTER TABLE memory_candidates ADD COLUMN sync_attempt_id TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in mc_cols:
            try:
                conn.execute(ddl)
            except sqlite3.Error:
                pass

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS deleted_books (
            deletion_id TEXT PRIMARY KEY,
            book_id TEXT NOT NULL,
            title TEXT NOT NULL,
            root_path TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'deleted',
            deleted_at TEXT NOT NULL,
            restored_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_deleted_books_status
            ON deleted_books(status, deleted_at);
        CREATE INDEX IF NOT EXISTS idx_relations_book ON relations(book_id);
        CREATE INDEX IF NOT EXISTS idx_relations_document ON relations(document_id);
        """
    )
    _migrate_relation_scope(conn)


def _migrate_relation_scope(conn: sqlite3.Connection) -> None:
    """Backfill scoped legacy relations and remove rows that cannot be scoped."""
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            UPDATE relations
               SET book_id = COALESCE(
                       NULLIF(book_id, ''),
                       (SELECT c.book_id FROM chunks c
                         WHERE c.chunk_id=relations.source_chunk_id)
                   ),
                   document_id = COALESCE(
                       NULLIF(document_id, ''),
                       (SELECT c.document_id FROM chunks c
                         WHERE c.chunk_id=relations.source_chunk_id)
                   )
             WHERE (book_id='' OR document_id='')
               AND source_chunk_id IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM chunks c
                    WHERE c.chunk_id=relations.source_chunk_id
               )
            """
        )
        conn.execute(
            "DELETE FROM relations WHERE book_id='' OR document_id=''"
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def _encode_snapshot_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "__memoryguard_type__": "bytes",
            "base64": base64.b64encode(value).decode("ascii"),
        }
    return value


def _decode_snapshot_value(value: Any) -> Any:
    if (
        isinstance(value, dict)
        and value.get("__memoryguard_type__") == "bytes"
    ):
        return base64.b64decode(str(value.get("base64", "")))
    return value


def _snapshot_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: list[Any] | tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [
        {key: _encode_snapshot_value(row[key]) for key in row.keys()}
        for row in conn.execute(sql, params).fetchall()
    ]


def _capture_book_snapshot(
    conn: sqlite3.Connection,
    book_id: str,
) -> dict[str, Any]:
    documents = _snapshot_rows(
        conn, "SELECT * FROM documents WHERE book_id=?", (book_id,),
    )
    document_ids = [row["document_id"] for row in documents]
    chunks = _snapshot_rows(
        conn, "SELECT * FROM chunks WHERE book_id=?", (book_id,),
    )
    chunk_ids = [row["chunk_id"] for row in chunks]

    relations_sql = "SELECT * FROM relations WHERE book_id=?"
    relation_params: list[Any] = [book_id]
    if document_ids:
        placeholders = ",".join("?" * len(document_ids))
        relations_sql += f" OR document_id IN ({placeholders})"
        relation_params.extend(document_ids)
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        relations_sql += f" OR source_chunk_id IN ({placeholders})"
        relation_params.extend(chunk_ids)
    relations = _snapshot_rows(conn, relations_sql, relation_params)

    chunk_entities: list[dict[str, Any]] = []
    embeddings: list[dict[str, Any]] = []
    if chunk_ids:
        placeholders = ",".join("?" * len(chunk_ids))
        chunk_entities = _snapshot_rows(
            conn,
            f"SELECT * FROM chunk_entities WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        embeddings = _snapshot_rows(
            conn,
            f"SELECT * FROM embeddings WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )

    entity_ids = {
        row["entity_id"] for row in chunk_entities
    }
    for relation in relations:
        entity_ids.add(relation["subject_entity_id"])
        entity_ids.add(relation["object_entity_id"])
    entities: list[dict[str, Any]] = []
    if entity_ids:
        ordered_ids = sorted(entity_ids)
        placeholders = ",".join("?" * len(ordered_ids))
        entities = _snapshot_rows(
            conn,
            f"SELECT * FROM entities WHERE entity_id IN ({placeholders})",
            ordered_ids,
        )

    return {
        "snapshot_version": 1,
        "book_id": book_id,
        "created_at": _now_iso(),
        "tables": {
            "books": _snapshot_rows(
                conn, "SELECT * FROM books WHERE book_id=?", (book_id,),
            ),
            "documents": documents,
            "chunks": chunks,
            "entities": entities,
            "chunk_entities": chunk_entities,
            "embeddings": embeddings,
            "relations": relations,
            "memory_candidates": _snapshot_rows(
                conn,
                "SELECT * FROM memory_candidates WHERE book_id=?",
                (book_id,),
            ),
            "index_jobs": _snapshot_rows(
                conn,
                "SELECT * FROM index_jobs WHERE book_id=?",
                (book_id,),
            ),
        },
    }


def _insert_snapshot_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict[str, Any]],
    *,
    ignore_conflicts: bool = False,
) -> None:
    verb = "INSERT OR IGNORE" if ignore_conflicts else "INSERT"
    for encoded in rows:
        row = {
            key: _decode_snapshot_value(value)
            for key, value in encoded.items()
        }
        columns = list(row)
        placeholders = ",".join("?" * len(columns))
        conn.execute(
            f"{verb} INTO {table} ({','.join(columns)}) "
            f"VALUES ({placeholders})",
            [row[column] for column in columns],
        )


def _restore_book_snapshot(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
) -> None:
    if int(snapshot.get("snapshot_version", 0)) != 1:
        raise ValueError("unsupported deleted book snapshot")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict) or not tables.get("books"):
        raise ValueError("invalid deleted book snapshot")
    _insert_snapshot_rows(conn, "books", tables["books"])
    _insert_snapshot_rows(conn, "documents", tables.get("documents", []))
    _insert_snapshot_rows(conn, "chunks", tables.get("chunks", []))
    _insert_snapshot_rows(
        conn, "entities", tables.get("entities", []), ignore_conflicts=True,
    )
    _insert_snapshot_rows(
        conn, "chunk_entities", tables.get("chunk_entities", []),
        ignore_conflicts=True,
    )
    _insert_snapshot_rows(
        conn, "embeddings", tables.get("embeddings", []),
        ignore_conflicts=True,
    )
    _insert_snapshot_rows(
        conn, "relations", tables.get("relations", []),
        ignore_conflicts=True,
    )
    _insert_snapshot_rows(
        conn, "memory_candidates", tables.get("memory_candidates", []),
        ignore_conflicts=True,
    )
    _insert_snapshot_rows(
        conn, "index_jobs", tables.get("index_jobs", []),
        ignore_conflicts=True,
    )


def _delete_orphan_entities(
    conn: sqlite3.Connection,
    entity_ids: list[str],
) -> None:
    if not entity_ids:
        return
    placeholders = ",".join("?" * len(entity_ids))
    conn.execute(
        f"""
        DELETE FROM entities
         WHERE entity_id IN ({placeholders})
           AND NOT EXISTS (
               SELECT 1 FROM chunk_entities ce
                WHERE ce.entity_id=entities.entity_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM relations r
                WHERE r.subject_entity_id=entities.entity_id
                   OR r.object_entity_id=entities.entity_id
           )
        """,
        entity_ids,
    )


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
    remote_embedding_allowed: bool = False
    remote_query_embedding_allowed: bool = False
    build_phases: dict[str, Any] = field(default_factory=dict)


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
    sensitivity: str = "normal"
    active: bool = True


class KnowledgeStore:
    """知识书库 SQLite 存储。线程安全单连接。"""

    def __init__(self, data_home: Path | None = None, read_only: bool = False):
        self._data_home = data_home
        self._db_path = knowledge_db_path(data_home)
        self._read_only = bool(read_only)
        self._closed = False
        self._lock = threading.Lock()
        if read_only:
            # 只读：不创建目录、不执行 schema、不设 WAL。数据库不存在时抛错，
            # 由调用方（open_shared_knowledge_store(must_exist=True)）提前判空。
            uri = f"file:{self._db_path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(
                uri, uri=True,
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
        else:
            ensure_dirs(data_home)
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,  # autocommit
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            _ensure_schema_compat(self._conn)

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
        if not self._closed:
            self._closed = True
            self._conn.close()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- books ----

    def add_book(self, book: Book) -> None:
        import json
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO books (book_id, title, root_path, cover_style, description, status,
                   file_count, chapter_count, chunk_count, entity_count, last_indexed_at,
                   created_at, updated_at, include_globs, exclude_globs, auto_extract_memory, vector_enabled,
                   remote_embedding_allowed, remote_query_embedding_allowed, build_phases)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (book.book_id, book.title, book.root_path, book.cover_style, book.description,
                 book.status, book.file_count, book.chapter_count, book.chunk_count, book.entity_count,
                 book.last_indexed_at, _now_iso(), _now_iso(), book.include_globs, book.exclude_globs,
                 int(book.auto_extract_memory), book.vector_enabled, int(book.remote_embedding_allowed),
                 int(book.remote_query_embedding_allowed),
                 json.dumps(book.build_phases, ensure_ascii=False)),
            )

    def get_book(self, book_id: str) -> Book | None:
        row = self._conn.execute("SELECT * FROM books WHERE book_id=?", (book_id,)).fetchone()
        if not row:
            return None
        return _row_to_book(row)

    def list_books(self) -> list[Book]:
        rows = self._conn.execute("SELECT * FROM books ORDER BY updated_at DESC").fetchall()
        return [_row_to_book(r) for r in rows]

    def update_book_status(self, book_id: str, status: str, **counts: Any) -> None:
        import json
        sets = ["status=?", "updated_at=?"]
        vals: list[Any] = [status, _now_iso()]
        for k in ("file_count", "chapter_count", "chunk_count", "entity_count", "last_indexed_at"):
            if k in counts:
                sets.append(f"{k}=?")
                vals.append(counts[k] if k != "last_indexed_at" else _now_iso())
        if "build_phases" in counts and isinstance(counts["build_phases"], dict):
            sets.append("build_phases=?")
            vals.append(json.dumps(counts["build_phases"], ensure_ascii=False))
        vals.append(book_id)
        with self._tx() as conn:
            conn.execute(f"UPDATE books SET {', '.join(sets)} WHERE book_id=?", vals)

    def update_book_settings(
        self,
        book_id: str,
        *,
        remote_embedding_allowed: bool | None = None,
        remote_query_embedding_allowed: bool | None = None,
        auto_extract_memory: bool | None = None,
        vector_enabled: str | None = None,
    ) -> bool:
        if remote_embedding_allowed is False:
            remote_query_embedding_allowed = False
        sets = ["updated_at=?"]
        values: list[Any] = [_now_iso()]
        if remote_embedding_allowed is not None:
            sets.append("remote_embedding_allowed=?")
            values.append(int(remote_embedding_allowed))
        if remote_query_embedding_allowed is not None:
            sets.append("remote_query_embedding_allowed=?")
            values.append(int(remote_query_embedding_allowed))
        if auto_extract_memory is not None:
            sets.append("auto_extract_memory=?")
            values.append(int(auto_extract_memory))
        if vector_enabled is not None:
            if vector_enabled not in {"auto", "on", "off"}:
                raise ValueError("invalid vector_enabled")
            sets.append("vector_enabled=?")
            values.append(vector_enabled)
        values.append(book_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE books SET {', '.join(sets)} WHERE book_id=?",
                values,
            )
            return cur.rowcount == 1

    def remove_book(self, book_id: str) -> dict[str, Any]:
        """Move a book into recoverable trash and remove all derived rows."""
        with self._tx() as conn:
            book = conn.execute(
                "SELECT * FROM books WHERE book_id=?",
                (book_id,),
            ).fetchone()
            if not book:
                raise ValueError("book not found")
            if conn.execute(
                "SELECT 1 FROM index_jobs WHERE book_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (book_id,),
            ).fetchone():
                raise ValueError("book has an active index job")
            if conn.execute(
                "SELECT 1 FROM memory_candidates WHERE book_id=? "
                "AND status='syncing' LIMIT 1",
                (book_id,),
            ).fetchone():
                raise ValueError("book has a candidate sync in progress")
            snapshot = _capture_book_snapshot(conn, book_id)
            deletion_id = uuid.uuid4().hex
            deleted_at = _now_iso()
            conn.execute(
                """INSERT INTO deleted_books
                   (deletion_id, book_id, title, root_path, snapshot_json,
                    status, deleted_at, restored_at)
                   VALUES (?,?,?,?,?,'deleted',?,'')""",
                (
                    deletion_id,
                    book_id,
                    book["title"],
                    book["root_path"],
                    json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                    deleted_at,
                ),
            )
            relation_ids = [
                row["relation_id"] for row in snapshot["tables"]["relations"]
            ]
            if relation_ids:
                placeholders = ",".join("?" * len(relation_ids))
                conn.execute(
                    f"DELETE FROM relations WHERE relation_id IN ({placeholders})",
                    relation_ids,
                )
            conn.execute("DELETE FROM books WHERE book_id=?", (book_id,))
            entity_ids = [
                row["entity_id"] for row in snapshot["tables"]["entities"]
            ]
            _delete_orphan_entities(conn, entity_ids)
            counts = {
                table: len(rows)
                for table, rows in snapshot["tables"].items()
                if table != "books"
            }
            return {
                "ok": True,
                "deletion_id": deletion_id,
                "book_id": book_id,
                "title": book["title"],
                "deleted_at": deleted_at,
                "cleanup_counts": counts,
                "source_files_removed": False,
            }

    def list_deleted_books(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT deletion_id, book_id, title, root_path, status,
                      deleted_at, restored_at
                 FROM deleted_books
                WHERE status='deleted'
                ORDER BY deleted_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def restore_book(self, deletion_id: str) -> dict[str, Any]:
        with self._tx() as conn:
            deleted = conn.execute(
                "SELECT * FROM deleted_books WHERE deletion_id=? AND status='deleted'",
                (deletion_id,),
            ).fetchone()
            if not deleted:
                return {
                    "ok": False,
                    "deletion_id": deletion_id,
                    "error": "deleted book not found",
                }
            if conn.execute(
                "SELECT 1 FROM books WHERE book_id=?",
                (deleted["book_id"],),
            ).fetchone():
                return {
                    "ok": False,
                    "deletion_id": deletion_id,
                    "book_id": deleted["book_id"],
                    "error": "book already exists",
                }
            snapshot = json.loads(deleted["snapshot_json"])
            _restore_book_snapshot(conn, snapshot)
            restored_at = _now_iso()
            conn.execute(
                "UPDATE deleted_books SET status='restored', restored_at=?, "
                "snapshot_json='' "
                "WHERE deletion_id=? AND status='deleted'",
                (restored_at, deletion_id),
            )
            return {
                "ok": True,
                "deletion_id": deletion_id,
                "book_id": deleted["book_id"],
                "title": deleted["title"],
                "restored_at": restored_at,
            }

    def purge_deleted_book(self, deletion_id: str) -> bool:
        """Permanently delete a recovery snapshot; source files stay untouched."""
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM deleted_books WHERE deletion_id=? AND status='deleted'",
                (deletion_id,),
            )
            return cur.rowcount == 1

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

    def replace_document_revision(
        self,
        document_id: str,
        book_id: str,
        relative_path: str,
        media_type: str,
        content_hash: str,
        chunks: list[Chunk],
        content_role: str = "knowledge",
        sensitivity: str = "normal",
    ) -> None:
        """原子提交一个文档的新整版：内容哈希与 Chunk 替换在同一事务完成。

        避免文档哈希先提交、Chunk 替换后失败导致"哈希已更新但旧 Chunk 永不重建"。
        先 DELETE 旧 chunk（而非停用），再插入新 chunk，最后 upsert 文档行。
        sensitivity 标记该文档片段是否含敏感内容（P0-5 隐私）。
        """
        with self._tx() as conn:
            new_hashes = sorted({c.text_hash for c in chunks if c.text_hash})
            if new_hashes:
                placeholders = ",".join("?" * len(new_hashes))
                conn.execute(
                    f"UPDATE memory_candidates SET status='stale', sync_error='source changed' "
                    f"WHERE document_id=? AND status IN ('pending','sync_failed') "
                    f"AND source_text_hash NOT IN ({placeholders})",
                    [document_id, *new_hashes],
                )
            else:
                conn.execute(
                    "UPDATE memory_candidates SET status='stale', sync_error='source removed' "
                    "WHERE document_id=? AND status IN ('pending','sync_failed')",
                    (document_id,),
                )
            # 先确保文档行存在（FK），再替换 chunks；整个事务一次性提交
            conn.execute(
                """INSERT INTO documents (document_id, book_id, relative_path, media_type, content_hash, status,
                                          content_role, sensitivity, updated_at)
                   VALUES (?,?,?,?,?,'active',?,?,?)
                   ON CONFLICT(document_id) DO UPDATE SET
                       content_hash=excluded.content_hash,
                       status='active',
                       content_role=excluded.content_role,
                       sensitivity=excluded.sensitivity,
                       updated_at=excluded.updated_at""",
                (document_id, book_id, relative_path, media_type, content_hash,
                 content_role, sensitivity, _now_iso()),
            )
            # Relation sources are not a foreign key because historical rows
            # may outlive a replaced chunk. Remove them before the chunk swap.
            conn.execute(
                "DELETE FROM relations WHERE document_id=? OR source_chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE document_id=?)",
                (document_id, document_id),
            )
            conn.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            for c in chunks:
                conn.execute(
                    """INSERT INTO chunks (chunk_id, document_id, book_id, chapter, section, ordinal,
                       text, summary, keywords, line_start, line_end, text_hash, sensitivity, active, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
                     c.text, c.summary, c.keywords, c.line_start, c.line_end,
                     c.text_hash, sensitivity, _now_iso()),
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
                   limit: int = 30,
                   policy: KnowledgeAccessPolicy | None = None) -> list[dict[str, Any]]:
        """FTS5 全文检索。返回 chunk + book_title。"""
        # FTS5 MATCH 查询需要转义特殊字符
        safe_query = _sanitize_fts_query(query)
        if not safe_query:
            return []
        sql = """
            SELECT c.chunk_id, c.document_id, c.book_id, c.chapter, c.section, c.ordinal,
                   c.text, c.summary, c.keywords, c.line_start, c.line_end,
                   c.sensitivity AS sensitivity,
                   b.title AS book_title, b.root_path,
                   d.relative_path AS relative_path, d.content_role AS content_role,
                   rank FROM chunks_fts f
            JOIN chunks c ON c.rowid = f.rowid
            JOIN books b ON b.book_id = c.book_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE chunks_fts MATCH ? AND c.active=1
        """
        params: list[Any] = [safe_query]
        access_sql, access_params = policy_sql(policy)
        if access_sql:
            sql += " AND " + access_sql
            params.extend(access_params)
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND c.book_id IN ({placeholders})"
            params.extend(book_ids)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def search_like(self, query: str, book_ids: list[str] | None = None,
                    limit: int = 30,
                    policy: KnowledgeAccessPolicy | None = None) -> list[dict[str, Any]]:
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
                   c.sensitivity AS sensitivity,
                   b.title AS book_title, b.root_path,
                   d.relative_path AS relative_path, d.content_role AS content_role,
                   0.0 AS rank
            FROM chunks c
            JOIN books b ON b.book_id = c.book_id
            JOIN documents d ON d.document_id = c.document_id
            WHERE c.active=1 AND (c.text LIKE ? OR c.chapter LIKE ? OR c.section LIKE ?)
        """
        params: list[Any] = [pattern, pattern, pattern]
        access_sql, access_params = policy_sql(policy)
        if access_sql:
            sql += " AND " + access_sql
            params.extend(access_params)
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

    def recover_orphan_jobs(self) -> int:
        """把长期停留在 running/queued 的孤儿 Job 标记为 failed（P1-8）。

        后台线程异常退出时 Job 可能永久 running；下次以写模式打开共享库时
        清理这些孤儿，避免 UI 永久显示"索引中"。
        """
        import datetime as _dt
        from datetime import timezone as _tz
        cutoff = (_dt.datetime.now(_tz.utc) - _dt.timedelta(hours=1)).isoformat()
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE index_jobs SET status='failed', error='orphaned job recovered', "
                "finished_at=? WHERE status IN ('queued','running') AND started_at < ?",
                (_now_iso(), cutoff),
            )
            return cur.rowcount

    # ---- memory_candidates (P1-4) ----

    def add_memory_candidate(self, book_id: str, content: str, source: str = "",
                             category: str = "knowledge", confidence: float = 0.5,
                             chunk_id: str | None = None,
                             document_id: str = "", source_text_hash: str = "",
                             kind: str = "fact") -> str:
        """写入一条记忆候选（待审核）。返回 candidate_id。

        同一 content 重复写入时保留既有审核状态（INSERT ... DO UPDATE 而非
        INSERT OR REPLACE），避免用户已审核的候选被重新置回 pending（P1-3）。
        """
        valid_kinds = {"fact", "project", "procedure", "preference"}
        if kind not in valid_kinds:
            raise ValueError(f"invalid candidate kind: {kind}")
        candidate_id = _stable_hash("mc", book_id, content)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO memory_candidates
                   (candidate_id, book_id, chunk_id, document_id, source_text_hash,
                    content, source, category, kind, confidence, status, sync_error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,'pending','',?)
                   ON CONFLICT(candidate_id) DO UPDATE SET
                       chunk_id=excluded.chunk_id,
                       document_id=excluded.document_id,
                       source_text_hash=excluded.source_text_hash,
                       source=excluded.source,
                       category=excluded.category,
                       kind=excluded.kind,
                       confidence=excluded.confidence,
                       status=CASE WHEN memory_candidates.status='stale'
                                   THEN 'pending' ELSE memory_candidates.status END,
                       sync_error=CASE WHEN memory_candidates.status='stale'
                                       THEN '' ELSE memory_candidates.sync_error END""",
                (candidate_id, book_id, chunk_id, document_id, source_text_hash,
                 content, source, category, kind, confidence, _now_iso()),
            )
        return candidate_id

    def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_memory_candidates(self, book_id: str | None = None,
                               status: str = "pending") -> list[dict[str, Any]]:
        """列出记忆候选（默认待审核）。"""
        sql = ("SELECT candidate_id, book_id, chunk_id, document_id, source_text_hash, "
               "content, source, category, kind, confidence, status, synced_memory_id, "
               "sync_error, target_group_id, sync_started_at, sync_attempt_id, "
               "created_at, reviewed_at FROM memory_candidates")
        params: list[Any] = []
        conds: list[str] = []
        if book_id:
            conds.append("book_id=?")
            params.append(book_id)
        if status and status != "all":
            if status == "approved":
                # Synced is a successful approved candidate and remains visible
                # to existing callers that ask for approved items.
                conds.append("status IN ('approved','synced')")
            elif status == "actionable":
                conds.append("status IN ('pending','sync_failed')")
            else:
                conds.append("status=?")
                params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def review_memory_candidate(self, candidate_id: str, status: str) -> bool:
        """审核记忆候选：approve / reject。仅在 pending 状态可审核（幂等）。"""
        status = {"approve": "approved", "reject": "rejected"}.get(status, status)
        if status not in {"approved", "rejected"}:
            return False
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status=?, reviewed_at=? "
                "WHERE candidate_id=? AND status IN ('pending','sync_failed')",
                (status, _now_iso(), candidate_id),
            )
            return cur.rowcount > 0

    def keep_memory_candidate(self, candidate_id: str) -> bool:
        """保留候选但不执行同步；失败候选恢复为可处理的 pending。"""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status='pending', sync_error='', "
                "reviewed_at=NULL WHERE candidate_id=? "
                "AND status IN ('pending','sync_failed')",
                (candidate_id,),
            )
            return cur.rowcount > 0

    def begin_candidate_sync(
        self,
        candidate_id: str,
        target_group_id: str,
        sync_attempt_id: str,
        *,
        stale_after_seconds: int = 600,
    ) -> dict[str, Any]:
        """Atomically claim a candidate before any governed memory write."""
        if not target_group_id or not sync_attempt_id:
            return {"ok": False, "error": "candidate_sync_target_required"}
        now = _now_iso()
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(1, stale_after_seconds))
        ).isoformat()
        with self._tx() as conn:
            cur = conn.execute(
                """
                UPDATE memory_candidates
                   SET status='syncing',
                       target_group_id=CASE
                           WHEN target_group_id='' THEN ?
                           ELSE target_group_id
                       END,
                       sync_started_at=?,
                       sync_attempt_id=?,
                       sync_error='',
                       reviewed_at=?
                 WHERE candidate_id=?
                   AND (
                       (
                           status IN ('pending','sync_failed','approved')
                           AND (target_group_id='' OR target_group_id=?)
                       )
                       OR (
                           status='syncing'
                           AND target_group_id=?
                           AND sync_started_at < ?
                       )
                   )
                """,
                (
                    target_group_id,
                    now,
                    sync_attempt_id,
                    now,
                    candidate_id,
                    target_group_id,
                    target_group_id,
                    cutoff,
                ),
            )
            if cur.rowcount == 1:
                return {
                    "ok": True,
                    "state": "claimed",
                    "target_group_id": target_group_id,
                    "sync_attempt_id": sync_attempt_id,
                }
            row = conn.execute(
                "SELECT status, target_group_id, synced_memory_id "
                "FROM memory_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "candidate_not_found"}
            current_group = str(row["target_group_id"] or "")
            if current_group and current_group != target_group_id:
                return {
                    "ok": False,
                    "error": "candidate_already_targeted_to_other_group",
                    "target_group_id": current_group,
                }
            if row["status"] == "synced":
                return {
                    "ok": True,
                    "state": "already_synced",
                    "target_group_id": current_group or target_group_id,
                    "memory_id": str(row["synced_memory_id"] or ""),
                }
            if row["status"] == "syncing":
                return {
                    "ok": False,
                    "error": "candidate_sync_in_progress",
                    "target_group_id": current_group,
                }
            return {
                "ok": False,
                "error": "candidate_not_actionable",
                "status": str(row["status"] or ""),
            }

    def complete_candidate_sync(
        self,
        candidate_id: str,
        memory_id: str,
        target_group_id: str,
        sync_attempt_id: str,
    ) -> bool:
        if not memory_id:
            return False
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status='synced', synced_memory_id=?, "
                "sync_error='', sync_started_at='', reviewed_at=? "
                "WHERE candidate_id=? AND status='syncing' "
                "AND target_group_id=? AND sync_attempt_id=?",
                (
                    memory_id,
                    _now_iso(),
                    candidate_id,
                    target_group_id,
                    sync_attempt_id,
                ),
            )
            return cur.rowcount == 1

    def fail_candidate_sync(
        self,
        candidate_id: str,
        error: str,
        target_group_id: str,
        sync_attempt_id: str,
    ) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status='sync_failed', sync_error=?, "
                "sync_started_at='', reviewed_at=? "
                "WHERE candidate_id=? AND status='syncing' "
                "AND target_group_id=? AND sync_attempt_id=?",
                (
                    str(error)[:1000],
                    _now_iso(),
                    candidate_id,
                    target_group_id,
                    sync_attempt_id,
                ),
            )
            return cur.rowcount == 1

    def mark_candidate_synced(self, candidate_id: str, memory_id: str) -> bool:
        """在长期记忆写入成功后原子标记候选已同步。"""
        if not memory_id:
            return False
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status='synced', synced_memory_id=?, "
                "sync_error='', sync_started_at='', reviewed_at=? WHERE candidate_id=? "
                "AND status IN ('pending','sync_failed','approved')",
                (memory_id, _now_iso(), candidate_id),
            )
            return cur.rowcount > 0

    def mark_candidate_sync_failed(self, candidate_id: str, error: str) -> bool:
        """记录同步失败但保留候选，使用户可以重试。"""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE memory_candidates SET status='sync_failed', sync_error=?, "
                "sync_started_at='', reviewed_at=? WHERE candidate_id=? "
                "AND status IN ('pending','approved','sync_failed','syncing')",
                (str(error)[:1000], _now_iso(), candidate_id),
            )
            return cur.rowcount > 0

    def set_candidate_synced(self, candidate_id: str, memory_id: str) -> None:
        """记录候选已同步的长期记忆 ID（P1-2）。"""
        with self._tx() as conn:
            conn.execute(
                "UPDATE memory_candidates SET synced_memory_id=?, status='synced', "
                "sync_error='' WHERE candidate_id=?",
                (memory_id, candidate_id),
            )

    def count_memory_candidates(self, status: str = "pending") -> int:
        if status == "approved":
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_candidates "
                "WHERE status IN ('approved','synced')",
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE status=?", (status,),
            ).fetchone()
        return int(row[0])

    # ---- embeddings (KB2) ----

    def upsert_embedding(self, chunk_id: str, embedding_model: str,
                         dimension: int, vector: list[float],
                         text_hash: str, embedding_space_id: str = "default") -> None:
        """存储或更新 chunk 的 embedding（KB2）。vector 为 float 列表。"""
        import struct
        blob = struct.pack(f"{len(vector)}f", *vector) if vector else b""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(chunk_id, embedding_space_id, embedding_model, dimension, vector, text_hash, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (chunk_id, embedding_space_id, embedding_model, dimension,
                 blob, text_hash, _now_iso()),
            )

    def get_embedding(self, chunk_id: str, embedding_space_id: str = "default") -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM embeddings WHERE chunk_id=? AND embedding_space_id=?",
            (chunk_id, embedding_space_id),
        ).fetchone()
        return dict(row) if row else None

    def list_chunks_without_embedding(self, book_id: str,
                                      embedding_model: str,
                                      embedding_space_id: str = "default") -> list[sqlite3.Row]:
        """列出需要生成 embedding 的 chunk（KB2，text_hash 变化则需重建）。"""
        return self._conn.execute(
            "SELECT c.chunk_id, c.text_hash FROM chunks c "
            "WHERE c.book_id=? AND c.active=1 "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM embeddings e WHERE e.chunk_id=c.chunk_id "
            "  AND e.embedding_space_id=? AND e.embedding_model=? AND e.text_hash=c.text_hash"
            ")",
            (book_id, embedding_space_id, embedding_model),
        ).fetchall()

    def list_remote_query_authorized_book_ids(
        self,
        book_ids: list[str] | None = None,
    ) -> list[str]:
        sql = (
            "SELECT book_id FROM books "
            "WHERE remote_embedding_allowed=1 "
            "AND remote_query_embedding_allowed=1"
        )
        params: list[Any] = []
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND book_id IN ({placeholders})"
            params.extend(book_ids)
        sql += " ORDER BY book_id"
        return [
            str(row["book_id"])
            for row in self._conn.execute(sql, params).fetchall()
        ]

    def has_searchable_embeddings(
        self,
        embedding_space_id: str,
        book_ids: list[str] | None = None,
        policy: KnowledgeAccessPolicy | None = None,
    ) -> bool:
        sql = (
            "SELECT 1 FROM embeddings e "
            "JOIN chunks c ON c.chunk_id=e.chunk_id "
            "JOIN documents d ON d.document_id=c.document_id "
            "WHERE e.embedding_space_id=? AND c.active=1"
        )
        params: list[Any] = [embedding_space_id]
        access_sql, access_params = policy_sql(policy)
        if access_sql:
            sql += " AND " + access_sql
            params.extend(access_params)
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND c.book_id IN ({placeholders})"
            params.extend(book_ids)
        sql += " LIMIT 1"
        return self._conn.execute(sql, params).fetchone() is not None

    def count_embeddings(
        self,
        book_id: str,
        embedding_space_id: str,
    ) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM embeddings e "
            "JOIN chunks c ON c.chunk_id=e.chunk_id "
            "WHERE c.book_id=? AND c.active=1 AND e.embedding_space_id=?",
            (book_id, embedding_space_id),
        ).fetchone()
        return int(row[0] if row else 0)

    def search_vectors(self, query_vec: list[float],
                       book_ids: list[str] | None = None,
                       limit: int = 30,
                       embedding_space_id: str = "default",
                       policy: KnowledgeAccessPolicy | None = None) -> list[dict[str, Any]]:
        """Python cosine 相似度向量检索（KB2，无 sqlite-vec 依赖）。

        强制按 embedding_space_id 过滤，并在比对时校验维度一致，
        避免不同模型/维度混用造成错误相似度（P1-1）。
        """
        q_dim = len(query_vec)
        q_norm = _vector_norm(query_vec)
        if q_norm == 0.0:
            return []
        sql = (
            "SELECT e.chunk_id, e.vector, e.dimension, e.text_hash, "
            "c.document_id, c.book_id, c.chapter, c.section, c.ordinal, "
            "c.text, c.summary, c.keywords, c.line_start, c.line_end, "
            "c.sensitivity AS sensitivity, "
            "b.title AS book_title, b.root_path, "
            "d.relative_path AS relative_path, d.content_role AS content_role "
            "FROM embeddings e "
            "JOIN chunks c ON c.chunk_id=e.chunk_id "
            "JOIN books b ON b.book_id=c.book_id "
            "JOIN documents d ON d.document_id=c.document_id "
            "WHERE c.active=1 AND e.embedding_space_id=?"
        )
        params: list[Any] = [embedding_space_id]
        access_sql, access_params = policy_sql(policy)
        if access_sql:
            sql += " AND " + access_sql
            params.extend(access_params)
        if book_ids:
            placeholders = ",".join("?" * len(book_ids))
            sql += f" AND c.book_id IN ({placeholders})"
            params.extend(book_ids)
        rows = self._conn.execute(sql, params).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            # 维度不一致直接跳过，不静默截断混用（P1-1）
            if row["dimension"] != q_dim:
                continue
            vec = _deserialize_vector(row["vector"], row["dimension"])
            v_norm = _vector_norm(vec)
            if v_norm == 0.0:
                continue
            dot = sum(a * b for a, b in zip(query_vec, vec))
            sim = dot / (q_norm * v_norm)
            d = dict(row)
            d.pop("vector", None)
            d.pop("dimension", None)
            d["rank"] = -sim  # 与 FTS5 rank 一致：越小越好
            d["retrieval_method"] = "vector"
            scored.append((sim, d))
        scored.sort(key=lambda x: -x[0])
        return [d for _, d in scored[:limit]]


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        book_id=row["book_id"], title=row["title"], root_path=row["root_path"],
        cover_style=row["cover_style"], description=row["description"], status=row["status"],
        file_count=row["file_count"], chapter_count=row["chapter_count"],
        chunk_count=row["chunk_count"], entity_count=row["entity_count"],
        last_indexed_at=row["last_indexed_at"], include_globs=row["include_globs"],
        exclude_globs=row["exclude_globs"], auto_extract_memory=bool(row["auto_extract_memory"]),
        vector_enabled=row["vector_enabled"],
        remote_embedding_allowed=bool(
            int(row["remote_embedding_allowed"]) if "remote_embedding_allowed" in row.keys() else 0
        ),
        remote_query_embedding_allowed=bool(
            int(row["remote_query_embedding_allowed"])
            if "remote_query_embedding_allowed" in row.keys()
            else 0
        ),
        build_phases=_parse_phases(row["build_phases"]) if "build_phases" in row.keys() else {},
    )


def _parse_phases(raw: str) -> dict[str, Any]:
    """解析 build_phases JSON，损坏时回退空 dict。"""
    if not raw:
        return {}
    try:
        import json
        data = json.loads(raw)
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _row_to_chunk(row: sqlite3.Row) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"], document_id=row["document_id"], book_id=row["book_id"],
        chapter=row["chapter"], section=row["section"], ordinal=row["ordinal"],
        text=row["text"], summary=row["summary"], keywords=row["keywords"],
        line_start=row["line_start"], line_end=row["line_end"],
        text_hash=row["text_hash"], active=bool(row["active"]),
        sensitivity=row["sensitivity"] if "sensitivity" in row.keys() else "normal",
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


def _vector_norm(vec: list[float]) -> float:
    """向量 L2 范数。"""
    return sum(x * x for x in vec) ** 0.5


def _deserialize_vector(blob: bytes, dimension: int) -> list[float]:
    """从 BLOB 反序列化 float 向量。"""
    import struct
    if not blob or dimension <= 0:
        return []
    try:
        return list(struct.unpack(f"{dimension}f", blob))
    except struct.error:
        return []


def open_shared_knowledge_store(
    *,
    read_only: bool = False,
    must_exist: bool = False,
) -> "KnowledgeStore | None":
    """返回唯一的全局共享知识库 Store。

    所有入口（GUI / MCP / Context Bootstrap）必须经此打开同一个数据库，
    禁止把 workspace 传给 KnowledgeStore。全局库落 resolve_data_home()。
    - read_only=True：不创建目录、不写库，SQLite 以 mode=ro 打开。
    - must_exist=True：数据库不存在时返回 None（调用方按空书架处理）。
    """
    data_home = resolve_data_home()
    db = knowledge_db_path(data_home)
    if must_exist and not db.exists():
        return None
    store = KnowledgeStore(data_home, read_only=read_only)
    if not read_only:
        try:
            store.recover_orphan_jobs()
        except Exception:
            pass
    return store
