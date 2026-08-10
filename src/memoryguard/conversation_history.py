"""Local conversation evidence store.

Raw conversations are evidence, not ``SharedMemoryRecord`` values.  This
module deliberately owns a separate SQLite database and never imports the
shared-memory store, context bootstrap, or projection builder.  Callers must
provide a trusted Agent scope; the store does not infer a cross-agent scope.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_PAGE = 100
MAX_TIMELINE_RADIUS = 25
MAX_IMPORT_CONVERSATIONS = 1_000
MAX_TURN_CHARS = 100_000
MAX_IMPORT_CHARS = 20_000_000
MAX_AGENT_ID_CHARS = 256
MAX_SESSION_ID_CHARS = 1_024
MAX_PROJECT_REF_CHARS = 2_048
MAX_PROVIDER_CHARS = 64
MAX_SHARE_GROUP_ID_CHARS = 256
MAX_TITLE_CHARS = 500
MAX_DERIVED_TITLE_CHARS = 88
MAX_MATCHED_SUMMARY_CHARS = 320
HISTORY_SCHEMA_VERSION = 3


class HistorySchemaError(RuntimeError):
    """The history database is missing, partial, or from an unsupported version."""

    def __init__(self, code: str) -> None:
        allowed = {
            "history_schema_future", "history_schema_invalid", "history_schema_partial",
            "history_schema_unsupported", "history_schema_version_invalid",
        }
        self.code = str(code) if str(code) in allowed else "history_schema_invalid"
        super().__init__(self.code)


# These are the canonical user-facing tables.  The native adapter imports the
# contract so a read-only open cannot silently accept a partial or widened
# schema.  SQLite's FTS implementation creates additional internal tables;
# those names are listed separately below and are validated by name only.
HISTORY_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "conversation_sessions": frozenset({
        "session_id", "external_id", "title", "provider", "agent_instance_id",
        "project_ref", "share_group_id", "created_at", "imported_at", "deleted_at",
    }),
    "conversation_turns": frozenset({
        "turn_id", "session_id", "ordinal", "role", "content", "created_at",
        "content_type", "event_key", "content_hash",
    }),
    "session_summaries": frozenset({
        "session_id", "summary", "summary_kind", "updated_at",
    }),
    "observations": frozenset({
        "observation_id", "session_id", "turn_id", "observation_type", "summary",
        "created_at",
    }),
    "evidence_links": frozenset({
        "link_id", "memory_id", "session_id", "turn_id", "status", "created_at",
        "invalidated_at",
    }),
    "history_mutation_receipts": frozenset({
        "idempotency_key", "operation", "payload_digest", "result_json", "created_at",
    }),
    "history_fts": frozenset({
        "session_id", "turn_id", "result_type", "title", "content",
    }),
}
HISTORY_FTS_INTERNAL_TABLES = frozenset({
    "history_fts_config", "history_fts_content", "history_fts_data",
    "history_fts_docsize", "history_fts_idx",
})
HISTORY_ALLOWED_TABLES = frozenset(HISTORY_TABLE_COLUMNS) | HISTORY_FTS_INTERNAL_TABLES
_HISTORY_AUTHORITATIVE_TABLES = frozenset(HISTORY_TABLE_COLUMNS)
_HISTORY_V2_REQUIRED_TABLES = _HISTORY_AUTHORITATIVE_TABLES - {"history_mutation_receipts"}
_HISTORY_V2_TABLE_COLUMNS = {
    name: frozenset(columns)
    for name, columns in HISTORY_TABLE_COLUMNS.items()
    if name != "history_mutation_receipts"
}
_HISTORY_V2_TABLE_COLUMNS["conversation_turns"] -= {"event_key", "content_hash"}
_HISTORY_V2_TABLE_COLUMNS["history_fts"] -= {"result_type"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: str, limit: int = 220) -> str:
    value = " ".join((value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _provider_label(provider: str) -> str:
    """Keep a missing-title fallback stable and useful without raw content."""
    labels = {
        "codex": "Codex", "claude": "Claude", "cursor": "Cursor",
        "chatgpt": "ChatGPT", "trae": "TRAE",
    }
    normalized = " ".join(str(provider or "").split()).lower()
    return labels.get(normalized, normalized.title() or "Agent")


def _title_text(value: Any, *, limit: int = MAX_TITLE_CHARS) -> str:
    """Normalize a human-facing title without attempting to summarize raw chat."""
    text = str(value or "").replace("\x00", " ")
    # First user turns are often Markdown headings or list entries.  Keep the
    # words, not the presentation noise, so a graph label is compact.
    text = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|>\s+|\d+[.)]\s+)", "", text)
    text = re.sub(r"[`*_]+", "", text)
    text = " ".join(text.split()).strip(" -—:：")
    return _short(text, limit)


def _fallback_title(provider: str, occurred_at: str = "") -> str:
    """Return a non-empty deterministic fallback; never expose a session id."""
    raw = str(occurred_at or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        moment = parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        # Preserve a supplied, already human-readable date rather than
        # generating an unstable value on every re-open.
        moment = _title_text(raw, limit=32) or "未知时间"
    return f"{_provider_label(provider)} 对话 · {moment}"


def _is_low_quality_title(title: Any, *, external_id: str = "", session_id: str = "") -> bool:
    normalized = _title_text(title).casefold()
    if not normalized or normalized in {"未命名会话", "未命名", "untitled", "new chat", "new conversation"}:
        return True
    identities = {_title_text(external_id).casefold(), _title_text(session_id).casefold()}
    if normalized in identities - {""}:
        return True
    # Old partial JSONL imports used rollout/file stems as their title.  They
    # are identifiers, not useful conversation names, and can be backfilled.
    if re.search(r"(?:^|[\\/])?rollout[-_]\d|\.jsonl?(?:\s|$)", normalized):
        return True
    return bool(re.fullmatch(r"(?:hist[-_])?[0-9a-f]{16,64}", normalized))


def _first_user_title(messages: Iterable[Any]) -> str:
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        text = _title_text(message.get("content"), limit=MAX_DERIVED_TITLE_CHARS)
        if text:
            return text
    return ""


def _choose_session_title(
    *, explicit_title: Any, messages: Iterable[Any], provider: str,
    occurred_at: str, external_id: str = "", session_id: str = "",
) -> tuple[str, int]:
    """Prefer host title, then first visible user turn, then stable fallback.

    Quality is only used within a write transaction to ensure an incomplete
    re-import cannot replace a useful existing title.
    """
    explicit = _title_text(explicit_title)
    if explicit and not _is_low_quality_title(explicit, external_id=external_id, session_id=session_id):
        return explicit, 3
    user_title = _first_user_title(messages)
    if user_title:
        return user_title, 2
    return _fallback_title(provider, occurred_at), 1


def _bounded_identity(value: Any, *, field: str, limit: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field}_required")
    if len(text) > limit:
        raise ValueError(f"{field}_too_long")
    return text


def _normalize_project_ref(value: Any) -> str:
    from .rule_scope import canonical_project_ref
    return canonical_project_ref(
        _bounded_identity(value, field="project_ref", limit=MAX_PROJECT_REF_CHARS)
    )


def _project_metadata(project_ref: str) -> dict[str, str]:
    """Safe, deterministic project identity for history projections."""
    ref = _normalize_project_ref(project_ref)
    key = "history-project-" + hashlib.sha256((ref or "unknown").encode("utf-8")).hexdigest()[:20]
    if not ref:
        return {"project_key": key, "project_ref": "", "project_label": "未识别项目", "project_status": "unknown", "project_parent": ""}
    filesystem_ref = "/" in ref or "\\" in ref or bool(re.match(r"^[A-Za-z]:", ref))
    path = Path(ref) if filesystem_ref else None
    if path is not None:
        label = path.name or str(path)
        parent = path.parent.name
        status = "available" if path.exists() else "removed"
    else:
        label, parent, status = ref, "", "available"
    return {"project_key": key, "project_ref": ref, "project_label": label,
            "project_status": status, "project_parent": parent}


def _safe_session_projection(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["owner_agent_instance_id"] = str(item.get("agent_instance_id") or "")
    item.update(_project_metadata(str(item.get("project_ref") or "")))
    return item


@dataclass(frozen=True)
class HistoryScope:
    """Identity boundary for raw history.  ``share_group_id`` is metadata,
    not permission to read another member's private history.
    """

    agent_instance_id: str
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    # Only HistoryAccessResolver may populate this for a shared read.  A
    # plain HistoryScope remains an owner/write scope for compatibility.
    authorized_agent_ids: tuple[str, ...] = ()
    shared_read: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_instance_id", _bounded_identity(
            self.agent_instance_id, field="agent_instance_id",
            limit=MAX_AGENT_ID_CHARS, required=True,
        ))
        object.__setattr__(self, "project_ref", _normalize_project_ref(self.project_ref))
        object.__setattr__(self, "provider", _bounded_identity(
            self.provider, field="provider", limit=MAX_PROVIDER_CHARS,
        ).lower())
        object.__setattr__(self, "share_group_id", _bounded_identity(
            self.share_group_id, field="share_group_id", limit=MAX_SHARE_GROUP_ID_CHARS,
        ))
        authorized = tuple(sorted({
            _bounded_identity(agent, field="authorized_agent_id", limit=MAX_AGENT_ID_CHARS, required=True)
            for agent in self.authorized_agent_ids
        }))
        object.__setattr__(self, "authorized_agent_ids", authorized)

    @classmethod
    def trusted(cls, data: dict[str, Any] | None, trusted_agent_id: str) -> "HistoryScope":
        data = data or {}
        requested = str(data.get("agent_instance_id") or "")
        if not trusted_agent_id or (requested and requested != trusted_agent_id):
            raise PermissionError("trusted_agent_scope_required")
        return cls(
            agent_instance_id=trusted_agent_id,
            project_ref=str(data.get("project_ref") or ""),
            provider=str(data.get("provider") or ""),
            share_group_id=str(data.get("share_group_id") or ""),
        )


class HistoryAccessResolver:
    """Create read scopes from the caller's *current* active binding.

    Raw session rows retain their owner and historical group metadata.  Read
    membership is deliberately evaluated for every request, so leaving a
    group immediately removes visibility without copying or rewriting rows.
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()

    def resolve(self, trusted_agent_id: str, requested: dict[str, Any] | None = None) -> HistoryScope:
        from .agent_binding import is_personal_group_id
        from .governance_scope import resolve_active_scope

        request = dict(requested or {})
        if not request and trusted_agent_id:
            request = {
                "mode": "agent",
                "agent_instance_id": trusted_agent_id,
            }
        resolution = resolve_active_scope(
            self.workspace,
            request,
            trusted_agent_id=trusted_agent_id,
        )
        if not resolution.ok:
            # Keep the stable history error contract while routing all
            # binding checks through the shared runtime scope resolver.
            if resolution.error in {
                "trusted_agent_scope_required",
                "trusted_share_group_scope_required",
            }:
                raise PermissionError(resolution.error)
            raise PermissionError(
                "history_active_binding_required"
                if resolution.error in {"active_binding_required", "multiple_active_bindings"}
                else resolution.error or "history_scope_required"
            )

        scope = resolution.scope
        assert scope is not None
        shared = (
            scope.mode == "share_group"
            and not is_personal_group_id(scope.share_group_id)
        )
        return HistoryScope(
            agent_instance_id=resolution.principal_agent_id,
            project_ref=str(request.get("project_ref") or ""),
            provider=str(request.get("provider") or ""),
            share_group_id=scope.share_group_id if shared else "",
            authorized_agent_ids=resolution.authorized_agent_ids,
            shared_read=shared,
        )


