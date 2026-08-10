"""V2 scenario/profile projection storage.

Projection rows are rebuildable views.  They contain only identifiers, hashes,
ACL metadata and small derived labels; source text never belongs in this DB.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Iterable, Iterator, Mapping, Sequence

from ..storage.database import execute_sql_script, open_database
from ..storage.layout import WorkspaceV2Layout
from ..storage.schema import initialize_database
from ..storage.transaction import transaction


PROJECTION_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_MARKER = "memoryguard-v2-phase3-projection"
_KINDS = {"scenario": ("scenario_projections", "scenario_key"), "profile": ("profile_projections", "profile_key")}
_UNKNOWN = "__UNKNOWN__"
_FORBIDDEN_KEYS = frozenset(
    {
        "body", "raw", "raw_content", "content", "text", "document",
        "document_body", "conversation", "conversation_body",
        "full_transcript", "transcript", "raw_text", "source_text",
        "payload", "full_content", "content_body",
        # Projection metadata is derived presentation only.  Authority,
        # identity and access-control decisions remain in their owning V2
        # domains and must never be smuggled into this payload.
        "authority", "authorities", "admin", "administrator",
        "permission", "permissions", "capability", "capabilities",
        "role", "roles", "effect", "effects", "grant", "grants",
        "deny", "denies", "allow", "allows", "access", "acl",
        "policy", "policy_class", "visibility", "principal", "subject",
        "is_admin", "admin_flag",
        "scope", "scope_key", "workspace", "workspace_id", "agent_instance_id",
        "project_ref", "provider", "share_group_id", "sensitivity",
    }
)
_MAX_METADATA_DEPTH = 8
_MAX_PAYLOAD_BYTES = 64 * 1024
_CONTROL_KEY_TOKENS = frozenset(
    {
        "authority", "admin", "administrator", "permission", "capability",
        "role", "effect", "grant", "deny", "allow", "access", "acl",
        "policy", "scope", "visibility", "principal", "subject",
    }
)


class ProjectionError(RuntimeError):
    """Projection input or storage failed closed."""


class ProjectionSchemaError(ProjectionError):
    """Projection marker/schema is unsupported or unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_projection_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(item) for item in (prefix, *parts))
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _assert_no_reparse(path: str | Path) -> None:
    raw = Path(path).expanduser()
    current = Path(os.path.abspath(os.fspath(raw)))
    while True:
        try:
            exists = current.exists() or current.is_symlink()
        except OSError as exc:
            raise ProjectionError(f"cannot inspect projection workspace: {current}") from exc
        if exists:
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                info = None
            except OSError as exc:
                raise ProjectionError(f"cannot inspect projection workspace: {current}") from exc
            if info is not None and (
                stat.S_ISLNK(info.st_mode)
                or bool(getattr(info, "st_file_attributes", 0) & 0x0400)
            ):
                raise ProjectionError(f"projection workspace cannot contain symlink or reparse point: {current}")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _clean_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _is_forbidden_key(value: Any) -> bool:
    key = _clean_key(value)
    if key in _FORBIDDEN_KEYS:
        return True
    return any(key.startswith(f"{token}_") or key.endswith(f"_{token}") for token in _CONTROL_KEY_TOKENS)