class ConversationHistoryStore:
    """SQLite/WAL history store with bounded, progressive retrieval.

    The database is local to one MemoryGuard workspace.  Every public read
    takes a ``HistoryScope``; an empty agent id is rejected, so callers cannot
    accidentally query every local Agent's raw conversations.
    """

    # Native adapters may require a durable replay fence before accepting a
    # destructive DI path.  This marker is separate from the read-only
    # protocol, which writable instances do not claim.
    supports_durable_idempotency = True

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        self.db_path = self.workspace / ".memoryguard" / "history" / "history.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            # Legacy rows predate the shared canonical helper. Keep their
            # durable IDs untouched while reads use new-write identity rules.
            conn.create_function(
                "history_canonical_project_ref", 1, _normalize_project_ref,
                deterministic=True,
            )
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self):
        """One explicit writer transaction with guaranteed connection close."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _init_db(self) -> None:
        # Inspect an existing database without WAL/transactions/DDL first.
        # Partial history stores are a hard boundary: opening one must not
        # silently create missing tables or mutate its bytes.
        if not self.db_path.exists():
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
            try:
                self._create_schema(conn)
            finally:
                conn.close()
            return

        try:
            inspect_conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        except sqlite3.DatabaseError as exc:
            raise HistorySchemaError("history_schema_invalid") from exc
        try:
            row = inspect_conn.execute("PRAGMA user_version").fetchone()
            try:
                version = int(row[0]) if row is not None else 0
            except (TypeError, ValueError, IndexError) as exc:
                raise HistorySchemaError("history_schema_version_invalid") from exc
            if version > HISTORY_SCHEMA_VERSION:
                raise HistorySchemaError("history_schema_future")
            if version not in {0, 2, HISTORY_SCHEMA_VERSION}:
                raise HistorySchemaError("history_schema_unsupported")
            objects = self._schema_objects(inspect_conn)
            user_objects = {name for name in objects if not name.startswith("sqlite_")}
            if version == 0:
                # v0 means an actually fresh SQLite file only.  A file with
                # any user/canonical table is an explicitly partial legacy DB.
                if user_objects:
                    raise HistorySchemaError("history_schema_partial")
                needs_create = True
            elif version == 2:
                if not self._schema_matches(inspect_conn, version=2):
                    raise HistorySchemaError("history_schema_partial")
                needs_create = False
            else:
                if not self._schema_matches(inspect_conn, version=HISTORY_SCHEMA_VERSION):
                    raise HistorySchemaError("history_schema_partial")
                needs_create = False
        finally:
            inspect_conn.close()

        if needs_create:
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
            try:
                self._create_schema(conn)
            finally:
                conn.close()
            return

        # Only a complete, known schema is allowed to enter a write/migration
        # transaction.  v2 -> v3 is intentionally explicit and bounded.
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if version == 2:
                    conn.executescript("""
                    CREATE TABLE IF NOT EXISTS history_mutation_receipts (
                      idempotency_key TEXT PRIMARY KEY,
                      operation TEXT NOT NULL,
                      payload_digest TEXT NOT NULL,
                      result_json TEXT NOT NULL,
                      created_at TEXT NOT NULL
                    );
                    """)
                    self._migrate_turn_identity(conn)
                    self._migrate_evidence_tombstones(conn)
                    self._migrate_fts(conn)
                self._backfill_session_titles(conn)
                conn.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _schema_objects(conn: sqlite3.Connection) -> dict[str, str]:
        rows = conn.execute(
            "SELECT type,name FROM sqlite_master "
            "WHERE type IN ('table','view')"
        ).fetchall()
        return {str(row[1]): str(row[0]) for row in rows}

    @classmethod
    def _schema_matches(cls, conn: sqlite3.Connection, *, version: int) -> bool:
        try:
            objects = cls._schema_objects(conn)
            allowed = set(HISTORY_ALLOWED_TABLES)
            names = set(objects)
            if {name for name in names if not name.startswith("sqlite_")} - allowed:
                return False
            if version == 2:
                required = _HISTORY_V2_REQUIRED_TABLES
                expected = _HISTORY_V2_TABLE_COLUMNS
            else:
                required = _HISTORY_AUTHORITATIVE_TABLES | HISTORY_FTS_INTERNAL_TABLES
                expected = HISTORY_TABLE_COLUMNS
            if not required.issubset(names):
                return False
            for table in required:
                if table in expected:
                    if objects.get(table) != "table":
                        return False
                    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                    if columns != set(expected[table]):
                        return False
            if version == 2:
                # FTS5 internals may be rebuilt by the explicit migration, but
                # the public virtual table itself is never silently created.
                if objects.get("history_fts") != "table":
                    return False
            elif not HISTORY_FTS_INTERNAL_TABLES.issubset(names):
                return False
            return True
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_sessions (
                  session_id TEXT PRIMARY KEY,
                  external_id TEXT NOT NULL,
                  title TEXT NOT NULL DEFAULT '',
                  provider TEXT NOT NULL DEFAULT '',
                  agent_instance_id TEXT NOT NULL,
                  project_ref TEXT NOT NULL DEFAULT '',
                  share_group_id TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL DEFAULT '',
                  imported_at TEXT NOT NULL,
                  deleted_at TEXT NOT NULL DEFAULT '',
                  UNIQUE(external_id, provider, agent_instance_id, project_ref, share_group_id)
                );
                CREATE TABLE IF NOT EXISTS conversation_turns (
                  turn_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
                  ordinal INTEGER NOT NULL,
                  role TEXT NOT NULL DEFAULT 'unknown',
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT '',
                  content_type TEXT NOT NULL DEFAULT 'text',
                  event_key TEXT NOT NULL DEFAULT '',
                  content_hash TEXT NOT NULL DEFAULT '',
                  UNIQUE(session_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS session_summaries (
                  session_id TEXT PRIMARY KEY REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
                  summary TEXT NOT NULL DEFAULT '',
                  summary_kind TEXT NOT NULL DEFAULT 'import',
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                  observation_id TEXT PRIMARY KEY,
                  session_id TEXT NOT NULL REFERENCES conversation_sessions(session_id) ON DELETE CASCADE,
                  turn_id TEXT REFERENCES conversation_turns(turn_id) ON DELETE SET NULL,
                  observation_type TEXT NOT NULL,
                  summary TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence_links (
                  link_id TEXT PRIMARY KEY,
                  memory_id TEXT NOT NULL,
                  session_id TEXT NOT NULL,
                  turn_id TEXT,
                  status TEXT NOT NULL DEFAULT 'valid',
                  created_at TEXT NOT NULL,
                  invalidated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_history_sessions_scope
                  ON conversation_sessions(agent_instance_id, project_ref, provider, deleted_at);
                CREATE INDEX IF NOT EXISTS idx_history_turns_session ON conversation_turns(session_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_history_evidence_session ON evidence_links(session_id, status);
                CREATE TABLE IF NOT EXISTS history_mutation_receipts (
                  idempotency_key TEXT PRIMARY KEY,
                  operation TEXT NOT NULL,
                  payload_digest TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                  session_id UNINDEXED, turn_id UNINDEXED, result_type UNINDEXED, title, content, tokenize='unicode61'
                );
                """)
            conn.execute(f"PRAGMA user_version={HISTORY_SCHEMA_VERSION}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_turn_identity(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_turns)")}
        if "event_key" not in columns:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN event_key TEXT NOT NULL DEFAULT ''")
        if "content_hash" not in columns:
            conn.execute("ALTER TABLE conversation_turns ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        rows = conn.execute(
            "SELECT turn_id,content FROM conversation_turns WHERE content_hash=''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE conversation_turns SET content_hash=? WHERE turn_id=?",
                (hashlib.sha256(row["content"].encode("utf-8")).hexdigest(), row["turn_id"]),
            )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_history_turn_event ON conversation_turns(session_id,event_key) WHERE event_key<>''")

    @staticmethod
    def _migrate_evidence_tombstones(conn: sqlite3.Connection) -> None:
        """Old FK cascade removed evidence links together with history.

        Evidence must survive as an invalid source tombstone so a governed
        memory can explain why its raw source is unavailable.
        """
        foreign = conn.execute("PRAGMA foreign_key_list(evidence_links)").fetchall()
        if not foreign:
            return
        conn.executescript("""
            ALTER TABLE evidence_links RENAME TO evidence_links_legacy;
            CREATE TABLE evidence_links (
              link_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, session_id TEXT NOT NULL,
              turn_id TEXT, status TEXT NOT NULL DEFAULT 'valid', created_at TEXT NOT NULL,
              invalidated_at TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO evidence_links SELECT link_id,memory_id,session_id,turn_id,status,created_at,invalidated_at FROM evidence_links_legacy;
            DROP TABLE evidence_links_legacy;
            CREATE INDEX IF NOT EXISTS idx_history_evidence_session ON evidence_links(session_id, status);
        """)

    @staticmethod
    def _migrate_fts(conn: sqlite3.Connection) -> None:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(history_fts)").fetchall()]
        if "result_type" in columns:
            return
        # FTS virtual tables cannot ALTER ADD COLUMN.  Rebuild from canonical
        # tables rather than copying stale FTS content.
        conn.execute("DROP TABLE IF EXISTS history_fts")
        conn.execute("CREATE VIRTUAL TABLE history_fts USING fts5(session_id UNINDEXED, turn_id UNINDEXED, result_type UNINDEXED, title, content, tokenize='unicode61')")
        conn.execute("""INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
                        SELECT t.session_id,t.turn_id,'turn',s.title,t.content FROM conversation_turns t JOIN conversation_sessions s ON s.session_id=t.session_id""")
        conn.execute("""INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
                        SELECT ss.session_id,'','summary',s.title,ss.summary FROM session_summaries ss JOIN conversation_sessions s ON s.session_id=ss.session_id""")
        conn.execute("""INSERT INTO history_fts(session_id,turn_id,result_type,title,content)
                        SELECT o.session_id,COALESCE(o.turn_id,''),'observation',s.title,o.summary FROM observations o JOIN conversation_sessions s ON s.session_id=o.session_id""")

    @staticmethod
    def _backfill_session_titles(conn: sqlite3.Connection) -> None:
        """Idempotently repair legacy blank/file-id titles and their FTS copies."""
        rows = conn.execute(
            "SELECT session_id,external_id,title,provider,created_at,imported_at FROM conversation_sessions"
        ).fetchall()
        for row in rows:
            existing = str(row["title"] or "")
            if not _is_low_quality_title(
                existing, external_id=row["external_id"], session_id=row["session_id"],
            ):
                # A prior fallback may be upgraded once a user turn exists.
                if not existing.startswith(f"{_provider_label(row['provider'])} 对话 · "):
                    continue
            user = conn.execute(
                "SELECT content FROM conversation_turns WHERE session_id=? "
                "AND lower(role)='user' AND trim(content)<>'' ORDER BY ordinal LIMIT 1",
                (row["session_id"],),
            ).fetchone()
            messages = [{"role": "user", "content": user["content"]}] if user else []
            title, _ = _choose_session_title(
                explicit_title="", messages=messages, provider=row["provider"],
                occurred_at=row["created_at"] or row["imported_at"],
                external_id=row["external_id"], session_id=row["session_id"],
            )
            if title == existing:
                continue
            conn.execute(
                "UPDATE conversation_sessions SET title=? WHERE session_id=?", (title, row["session_id"])
            )
            conn.execute("UPDATE history_fts SET title=? WHERE session_id=?", (title, row["session_id"]))

    @staticmethod
    def _scope_where(scope: HistoryScope) -> tuple[str, list[str]]:
        if not scope.agent_instance_id:
            raise ValueError("agent_instance_id_required")
        authorized = scope.authorized_agent_ids or (scope.agent_instance_id,)
        sql = "s.agent_instance_id IN (" + ",".join("?" for _ in authorized) + ") AND s.deleted_at = ''"
        args = list(authorized)
        if scope.project_ref:
            sql += " AND history_canonical_project_ref(s.project_ref) = ?"
            args.append(scope.project_ref)
        if scope.provider:
            sql += " AND s.provider = ?"
            args.append(scope.provider)
        # Group stored on a row is lineage metadata, not the authorization
        # predicate.  A newly joined member can therefore expose its prior
        # sessions, while an unbound member is removed by the resolver.
        return sql, args

    @staticmethod
    def _owner_where(scope: HistoryScope) -> tuple[str, list[str]]:
        """Mutations never inherit a shared read's fan-out capability."""
        sql = "s.agent_instance_id = ? AND s.deleted_at = ''"
        args = [scope.agent_instance_id]
        if scope.project_ref:
            sql += " AND history_canonical_project_ref(s.project_ref) = ?"
            args.append(scope.project_ref)
        if scope.provider:
            sql += " AND s.provider = ?"
            args.append(scope.provider)
        return sql, args

    @staticmethod
    def _session_id(external_id: str, scope: HistoryScope, provider: str) -> str:
        import hashlib
        raw = "\x1f".join((external_id, provider, scope.agent_instance_id, scope.project_ref, scope.share_group_id))
        return "hist-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _resolve_canonical_session(
        conn: Any,
        candidate_session_id: str,
        external_id: str,
        scope: HistoryScope,
        provider: str,
    ) -> str:
        """Retarget an append at the canonical session row for ``external_id``.

        The session identity hashes ``provider`` into the key, so the same
        physical conversation archived under two hosts (the dual-write bug)
        yields two rows.  Look the row up by ``external_id`` -- same agent /
        same canonical project first, then any agent -- and when a *different*
        row already exists, fold it into the current host-proven identity
        (provider / agent / project / group) and return its ``session_id`` so
        the append continues under the canonical row.  Returns the candidate
        when no such row exists.
        """
        same_agent = conn.execute(
            "SELECT session_id FROM conversation_sessions "
            "WHERE external_id=? AND deleted_at='' AND agent_instance_id=? "
            "AND (project_ref=? OR project_ref='') "
            "ORDER BY CASE WHEN project_ref=? THEN 0 ELSE 1 END LIMIT 1",
            (external_id, scope.agent_instance_id, scope.project_ref, scope.project_ref),
        ).fetchone()
        row = same_agent
        if row is None:
            # Cross-agent fallback: the original bug also wrote rows under a
            # different agent instance.  Consolidate onto one row either way.
            row = conn.execute(
                "SELECT session_id FROM conversation_sessions "
                "WHERE external_id=? AND deleted_at='' "
                "ORDER BY rowid LIMIT 1",
                (external_id,),
            ).fetchone()
        if row is None:
            return candidate_session_id
        canonical = str(row["session_id"])
        if canonical == candidate_session_id:
            return canonical
        # Fold the row into the current identity so the subsequent
        # ON CONFLICT(external_id, provider, agent_instance_id, project_ref,
        # share_group_id) matches it instead of colliding on the primary key.
        conn.execute(
            "UPDATE conversation_sessions SET provider=?, agent_instance_id=?, "
            "project_ref=?, share_group_id=? WHERE session_id=?",
            (provider, scope.agent_instance_id, scope.project_ref,
             scope.share_group_id, canonical),
        )
        return canonical

    @staticmethod
    def _import_turn_id(session_id: str, source_ordinal: int, content_hash: str) -> str:
        return f"{session_id}-i{source_ordinal:06d}-{content_hash[:16]}"

    def import_conversations(self, conversations: Iterable[Any], *, provider: str,
                             scope: HistoryScope, shadow: Any | None = None,
                             shadow_max_turns: int = 1000,
                             shadow_max_chars: int = 1_000_000) -> dict[str, Any]:
        """Idempotently archive parsed ImportedConversation objects.

        This method intentionally creates no MemoryRecord and no evidence link.
        """
        if not scope.agent_instance_id:
            raise ValueError("agent_instance_id_required")
        items = list(conversations)
        if len(items) > MAX_IMPORT_CONVERSATIONS:
            raise ValueError("history_import_conversation_limit_exceeded")
        from .encoding_guard import guard_persist_content

        def _guard_import_content(value: str) -> str:
            """Repair mojibake; unrecoverable messages are skipped (fail-closed
            per message, never aborting the whole batch import)."""
            return guard_persist_content(value)

        sessions = turns = total_chars = 0
        with self._transaction() as conn:
            try:
                for conv in items:
                    external_id = _bounded_identity(
                        getattr(conv, "conv_id", ""), field="external_session_id",
                        limit=MAX_SESSION_ID_CHARS,
                    )
                    if not external_id:
                        continue
                    active_provider = _bounded_identity(
                        provider or "unknown", field="provider", limit=MAX_PROVIDER_CHARS,
                    ).lower()
                    # Project identity belongs to a conversation, not its
                    # source batch.  Missing metadata remains unknown.
                    conv_scope = replace(
                        scope,
                        project_ref=str(getattr(conv, "project_ref", "") or scope.project_ref),
                    )
                    session_id = self._session_id(external_id, conv_scope, active_provider)
                    messages = list(getattr(conv, "messages", []) or [])
                    # There is no lifetime session-turn ceiling.  Bounded
                    # provider batches belong to the optional shadow bridge;
                    # V1 history remains complete for arbitrarily long logs.
                    created_at = next((str(m.get("created_at") or "") for m in messages if isinstance(m, dict) and m.get("created_at")), "")
                    candidate_title, candidate_quality = _choose_session_title(
                        explicit_title=getattr(conv, "title", ""), messages=messages,
                        provider=active_provider, occurred_at=created_at or _now(),
                        external_id=external_id, session_id=session_id,
                    )
                    # A one-time metadata backfill moves an old unknown
                    # project row in place.  Session/turn/evidence IDs remain
                    # stable; the project column is the only identity that
                    # changes.  Prefer an exact current-project row.
                    prior = conn.execute(
                        "SELECT session_id, provider FROM conversation_sessions "
                        "WHERE external_id=? AND provider=? AND agent_instance_id=? "
                        "AND (project_ref=? OR project_ref='') "
                        "ORDER BY CASE WHEN project_ref=? THEN 0 ELSE 1 END LIMIT 1",
                        (external_id, active_provider, conv_scope.agent_instance_id,
                         conv_scope.project_ref, conv_scope.project_ref),
                    ).fetchone()
                    if prior is None:
                        # Cross-provider retry: a conversation imported under
                        # another host (dual-write) must fold into one row.
                        prior = conn.execute(
                            "SELECT session_id, provider FROM conversation_sessions "
                            "WHERE external_id=? AND agent_instance_id=? "
                            "AND (project_ref=? OR project_ref='') "
                            "ORDER BY CASE WHEN project_ref=? THEN 0 ELSE 1 END LIMIT 1",
                            (external_id, conv_scope.agent_instance_id,
                             conv_scope.project_ref, conv_scope.project_ref),
                        ).fetchone()
                    if prior is not None:
                        session_id = str(prior["session_id"])
                        if str(prior["provider"] or "") != active_provider:
                            conn.execute(
                                "UPDATE conversation_sessions SET provider=? WHERE session_id=?",
                                (active_provider, session_id),
                            )
                    existing = conn.execute(
                        "SELECT title FROM conversation_sessions WHERE session_id=?", (session_id,)
                    ).fetchone()
                    existing_title = str(existing["title"] or "") if existing else ""
                    existing_quality = 0 if _is_low_quality_title(
                        existing_title, external_id=external_id, session_id=session_id,
                    ) else (1 if existing_title.startswith(f"{_provider_label(active_provider)} 对话 · ") else 3)
                    # A real host title is authoritative.  Otherwise preserve
                    # a title at least as informative as this import, so a
                    # truncated/blank re-import cannot degrade it.
                    title = candidate_title if candidate_quality == 3 or candidate_quality > existing_quality else existing_title
                    new_turn_ids: list[str] = []
                    for source_ordinal, message in enumerate(messages, 1):
                        if not isinstance(message, dict):
                            continue
                        candidate = str(message.get("content") or "").strip()
                        if not candidate:
                            continue
                        try:
                            candidate = _guard_import_content(candidate)
                        except ValueError:
                            continue
                        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                        new_turn_ids.append(self._import_turn_id(session_id, source_ordinal, digest))
                    conn.execute("""
                      INSERT INTO conversation_sessions(session_id, external_id, title, provider, agent_instance_id, project_ref, share_group_id, created_at, imported_at, deleted_at)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                      ON CONFLICT(session_id)
                      DO UPDATE SET title=excluded.title, project_ref=excluded.project_ref,
                                    share_group_id=excluded.share_group_id,
                                    imported_at=excluded.imported_at, deleted_at=''
                    """, (session_id, external_id, title, active_provider, conv_scope.agent_instance_id,
                          conv_scope.project_ref, conv_scope.share_group_id, created_at, _now()))
                    previous_turn_ids = [row["turn_id"] for row in conn.execute(
                        "SELECT turn_id FROM conversation_turns WHERE session_id=? ORDER BY ordinal",
                        (session_id,),
                    ).fetchall()]
                    if previous_turn_ids and previous_turn_ids != new_turn_ids:
                        conn.execute(
                            "UPDATE evidence_links SET status='invalid',invalidated_at=? "
                            "WHERE session_id=? AND status='valid'",
                            (_now(), session_id),
                        )
                    conn.execute("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM history_fts WHERE session_id = ?", (session_id,))
                    # A re-import replaces one raw source atomically.  Derived
                    # observations/summaries from the old source must not stay
                    # searchable after its turns changed.
                    conn.execute("DELETE FROM observations WHERE session_id = ?", (session_id,))
                    conn.execute("DELETE FROM session_summaries WHERE session_id = ?", (session_id,))
                    visible = 0
                    for source_ordinal, message in enumerate(messages, 1):
                        if not isinstance(message, dict):
                            continue
                        content = str(message.get("content") or "").strip()
                        if not content:
                            continue
                        try:
                            content = _guard_import_content(content)
                        except ValueError:
                            continue
                        if len(content) > MAX_TURN_CHARS:
                            raise ValueError("history_import_turn_size_limit_exceeded")
                        total_chars += len(content)
                        if total_chars > MAX_IMPORT_CHARS:
                            raise ValueError("history_import_total_size_limit_exceeded")
                        visible += 1
                        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                        turn_id = self._import_turn_id(session_id, source_ordinal, content_hash)
                        role = str(message.get("role") or "unknown")[:64]
                        turn_created = str(message.get("created_at") or "")[:80]
                        content_type = str(message.get("content_type") or "text")[:64]
                        conn.execute("INSERT INTO conversation_turns(turn_id, session_id, ordinal, role, content, created_at, content_type, event_key, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (turn_id, session_id, visible, role, content, turn_created, content_type,
                                      f"import:{source_ordinal}:{content_hash}", content_hash))
                        conn.execute("INSERT INTO history_fts(session_id, turn_id, result_type, title, content) VALUES (?, ?, 'turn', ?, ?)",
                                     (session_id, turn_id, title, content))
                        turns += 1
                    summary = f"{visible} 条对话记录；导入时间 {_now()[:10]}"
                    conn.execute("INSERT INTO session_summaries(session_id, summary, summary_kind, updated_at) VALUES (?, ?, 'import', ?) ON CONFLICT(session_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at",
                                 (session_id, summary, _now()))
                    conn.execute("INSERT INTO history_fts(session_id, turn_id, result_type, title, content) VALUES (?, '', 'summary', ?, ?)",
                                 (session_id, title, summary))
                    sessions += 1
            except Exception:
                raise
        result: dict[str, Any] = {"conversation_count": sessions, "turn_count": turns}
        if shadow is not None:
            shadow_results = []
            for item in items:
                try:
                    shadow_results.append(shadow.sync_conversation(
                        item,
                        provider=provider,
                        scope=scope,
                        max_turns=shadow_max_turns,
                        max_chars=shadow_max_chars,
                    ))
                except Exception as exc:
                    # Shadow failures are diagnostics only; V1 has already
                    # committed and remains the primary source of truth.
                    shadow_results.append({"status": "failed", "code": type(exc).__name__})
            result["shadow"] = shadow_results
        return result

    def append_turn(
        self,
        scope: HistoryScope,
        *,
        external_session_id: str,
        provider: str,
        role: str,
        content: str,
        event_id: str = "",
        event_stable: bool = True,
        title: str = "",
        created_at: str = "",
        content_type: str = "text",
        shadow: Any | None = None,
        shadow_max_turns: int = 1000,
        shadow_max_chars: int = 1_000_000,
    ) -> dict[str, Any]:
        """Atomically append one host-captured turn without keeping raw text elsewhere.

        Stable host IDs provide strict replay idempotency.  Without one the
        caller must pass ``event_stable=False``: every observed call is kept,
        and the receipt reports degraded coverage instead of falsely treating
        equal-content messages as a duplicate.
        """
        if not scope.agent_instance_id:
            raise ValueError("agent_instance_id_required")
        external_session_id = _bounded_identity(
            external_session_id, field="external_session_id",
            limit=MAX_SESSION_ID_CHARS, required=True,
        )
        event_id = _bounded_identity(event_id, field="event_id", limit=MAX_SESSION_ID_CHARS)
        text = str(content or "").strip()
        active_provider = _bounded_identity(
            provider or "unknown", field="provider", limit=MAX_PROVIDER_CHARS,
            required=True,
        ).lower()
        if not text or (event_stable and not event_id):
            raise ValueError("history_event_identity_and_content_required")
        if len(text) > MAX_TURN_CHARS:
            raise ValueError("history_import_turn_size_limit_exceeded")
        # Persist-boundary mojibake guard: auto-repair pervasively corrupt
        # content; raise (fail-closed) if irrecoverable garbage remains.  This
        # replaces the old ``.encode("utf-8", errors="strict")`` check, which
        # could not stop already-mojibake'd *valid* Unicode.
        from .encoding_guard import guard_persist_content

        text = guard_persist_content(text)

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        event_key = event_id if event_stable else ""
        capture_key = event_key or f"capture:{uuid.uuid4().hex}"
        with self._transaction() as conn:
            # Cross-provider dedup: fold the append into the canonical row for
            # this external_id (B2) before deriving any session-scoped IDs.
            candidate_session_id = self._session_id(
                external_session_id, scope, active_provider
            )
            session_id = self._resolve_canonical_session(
                conn, candidate_session_id, external_session_id, scope, active_provider,
            )
            turn_hash = hashlib.sha256(
                f"{session_id}\x1f{capture_key}\x1f{content_hash}".encode("utf-8")
            ).hexdigest()[:24]
            turn_id = f"{session_id}-e{turn_hash}"
            safe_created_at = str(created_at or "")[:80]
            candidate_title, candidate_quality = _choose_session_title(
                explicit_title=title,
                messages=[{"role": role, "content": text}],
                provider=active_provider, occurred_at=safe_created_at or _now(),
                external_id=external_session_id, session_id=session_id,
            )
            existing_session = conn.execute(
                "SELECT title FROM conversation_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            existing_title = str(existing_session["title"] or "") if existing_session else ""
            existing_quality = 0 if _is_low_quality_title(
                existing_title, external_id=external_session_id, session_id=session_id,
            ) else (1 if existing_title.startswith(f"{_provider_label(active_provider)} 对话 · ") else 3)
            safe_title = (
                candidate_title
                if candidate_quality == 3 or candidate_quality > existing_quality
                else existing_title
            )
            conn.execute("""
                INSERT INTO conversation_sessions(
                  session_id, external_id, title, provider, agent_instance_id,
                  project_ref, share_group_id, created_at, imported_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(external_id, provider, agent_instance_id, project_ref, share_group_id)
                DO UPDATE SET
                  title=excluded.title,
                  imported_at=excluded.imported_at, deleted_at=''
            """, (session_id, external_session_id, safe_title, active_provider,
                  scope.agent_instance_id, scope.project_ref, scope.share_group_id,
                  safe_created_at, _now()))
            if safe_title != existing_title:
                conn.execute("UPDATE history_fts SET title=? WHERE session_id=?", (safe_title, session_id))
            existing = None
            if event_key:
                existing = conn.execute(
                    "SELECT turn_id,content_hash FROM conversation_turns "
                    "WHERE session_id=? AND event_key=?",
                    (session_id, event_key),
                ).fetchone()
            if existing is not None:
                conflict = existing["content_hash"] != content_hash
                result = {
                    "session_id": session_id, "turn_id": existing["turn_id"],
                    "inserted": False, "replayed": not conflict,
                    "event_conflict": conflict, "idempotency": "strict",
                }
                if shadow is not None:
                    try:
                        result["shadow"] = shadow.sync_turn(
                            external_session_id=external_session_id,
                            provider=active_provider,
                            role=role,
                            content=text,
                            event_id=event_id,
                            title=title,
                            created_at=created_at,
                            scope=scope,
                            max_turns=shadow_max_turns,
                            max_chars=shadow_max_chars,
                        )
                    except Exception as exc:
                        result["shadow"] = {"status": "failed", "code": type(exc).__name__}
                return result
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM conversation_turns WHERE session_id=?", (session_id,)
            ).fetchone()["count"]
            ordinal = int(count) + 1
            conn.execute("""
                INSERT INTO conversation_turns(
                  turn_id, session_id, ordinal, role, content, created_at,
                  content_type, event_key, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (turn_id, session_id, ordinal, str(role or "unknown")[:64], text,
                  safe_created_at, str(content_type or "text")[:64],
                  event_key, content_hash))
            row = conn.execute(
                "SELECT title FROM conversation_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            conn.execute("""
                INSERT INTO history_fts(session_id, turn_id, result_type, title, content)
                VALUES (?, ?, 'turn', ?, ?)
            """, (session_id, turn_id, row["title"] if row else safe_title, text))
            summary = f"{ordinal} turns archived; last event {str(created_at or _now())[:25]}"
            conn.execute("DELETE FROM history_fts WHERE session_id=? AND result_type='summary'", (session_id,))
            conn.execute("""
                INSERT INTO session_summaries(session_id, summary, summary_kind, updated_at)
                VALUES (?, ?, 'hook', ?)
                ON CONFLICT(session_id) DO UPDATE SET
                  summary=excluded.summary, summary_kind='hook', updated_at=excluded.updated_at
            """, (session_id, summary, _now()))
            conn.execute("""
                INSERT INTO history_fts(session_id, turn_id, result_type, title, content)
                VALUES (?, '', 'summary', ?, ?)
            """, (session_id, row["title"] if row else safe_title, summary))
        result = {
            "session_id": session_id, "turn_id": turn_id, "inserted": True,
            "replayed": False, "event_conflict": False,
            "idempotency": "strict" if event_key else "degraded",
        }
        if shadow is not None:
            try:
                result["shadow"] = shadow.sync_turn(
                    external_session_id=external_session_id,
                    provider=active_provider,
                    role=role,
                    content=text,
                    event_id=event_id,
                    title=title,
                    created_at=created_at,
                    scope=scope,
                    max_turns=shadow_max_turns,
                    max_chars=shadow_max_chars,
                )
            except Exception as exc:
                result["shadow"] = {"status": "failed", "code": type(exc).__name__}
        return result

    def add_observation(self, scope: HistoryScope, *, session_id: str, summary: str,
                        observation_type: str = "note", turn_id: str = "") -> str:
        """Store an indexed observation without promoting it to long-term memory."""
        if not summary or len(summary) > MAX_TURN_CHARS:
            raise ValueError("history_observation_invalid")
        where, args = self._owner_where(scope)
        observation_id = "obs-" + hashlib.sha256(f"{session_id}\x1f{turn_id}\x1f{summary}".encode()).hexdigest()[:24]
        with self._transaction() as conn:
            session = conn.execute(f"SELECT title FROM conversation_sessions s WHERE s.session_id=? AND {where}", [session_id, *args]).fetchone()
            if session is None: raise LookupError("history_session_not_found")
            existing = conn.execute(
                "SELECT 1 FROM observations WHERE observation_id=?", (observation_id,)
            ).fetchone()
            conn.execute("INSERT INTO observations(observation_id,session_id,turn_id,observation_type,summary,created_at) VALUES (?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET observation_type=excluded.observation_type", (observation_id, session_id, turn_id or None, observation_type[:64], summary, _now()))
            if existing is not None:
                return observation_id
            conn.execute("INSERT INTO history_fts(session_id,turn_id,result_type,title,content) VALUES (?,?,'observation',?,?)", (session_id, turn_id, session["title"], summary))
        return observation_id

    @staticmethod
    def _collapse_duplicate_sessions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Read-side dedup of dual-write rows for the SAME physical session.

        A conversation archived under two hosts (the append guard B2 is
        forward-only) can leave two session rows sharing one ``external_id``.
        Fold them at read time -- but only when the group shares the same
        agent instance and the same canonical project.  Rows that differ in
        agent or project are genuinely separate conversations and are kept.
        The first member of a folded group survives (caller's sort order),
        with ``duplicate_count`` reporting how many were absorbed.  Items
        without an ``external_id`` are never touched.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            external = str(item.get("external_id") or "")
            if external:
                grouped.setdefault(external, []).append(item)
        processed: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            external = str(item.get("external_id") or "")
            if not external:
                out.append(item)
                continue
            if external in processed:
                continue
            processed.add(external)
            members = grouped[external]
            if len(members) == 1:
                out.append(members[0])
                continue
            agent = str(members[0].get("agent_instance_id") or "")
            project_key = _project_metadata(str(members[0].get("project_ref") or ""))["project_key"]
            if all(
                str(m.get("agent_instance_id") or "") == agent
                and _project_metadata(str(m.get("project_ref") or ""))["project_key"] == project_key
                for m in members
            ):
                first = dict(members[0])
                first["duplicate_count"] = len(members) - 1
                out.append(first)
            else:
                out.extend(members)
        return out

    def list_sessions(self, scope: HistoryScope, *, limit: int = 50, offset: int = 0,
                      extracted: bool | None = None, date_from: str = "", date_to: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        where, args = self._scope_where(scope)
        if date_from:
            where += " AND COALESCE(s.created_at, s.imported_at) >= ?"; args.append(date_from)
        if date_to:
            where += " AND COALESCE(s.created_at, s.imported_at) <= ?"; args.append(date_to)
        evidence_clause = ""
        if extracted is not None:
            evidence_clause = " HAVING evidence_count " + ("> 0" if extracted else "= 0")
        query = f"""
          SELECT s.session_id, s.external_id, s.title, s.provider, s.agent_instance_id, s.project_ref, s.share_group_id,
                 s.created_at, s.imported_at, COALESCE(ss.summary, '') AS summary,
                 COUNT(DISTINCT t.turn_id) AS turn_count,
                 COUNT(DISTINCT CASE WHEN e.status='valid' THEN e.link_id END) AS evidence_count
          FROM conversation_sessions s
          LEFT JOIN conversation_turns t ON t.session_id=s.session_id
          LEFT JOIN session_summaries ss ON ss.session_id=s.session_id
          LEFT JOIN evidence_links e ON e.session_id=s.session_id
          WHERE {where}
          GROUP BY s.session_id{evidence_clause}
          ORDER BY COALESCE(s.created_at, s.imported_at) DESC, s.session_id DESC LIMIT ? OFFSET ?
        """
        # Count the same grouped rows as the page query.  Counting the base
        # sessions table would ignore the ``extracted`` HAVING clause and make
        # graph/category totals disagree with the visible scoped list.
        count_query = f"""
          SELECT COUNT(*) AS total FROM (
            SELECT s.session_id,
                   COUNT(DISTINCT CASE WHEN e.status='valid' THEN e.link_id END) AS evidence_count
            FROM conversation_sessions s
            LEFT JOIN evidence_links e ON e.session_id=s.session_id
            WHERE {where}
            GROUP BY s.session_id{evidence_clause}
          ) scoped_sessions
        """
        with self._connect() as conn:
            rows = conn.execute(query, [*args, limit, offset]).fetchall()
            total = int(conn.execute(count_query, args).fetchone()["total"])
            project_where = where
            project_args = list(args)
            if extracted is not None:
                predicate = "EXISTS" if extracted else "NOT EXISTS"
                project_where += (
                    " AND " + predicate + " (SELECT 1 FROM evidence_links pe "
                    "WHERE pe.session_id=s.session_id AND pe.status='valid')"
                )
            project_rows = conn.execute(
                f"SELECT s.project_ref,s.agent_instance_id,"
                "COUNT(*) AS session_count,MAX(COALESCE(s.created_at,s.imported_at)) AS latest_at "
                f"FROM conversation_sessions s WHERE {project_where} "
                "GROUP BY s.project_ref,s.agent_instance_id",
                project_args,
            ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for row in project_rows:
            meta = _project_metadata(str(row["project_ref"] or ""))
            group = groups.setdefault(meta["project_key"], {
                **meta, "session_count": 0, "agents": [], "latest_at": "",
            })
            agent = str(row["agent_instance_id"] or "")
            group["session_count"] += int(row["session_count"] or 0)
            if agent and agent not in group["agents"]:
                group["agents"].append(agent)
            group["latest_at"] = max(str(group["latest_at"] or ""), str(row["latest_at"] or ""))
        collapsed = self._collapse_duplicate_sessions([dict(row) for row in rows])
        return {
            "sessions": [_safe_session_projection(item) for item in collapsed],
            "project_groups": sorted(groups.values(), key=lambda item: (item["latest_at"], item["project_key"]), reverse=True),
            "total": total, "limit": limit, "offset": offset,
        }

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", query or "")[:12]
        if not tokens:
            raise ValueError("history_query_required")
        return " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)

    def search(self, scope: HistoryScope, query: str, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """First stage: no raw turn body, only identifiers and bounded summaries."""
        limit = max(1, min(int(limit), MAX_PAGE)); offset = max(0, int(offset))
        where, args = self._scope_where(scope)
        fts_query = self._fts_query(query)
        sql = f"""
          SELECT h.session_id, s.external_id, h.turn_id, h.result_type, s.title, s.provider, s.agent_instance_id, s.project_ref,
                 s.created_at, COALESCE(ss.summary, '') AS summary, t.role, t.created_at AS turn_created_at,
                 t.content_type, snippet(history_fts,4,'','',' … ',18) AS matched_summary
          FROM history_fts h
          JOIN conversation_sessions s ON s.session_id=h.session_id
          LEFT JOIN conversation_turns t ON t.turn_id=h.turn_id
          LEFT JOIN session_summaries ss ON ss.session_id=s.session_id
          WHERE history_fts MATCH ? AND {where}
          ORDER BY bm25(history_fts), COALESCE(t.created_at, s.imported_at) DESC LIMIT ? OFFSET ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, [fts_query, *args, limit, offset]).fetchall()
        results = []
        for row in rows:
            item = _safe_session_projection(dict(row))
            item["matched_summary"] = _short(item.get("matched_summary", ""), MAX_MATCHED_SUMMARY_CHARS)
            anchor = str(item.get("turn_id") or "")
            item["anchor_turn_id"] = anchor
            item["can_timeline"] = bool(anchor)
            item["read_target"] = "turn" if anchor else "session"
            results.append(item)
        results = self._collapse_duplicate_sessions(results)
        return {"query": query, "results": results, "limit": limit, "offset": offset}

    def timeline(self, scope: HistoryScope, session_id: str, anchor_turn_id: str, *, radius: int = 4) -> dict[str, Any]:
        radius = max(0, min(int(radius), MAX_TIMELINE_RADIUS))
        where, args = self._scope_where(scope)
        with self._connect() as conn:
            anchor = conn.execute(f"""
              SELECT t.ordinal FROM conversation_turns t JOIN conversation_sessions s ON s.session_id=t.session_id
              WHERE t.turn_id=? AND t.session_id=? AND {where}
            """, [anchor_turn_id, session_id, *args]).fetchone()
            if anchor is None:
                raise LookupError("history_anchor_not_found")
            rows = conn.execute("""
              SELECT turn_id, ordinal, role, created_at, content_type, content FROM conversation_turns
              WHERE session_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal
            """, (session_id, anchor["ordinal"] - radius, anchor["ordinal"] + radius)).fetchall()
        return {"session_id": session_id, "anchor_turn_id": anchor_turn_id, "radius": radius,
                "turns": [{**{k: row[k] for k in ("turn_id", "ordinal", "role", "created_at", "content_type")}, "content_preview": _short(row["content"])} for row in rows]}

    def read(self, scope: HistoryScope, *, session_id: str = "", turn_id: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Final stage. Returns full raw text only for one authorized record/session."""
        if bool(session_id) == bool(turn_id):
            raise ValueError("exactly_one_of_session_id_or_turn_id_required")
        where, args = self._scope_where(scope)
        limit = max(1, min(int(limit), 250)); offset = max(0, int(offset))
        with self._connect() as conn:
            if turn_id:
                row = conn.execute(f"""
                  SELECT t.turn_id,t.session_id,t.ordinal,t.role,t.content,t.created_at,t.content_type,s.title,s.provider,s.project_ref
                  FROM conversation_turns t JOIN conversation_sessions s ON s.session_id=t.session_id
                  WHERE t.turn_id=? AND {where}
                """, [turn_id, *args]).fetchone()
                if row is None: raise LookupError("history_turn_not_found")
                return {"turn": dict(row)}
            session = conn.execute(f"SELECT s.session_id,s.title,s.provider,s.project_ref,s.created_at,COALESCE(ss.summary,'') summary FROM conversation_sessions s LEFT JOIN session_summaries ss ON ss.session_id=s.session_id WHERE s.session_id=? AND {where}", [session_id, *args]).fetchone()
            if session is None: raise LookupError("history_session_not_found")
            rows = conn.execute("SELECT turn_id,ordinal,role,content,created_at,content_type FROM conversation_turns WHERE session_id=? ORDER BY ordinal LIMIT ? OFFSET ?", (session_id, limit, offset)).fetchall()
        return {"session": dict(session), "turns": [dict(r) for r in rows], "limit": limit, "offset": offset}

    def extract_preview(self, scope: HistoryScope, session_id: str, *, turn_ids: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        """Explicit candidate preview.  It never writes long-term memory."""
        where, args = self._scope_where(scope)
        limit = max(1, min(int(limit), MAX_PAGE))
        with self._connect() as conn:
            authorized = conn.execute(f"SELECT s.title,s.provider FROM conversation_sessions s WHERE s.session_id=? AND {where}", [session_id, *args]).fetchone()
            if authorized is None: raise LookupError("history_session_not_found")
            params: list[Any] = [session_id]
            extra = ""
            if turn_ids:
                cleaned = [str(t) for t in turn_ids[:limit] if str(t)]
                if not cleaned: raise ValueError("turn_ids_required")
                extra = " AND turn_id IN (" + ",".join("?" for _ in cleaned) + ")"; params.extend(cleaned)
            rows = conn.execute("SELECT turn_id,role,content,created_at FROM conversation_turns WHERE session_id=?" + extra + " ORDER BY ordinal LIMIT ?", [*params, limit]).fetchall()
        candidates = []
        for row in rows:
            text = _short(row["content"], 1200)
            if not text: continue
            import hashlib
            candidates.append({"title": _short(authorized["title"], 80), "body": text,
                               "kind_hint": "episode", "confidence": 0.3,
                               "evidence": {"session_id": session_id, "turn_id": row["turn_id"],
                                            "provider": authorized["provider"], "created_at": row["created_at"]},
                               # Same structural fields as MemoryRecord.Provenance;
                               # only an explicit governance action may turn this
                               # preview into a record and add an evidence_link.
                               "provenance": [{"source_object_id": session_id,
                                               "locator": f"history:turn:{row['turn_id']}",
                                               "excerpt_hash": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()}]})
        return {"session_id": session_id, "candidates": candidates, "written_to_long_term_memory": False}

    def add_evidence_link(self, *, memory_id: str, session_id: str, turn_id: str = "") -> str:
        link_id = "evi-" + hashlib.sha256(f"{memory_id}\x1f{session_id}\x1f{turn_id}".encode()).hexdigest()[:24]
        with self._transaction() as conn:
            session = conn.execute(
                "SELECT 1 FROM conversation_sessions WHERE session_id=? AND deleted_at=''",
                (session_id,),
            ).fetchone()
            if session is None:
                raise LookupError("history_session_not_found")
            if turn_id and conn.execute(
                "SELECT 1 FROM conversation_turns WHERE session_id=? AND turn_id=?",
                (session_id, turn_id),
            ).fetchone() is None:
                raise LookupError("history_turn_not_found")
            conn.execute("INSERT INTO evidence_links(link_id,memory_id,session_id,turn_id,status,created_at,invalidated_at) VALUES (?, ?, ?, ?, 'valid', ?, '') ON CONFLICT(link_id) DO UPDATE SET status='valid', invalidated_at=''", (link_id, memory_id, session_id, turn_id or None, _now()))
        return link_id

    def export(self, scope: HistoryScope, *, session_ids: list[str]) -> dict[str, Any]:
        if not session_ids: raise ValueError("session_ids_required")
        exported = []
        for session_id in session_ids[:MAX_PAGE]:
            exported.append(self.read(scope, session_id=session_id))
        return {"format": "memoryguard-history-v1", "exported_at": _now(), "sessions": exported}

    def delete(
        self,
        scope: HistoryScope,
        *,
        session_ids: list[str],
        invalidate_evidence: bool = False,
        idempotency_key: str = "",
        operation_digest: str = "",
    ) -> dict[str, int | bool]:
        if not session_ids:
            raise ValueError("history_delete_scope_required")
        unique = list(dict.fromkeys(str(x) for x in session_ids if str(x)))[:MAX_PAGE]
        if not unique: raise ValueError("history_delete_scope_required")
        where, args = self._owner_where(scope)
        deleted = invalidated = 0
        with self._transaction() as conn:
            key = str(idempotency_key or "")
            digest = str(operation_digest or "")
            if key:
                if not digest:
                    raise ValueError("history_delete_idempotency_digest_required")
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_mutation_receipts'"
                ).fetchone()
                if table is None:
                    # Never perform a destructive mutation without a durable
                    # replay fence.  The caller can surface this as a stable
                    # blocker instead of pretending process-local idempotency.
                    raise ValueError("durable_idempotency_unavailable")
                prior = conn.execute(
                    "SELECT operation,payload_digest,result_json FROM history_mutation_receipts WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if prior is not None:
                    if prior["operation"] != "delete" or prior["payload_digest"] != digest:
                        raise ValueError("mutation_idempotency_conflict")
                    try:
                        replay = json.loads(prior["result_json"])
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError("history_mutation_receipt_corrupt") from exc
                    replay["idempotent_replay"] = True
                    return replay
            for session_id in unique:
                row = conn.execute(f"SELECT session_id FROM conversation_sessions s WHERE s.session_id=? AND {where}", [session_id, *args]).fetchone()
                if row is None: continue
                # A raw source may be removed only with a durable evidence
                # tombstone.  The legacy flag remains API-compatible, but
                # deletion always invalidates valid links atomically.
                cur = conn.execute("UPDATE evidence_links SET status='invalid', invalidated_at=? WHERE session_id=? AND status='valid'", (_now(), session_id)); invalidated += cur.rowcount
                conn.execute("DELETE FROM history_fts WHERE session_id=?", (session_id,))
                cur = conn.execute("DELETE FROM conversation_sessions WHERE session_id=?", (session_id,)); deleted += cur.rowcount
            result: dict[str, int | bool] = {
                "deleted_sessions": deleted,
                "invalidated_evidence_links": invalidated,
                "long_term_memories_deleted": 0,
            }
            if key:
                result["idempotent_replay"] = False
                conn.execute(
                    "INSERT INTO history_mutation_receipts(idempotency_key,operation,payload_digest,result_json,created_at) VALUES (?,?,?,?,?)",
                    (key, "delete", digest, json.dumps(result, sort_keys=True, separators=(",", ":")), _now()),
                )
            return result