def _validate_payload(value: Mapping[str, Any] | None, *, label: str = "payload") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProjectionError(f"{label} must be a JSON object")

    def walk(item: Any, depth: int, path: str) -> Any:
        if depth > _MAX_METADATA_DEPTH:
            raise ProjectionError(f"{label} exceeds maximum nesting depth at {path}")
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in item.items():
                key = str(raw_key)
                if _is_forbidden_key(key):
                    raise ProjectionError(f"{label} contains forbidden source field: {key}")
                result[key] = walk(child, depth + 1, f"{path}.{key}" if path else key)
            return result
        if isinstance(item, (list, tuple)):
            return [walk(child, depth + 1, f"{path}[{i}]") for i, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise ProjectionError(f"{label} contains unsupported value at {path}")

    result = walk(value, 0, "")
    assert isinstance(result, dict)
    if len(_json(result).encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ProjectionError(f"{label} exceeds 64 KiB")
    return result


@dataclass(frozen=True)
class ProjectionReadScope:
    workspace_id: str
    agent_instance_id: str = ""
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    sensitivity: str = "normal"
    policy_class: str = "private"

    def __post_init__(self) -> None:
        for field_name in (
            "workspace_id", "agent_instance_id", "project_ref", "provider",
            "share_group_id", "sensitivity", "policy_class",
        ):
            value = getattr(self, field_name)
            if value is None:
                raise ValueError(f"{field_name} must be explicit")
            if not isinstance(value, str):
                object.__setattr__(self, field_name, str(value))
        if not self.workspace_id:
            raise ValueError("workspace_id is required")

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.workspace_id,
            self.agent_instance_id,
            self.project_ref,
            self.provider,
            self.share_group_id,
            self.sensitivity,
            self.policy_class,
        )


@dataclass(frozen=True)
class ProjectionRecord:
    projection_id: str
    kind: str
    key: str
    generation: int
    source_digest: str
    projection_digest: str
    status: str
    payload: Mapping[str, Any]
    scope: ProjectionReadScope
    evidence_links: tuple[Mapping[str, Any], ...]


PROJECTION_AUX_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_items (
    item_id TEXT PRIMARY KEY,
    projection_id TEXT NOT NULL,
    atom_id TEXT NOT NULL,
    atom_hash TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(projection_id) REFERENCES {projection_table}(projection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS projection_evidence_links (
    link_id TEXT PRIMARY KEY,
    projection_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports',
    created_at TEXT NOT NULL,
    UNIQUE(projection_id, evidence_id, relation),
    FOREIGN KEY(projection_id) REFERENCES {projection_table}(projection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS projection_acl (
    acl_id TEXT PRIMARY KEY,
    projection_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    policy_class TEXT NOT NULL DEFAULT 'private',
    UNIQUE(projection_id, workspace_id, agent_instance_id, project_ref, provider, share_group_id, sensitivity, policy_class),
    FOREIGN KEY(projection_id) REFERENCES {projection_table}(projection_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS projection_heads (
    head_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL,
    projection_key TEXT NOT NULL,
    current_projection_id TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(projection_kind, projection_key)
);
CREATE TABLE IF NOT EXISTS projection_head_events (
    event_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL,
    projection_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    projection_id TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_tombstones (
    tombstone_id TEXT PRIMARY KEY,
    projection_kind TEXT NOT NULL,
    projection_key TEXT NOT NULL,
    projection_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projection_ledger (
    ledger_id TEXT PRIMARY KEY,
    source_ref TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(source_ref, code, detail)
);
CREATE INDEX IF NOT EXISTS idx_projection_acl_scope ON projection_acl(workspace_id, agent_instance_id, project_ref, provider, share_group_id);
CREATE INDEX IF NOT EXISTS idx_projection_items_projection ON projection_items(projection_id);
"""


class ProjectionStore:
    """Immutable-generation scenario/profile projection store."""

    SCHEMA_VERSION = PROJECTION_SCHEMA_VERSION
    SCHEMA_MARKER = PROJECTION_SCHEMA_MARKER

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        initialize: bool = True,
        source_workspace: str | Path | None = None,
    ) -> None:
        if isinstance(workspace, WorkspaceV2Layout):
            # WorkspaceV2Layout.__post_init__ resolves its input and therefore
            # cannot prove that the original lexical path was not a symlink.
            # Require the caller to retain/pass that raw path explicitly;
            # plain path callers continue to use the normal API unchanged.
            raw_workspace = source_workspace or getattr(workspace, "source_workspace", None)
            if raw_workspace is None:
                raise ProjectionError(
                    "WorkspaceV2Layout input requires source_workspace for lexical containment validation"
                )
            _assert_no_reparse(raw_workspace)
            checked_layout = WorkspaceV2Layout(Path(raw_workspace))
            if checked_layout.workspace != workspace.workspace:
                raise ProjectionError("WorkspaceV2Layout source_workspace does not match resolved workspace")
            self.layout = workspace
        else:
            _assert_no_reparse(workspace)
            self.layout = WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.db_paths = {"scenario": self.layout.scenario_db, "profile": self.layout.profile_db}
        if initialize:
            self.layout.ensure_dirs()
            for kind, path in self.db_paths.items():
                state = self._preflight(path, kind)
                if state != "current":
                    initialize_database(path, "projection", layout=self.layout)
                    self._ensure_schema(path, kind)

    @staticmethod
    def _table_set(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _preflight(self, path: Path, kind: str) -> str:
        if not path.is_file():
            return "fresh"
        table_name = _KINDS[kind][0]
        try:
            with open_database(path, readonly=True) as conn:
                tables = self._table_set(conn)
                if "projection_schema_meta" not in tables:
                    if tables & {"projection_items", "projection_evidence_links", "projection_acl", "projection_heads", "projection_tombstones", "projection_ledger"}:
                        raise ProjectionSchemaError("projection aux schema marker missing")
                    return "needs_aux"
                rows = conn.execute("SELECT key,value FROM projection_schema_meta ORDER BY key").fetchall()
                if len(rows) != 1 or str(rows[0][0]) != "version":
                    raise ProjectionSchemaError("unknown projection schema marker")
                marker = str(rows[0][1])
                if marker != str(PROJECTION_SCHEMA_VERSION):
                    direction = "future" if marker.isdigit() and int(marker) > PROJECTION_SCHEMA_VERSION else "unsupported"
                    raise ProjectionSchemaError(f"{direction} projection schema version: {marker!r}")
                required = {"projection_schema_meta", table_name, "projection_items", "projection_evidence_links", "projection_acl", "projection_heads", "projection_head_events", "projection_tombstones", "projection_ledger"}
                missing = sorted(required - tables)
                if missing:
                    raise ProjectionSchemaError("projection marker current but tables missing: " + ",".join(missing))
            initialize_database(path, "projection", layout=self.layout, readonly=True)
            return "current"
        except ProjectionError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise ProjectionSchemaError(f"cannot preflight projection DB: {path}") from exc

    def _ensure_schema(self, path: Path, kind: str) -> None:
        table_name = _KINDS[kind][0]
        script = PROJECTION_AUX_SCHEMA.replace("{projection_table}", table_name)
        with open_database(path) as conn:
            with transaction(conn):
                execute_sql_script(conn, script)
                conn.execute(
                    "INSERT INTO projection_schema_meta(key,value) VALUES('version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(PROJECTION_SCHEMA_VERSION),),
                )

    def _kind(self, kind: str) -> tuple[str, str]:
        normalized = str(kind).strip().lower()
        if normalized not in _KINDS:
            raise ValueError(f"unknown projection kind: {kind!r}")
        return _KINDS[normalized]

    @contextmanager
    def connection(self, kind: str = "scenario") -> Iterator[sqlite3.Connection]:
        normalized = str(kind).strip().lower()
        if normalized not in self.db_paths:
            raise ValueError(f"unknown projection kind: {kind!r}")
        with open_database(self.db_paths[normalized], readonly=True) as conn:
            yield conn

    def _scope_ok(self, scope: ProjectionReadScope | None) -> bool:
        if not isinstance(scope, ProjectionReadScope):
            return False
        if _UNKNOWN in scope.as_tuple():
            return False
        # A projection DB is workspace-local; accepting another workspace ID
        # would turn an exact ACL match into a cross-workspace read/write.
        return bool(scope.workspace_id) and scope.workspace_id == str(self.workspace)

    @staticmethod
    def _scope_digest(scope: ProjectionReadScope) -> str:
        return _digest(scope.as_tuple())

    def _row_to_record(self, conn: sqlite3.Connection, kind: str, row: sqlite3.Row, scope: ProjectionReadScope) -> ProjectionRecord:
        table_name = _KINDS[kind][0]
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ProjectionError("projection payload is malformed")
        links = conn.execute(
            "SELECT evidence_id,evidence_hash,relation FROM projection_evidence_links WHERE projection_id=? ORDER BY evidence_id,relation",
            (str(row["projection_id"]),),
        ).fetchall()
        if not links or any(not str(link[0]) or not str(link[1]) for link in links):
            raise ProjectionError("projection evidence links are incomplete")
        return ProjectionRecord(
            projection_id=str(row["projection_id"]),
            kind=kind,
            key=str(row[_KINDS[kind][1]]),
            generation=int(row["generation"]),
            source_digest=str(row["source_digest"] or ""),
            projection_digest=str(row["projection_digest"] or ""),
            status=str(row["status"]),
            payload=payload if isinstance(payload, Mapping) else {},
            scope=scope,
            evidence_links=tuple({"evidence_id": str(link[0]), "evidence_hash": str(link[1]), "relation": str(link[2])} for link in links),
        )

    @staticmethod
    def _next_generation(
        conn: sqlite3.Connection,
        table_name: str,
        key_column: str,
        key: str,
        head_generation: int | None,
    ) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(generation),-1) FROM {table_name} WHERE {key_column}=?",
            (str(key),),
        ).fetchone()
        row_generation = int(row[0]) if row is not None else -1
        return max(-1 if head_generation is None else int(head_generation), row_generation) + 1

    def put_projection(
        self,
        kind: str,
        key: str,
        *,
        source_digest: str,
        payload: Mapping[str, Any],
        scope: ProjectionReadScope,
        evidence_links: Sequence[Mapping[str, Any] | str],
        item_refs: Sequence[Mapping[str, Any]] = (),
        projection_digest: str = "",
        status: str = "ready",
        fail_at: str | None = None,
    ) -> ProjectionRecord:
        table_name, key_column = self._kind(kind)
        normalized_kind = str(kind).strip().lower()
        if not self._scope_ok(scope):
            raise ProjectionError("invalid or unknown projection ACL scope")
        if status != "ready":
            raise ProjectionError("only ready immutable projections may be published")
        safe_payload = _validate_payload(payload)
        links: list[dict[str, str]] = []
        seen_links: set[tuple[str, str]] = set()
        for raw in evidence_links:
            if isinstance(raw, str):
                evidence_id, evidence_hash, relation = raw, "", "supports"
            elif isinstance(raw, Mapping):
                evidence_id = str(raw.get("evidence_id") or raw.get("id") or "")
                evidence_hash = str(raw.get("evidence_hash") or raw.get("digest") or raw.get("hash") or "")
                relation = str(raw.get("relation") or "supports")
            else:
                raise ProjectionError("evidence link must be an ID or object")
            if not evidence_id or not evidence_hash:
                raise ProjectionError("projection requires non-empty evidence_id and evidence_hash")
            link_key = (evidence_id, relation)
            if link_key in seen_links:
                raise ProjectionError("duplicate evidence_id+relation in projection links")
            seen_links.add(link_key)
            links.append({"evidence_id": evidence_id, "evidence_hash": evidence_hash, "relation": relation})
        if not links:
            raise ProjectionError("every projection requires at least one evidence link")
        refs: list[dict[str, str]] = []
        links_by_id = {item["evidence_id"]: item for item in links}
        for raw in item_refs:
            if not isinstance(raw, Mapping):
                raise ProjectionError("projection item reference must be an object")
            atom_id = str(raw.get("atom_id") or raw.get("memory_id") or "")
            atom_hash = str(raw.get("atom_hash") or raw.get("canonical_hash") or raw.get("hash") or "")
            evidence_id = str(raw.get("evidence_id") or (links[0]["evidence_id"] if len(links) == 1 else ""))
            link = links_by_id.get(evidence_id)
            if not atom_id or not evidence_id or link is None:
                raise ProjectionError("projection item requires atom_id and an evidence link in this projection")
            raw_evidence_hash = raw.get("evidence_hash")
            if raw_evidence_hash is None:
                raw_evidence_hash = raw.get("digest")
            if raw_evidence_hash is None:
                raw_evidence_hash = raw.get("hash")
            evidence_hash = str(link["evidence_hash"] if raw_evidence_hash is None else raw_evidence_hash)
            if not evidence_hash or evidence_hash != link["evidence_hash"]:
                raise ProjectionError("projection item evidence_hash must match its evidence link")
            refs.append({"atom_id": atom_id, "atom_hash": atom_hash, "evidence_id": evidence_id, "evidence_hash": evidence_hash})
        if projection_digest == "":
            projection_digest = _digest({"payload": safe_payload, "links": links, "items": refs, "scope": scope.as_tuple()})
        source_digest = str(source_digest)
        projection_digest = str(projection_digest)
        now = _now()
        with open_database(self.db_paths[normalized_kind]) as conn:
            with transaction(conn):
                head = conn.execute(
                    "SELECT current_projection_id,generation FROM projection_heads WHERE projection_kind=? AND projection_key=?",
                    (normalized_kind, str(key)),
                ).fetchone()
                if head is not None and str(head[0]):
                    existing = conn.execute(
                        f"SELECT * FROM {table_name} WHERE projection_id=?",
                        (str(head[0]),),
                    ).fetchone()
                    if existing is None or str(existing[key_column]) != str(key):
                        raise ProjectionError("projection head points outside its key domain")
                    acl = conn.execute(
                        "SELECT 1 FROM projection_acl WHERE projection_id=? AND workspace_id=? AND agent_instance_id=? AND project_ref=? AND provider=? AND share_group_id=? AND sensitivity=? AND policy_class=?",
                        (str(head[0]), *scope.as_tuple()),
                    ).fetchone()
                    if acl is not None and str(existing["source_digest"]) == source_digest and str(existing["projection_digest"]) == projection_digest and str(existing["status"]) == "ready":
                        return self._row_to_record(conn, normalized_kind, existing, scope)
                head_generation = int(head[1]) if head is not None else None
                generation = self._next_generation(conn, table_name, key_column, str(key), head_generation)
                projection_id = stable_projection_id("projection", normalized_kind, key, generation, projection_digest)
                conn.execute(
                    f"INSERT INTO {table_name}(projection_id,{key_column},generation,source_digest,projection_digest,status,payload_json,error_json,created_at,updated_at) VALUES(?,?,?,?,?,'ready',?,'{{}}',?,?)",
                    (projection_id, str(key), generation, source_digest, projection_digest, _json(safe_payload), now, now),
                )
                if fail_at == "after_projection":
                    raise ProjectionError("injected projection failure after projection")
                for index, link in enumerate(links):
                    conn.execute(
                        "INSERT INTO projection_evidence_links(link_id,projection_id,evidence_id,evidence_hash,relation,created_at) VALUES(?,?,?,?,?,?)",
                        (stable_projection_id("projection-link", projection_id, link["evidence_id"], link["relation"]), projection_id, link["evidence_id"], link["evidence_hash"], link["relation"], now),
                    )
                if fail_at == "after_links":
                    raise ProjectionError("injected projection failure after links")
                for index, ref in enumerate(refs):
                    conn.execute(
                        "INSERT INTO projection_items(item_id,projection_id,atom_id,atom_hash,evidence_id,evidence_hash,metadata_json) VALUES(?,?,?,?,?,?,?)",
                        (stable_projection_id("projection-item", projection_id, index, ref["atom_id"]), projection_id, ref["atom_id"], ref["atom_hash"], ref["evidence_id"], ref["evidence_hash"], "{}"),
                    )
                acl_id = stable_projection_id("projection-acl", projection_id, *scope.as_tuple())
                conn.execute(
                    "INSERT INTO projection_acl(acl_id,projection_id,workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class) VALUES(?,?,?,?,?,?,?,?,?)",
                    (acl_id, projection_id, *scope.as_tuple()),
                )
                head_id = stable_projection_id("projection-head", normalized_kind, key)
                conn.execute(
                    "INSERT INTO projection_heads(head_id,projection_kind,projection_key,current_projection_id,generation,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(projection_kind,projection_key) DO UPDATE SET current_projection_id=excluded.current_projection_id,generation=excluded.generation,updated_at=excluded.updated_at",
                    (head_id, normalized_kind, str(key), projection_id, generation, now),
                )
                conn.execute(
                    "INSERT INTO projection_head_events(event_id,projection_kind,projection_key,event_type,projection_id,generation,created_at) VALUES(?,?,?,?,?,?,?)",
                    (stable_projection_id("projection-event", normalized_kind, key, generation, projection_id), normalized_kind, str(key), "publish", projection_id, generation, now),
                )
                row = conn.execute(f"SELECT * FROM {table_name} WHERE projection_id=?", (projection_id,)).fetchone()
                assert row is not None
                return self._row_to_record(conn, normalized_kind, row, scope)

    upsert_projection = put_projection

    def get_projection(
        self,
        kind: str,
        key: str,
        *,
        scope: ProjectionReadScope | None = None,
    ) -> ProjectionRecord | None:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in self.db_paths or not self._scope_ok(scope):
            return None
        table_name, key_column = _KINDS[normalized_kind]
        assert scope is not None
        with self.connection(normalized_kind) as conn:
            row = conn.execute(
                f"SELECT p.* FROM {table_name} p JOIN projection_heads h ON h.current_projection_id=p.projection_id JOIN projection_acl a ON a.projection_id=p.projection_id "
                f"WHERE h.projection_kind=? AND h.projection_key=? AND p.{_KINDS[normalized_kind][1]}=h.projection_key AND p.status='ready' AND a.workspace_id=? AND a.agent_instance_id=? AND a.project_ref=? AND a.provider=? AND a.share_group_id=? AND a.sensitivity=? AND a.policy_class=? LIMIT 1",
                (normalized_kind, str(key), *scope.as_tuple()),
            ).fetchone()
            return self._row_to_record(conn, normalized_kind, row, scope) if row is not None else None

    read = get_projection

    def tombstone(self, kind: str, key: str, *, reason: str = "deleted") -> str:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in self.db_paths:
            raise ValueError(f"unknown projection kind: {kind!r}")
        table_name, key_column = self._kind(normalized_kind)
        now = _now()
        with open_database(self.db_paths[normalized_kind]) as conn:
            with transaction(conn):
                head = conn.execute("SELECT current_projection_id,generation FROM projection_heads WHERE projection_kind=? AND projection_key=?", (normalized_kind, str(key))).fetchone()
                old_id = str(head[0]) if head is not None else ""
                if old_id:
                    pointed = conn.execute(
                        f"SELECT {key_column} FROM {table_name} WHERE projection_id=?",
                        (old_id,),
                    ).fetchone()
                    if pointed is None or str(pointed[0]) != str(key):
                        raise ProjectionError("projection head points outside its key domain")
                # Repeating the same tombstone while the head is already
                # empty is an identical-only no-op: no new generation/event.
                if head is not None and not old_id:
                    previous = conn.execute(
                        "SELECT tombstone_id,reason FROM projection_tombstones WHERE projection_kind=? AND projection_key=? ORDER BY rowid DESC LIMIT 1",
                        (normalized_kind, str(key)),
                    ).fetchone()
                    if previous is not None and str(previous[1]) == str(reason):
                        return str(previous[0])
                head_generation = int(head[1]) if head is not None else None
                generation = self._next_generation(conn, table_name, key_column, str(key), head_generation)
                tombstone_id = stable_projection_id("projection-tombstone", normalized_kind, key, generation, reason)
                conn.execute("INSERT INTO projection_tombstones(tombstone_id,projection_kind,projection_key,projection_id,reason,created_at) VALUES(?,?,?,?,?,?)", (tombstone_id, normalized_kind, str(key), old_id, str(reason), now))
                head_id = stable_projection_id("projection-head", normalized_kind, key)
                conn.execute("INSERT INTO projection_heads(head_id,projection_kind,projection_key,current_projection_id,generation,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(projection_kind,projection_key) DO UPDATE SET current_projection_id='',generation=excluded.generation,updated_at=excluded.updated_at", (head_id, normalized_kind, str(key), "", generation, now))
                conn.execute("INSERT INTO projection_head_events(event_id,projection_kind,projection_key,event_type,projection_id,generation,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (stable_projection_id("projection-event", normalized_kind, key, generation, tombstone_id), normalized_kind, str(key), "tombstone", "", generation, str(reason), now))
                return tombstone_id

    delete = tombstone

    def rollback(
        self,
        kind: str,
        key: str,
        projection_id: str,
        *,
        reason: str = "rollback",
        scope: ProjectionReadScope | None = None,
    ) -> str:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in self.db_paths:
            raise ValueError(f"unknown projection kind: {kind!r}")
        table_name, key_column = _KINDS[normalized_kind]
        now = _now()
        with open_database(self.db_paths[normalized_kind]) as conn:
            with transaction(conn):
                target = conn.execute(
                    f"SELECT generation,{key_column} FROM {table_name} WHERE projection_id=? AND status='ready'",
                    (str(projection_id),),
                ).fetchone()
                if target is None or str(target[1]) != str(key):
                    raise ProjectionError("rollback target is missing")
                target_acl_rows = conn.execute(
                    "SELECT workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class FROM projection_acl WHERE projection_id=? ORDER BY workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class",
                    (str(projection_id),),
                ).fetchall()
                if not target_acl_rows:
                    raise ProjectionError("rollback target has no ACL domain")
                target_acl = {tuple(str(value) for value in row) for row in target_acl_rows}
                if scope is not None:
                    if not self._scope_ok(scope) or scope.as_tuple() not in target_acl:
                        raise ProjectionError("rollback scope is outside target ACL domain")
                current = conn.execute(
                    "SELECT current_projection_id,generation FROM projection_heads WHERE projection_kind=? AND projection_key=?",
                    (normalized_kind, str(key)),
                ).fetchone()
                current_id = str(current[0]) if current is not None else ""
                if current_id:
                    pointed = conn.execute(
                        f"SELECT {key_column} FROM {table_name} WHERE projection_id=?",
                        (current_id,),
                    ).fetchone()
                    if pointed is None or str(pointed[0]) != str(key):
                        raise ProjectionError("projection head points outside its key domain")
                    current_acl_rows = conn.execute(
                        "SELECT workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class FROM projection_acl WHERE projection_id=? ORDER BY workspace_id,agent_instance_id,project_ref,provider,share_group_id,sensitivity,policy_class",
                        (current_id,),
                    ).fetchall()
                    current_acl = {tuple(str(value) for value in row) for row in current_acl_rows}
                    if current_acl and current_acl != target_acl:
                        raise ProjectionError("rollback target ACL domain differs from current head")
                head_generation = int(current[1]) if current is not None else None
                generation = self._next_generation(conn, table_name, key_column, str(key), head_generation)
                head_id = stable_projection_id("projection-head", normalized_kind, key)
                conn.execute("INSERT INTO projection_heads(head_id,projection_kind,projection_key,current_projection_id,generation,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(projection_kind,projection_key) DO UPDATE SET current_projection_id=excluded.current_projection_id,generation=excluded.generation,updated_at=excluded.updated_at", (head_id, normalized_kind, str(key), str(projection_id), generation, now))
                event_id = stable_projection_id("projection-event", normalized_kind, key, generation, "rollback", projection_id)
                conn.execute("INSERT INTO projection_head_events(event_id,projection_kind,projection_key,event_type,projection_id,generation,reason,created_at) VALUES(?,?,?,?,?,?,?,?)", (event_id, normalized_kind, str(key), "rollback", str(projection_id), generation, str(reason), now))
                return event_id

    def record_ledger(self, source_ref: str, code: str, detail: str = "") -> str:
        ledger_id = stable_projection_id("projection-ledger", source_ref, code, detail)
        now = _now()
        # Ledger belongs to scenario DB; migration can use it even when no
        # projection rows exist yet.
        with open_database(self.db_paths["scenario"]) as conn:
            with transaction(conn):
                conn.execute("INSERT INTO projection_ledger(ledger_id,source_ref,code,detail,created_at) VALUES(?,?,?,?,?) ON CONFLICT(source_ref,code,detail) DO NOTHING", (ledger_id, str(source_ref), str(code), str(detail), now))
        return ledger_id

    def counts(self, kind: str = "scenario") -> dict[str, int]:
        normalized_kind = str(kind).strip().lower()
        table_name, _ = self._kind(normalized_kind)
        with self.connection(normalized_kind) as conn:
            return {
                "projections": int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]),
                "evidence_links": int(conn.execute("SELECT COUNT(*) FROM projection_evidence_links").fetchone()[0]),
                "items": int(conn.execute("SELECT COUNT(*) FROM projection_items").fetchone()[0]),
                "heads": int(conn.execute("SELECT COUNT(*) FROM projection_heads").fetchone()[0]),
                "tombstones": int(conn.execute("SELECT COUNT(*) FROM projection_tombstones").fetchone()[0]),
                "ledger": int(conn.execute("SELECT COUNT(*) FROM projection_ledger").fetchone()[0]),
            }

    table_counts = counts

    def orphan_count(self, kind: str = "scenario") -> int:
        normalized_kind = str(kind).strip().lower()
        table_name, _ = self._kind(normalized_kind)
        with self.connection(normalized_kind) as conn:
            return int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT i.item_id) FROM projection_items i "
                    f"LEFT JOIN {table_name} p ON p.projection_id=i.projection_id "
                    "LEFT JOIN projection_evidence_links l ON l.projection_id=i.projection_id "
                    "AND l.evidence_id=i.evidence_id AND l.evidence_hash=i.evidence_hash "
                    "WHERE p.projection_id IS NULL OR i.evidence_id='' OR i.evidence_hash='' "
                    "OR i.atom_id='' OR l.link_id IS NULL OR l.evidence_id='' OR l.evidence_hash=''"
                ).fetchone()[0]
            )

    def integrity_check(self, kind: str = "scenario") -> list[str]:
        with self.connection(kind) as conn:
            return [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]

    def foreign_key_check(self, kind: str = "scenario") -> list[tuple[Any, ...]]:
        with self.connection(kind) as conn:
            return [tuple(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]


__all__ = [
    "ProjectionError",
    "ProjectionSchemaError",
    "ProjectionReadScope",
    "ProjectionRecord",
    "ProjectionStore",
    "PROJECTION_SCHEMA_MARKER",
    "PROJECTION_SCHEMA_VERSION",
    "stable_projection_id",
]
