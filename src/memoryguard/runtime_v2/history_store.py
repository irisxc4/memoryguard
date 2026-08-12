"""V2-native conversation history over the canonical Content database."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from ..storage.layout import WorkspaceV2Layout
from ..storage.transaction import transaction
from .group_native import GroupControlError, GroupControlService, personal_group_id


MAX_PAGE = 100
MAX_TIMELINE_RADIUS = 25
CONTENT_HISTORY_SCHEMA_VERSION = 3

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "content_schema_meta": frozenset({"key", "value"}),
    "conversation_sessions": frozenset({
        "session_id", "source_object_id", "external_id", "title", "provider",
        "workspace_id", "agent_instance_id", "project_ref", "share_group_id",
        "policy_class", "created_at", "imported_at", "active",
    }),
    "conversation_turns": frozenset({
        "turn_id", "occurrence_id", "session_ref", "role", "ordinal",
        "metadata_json", "created_at", "session_id", "event_key",
        "content_type", "source_revision",
    }),
    "conversation_summaries": frozenset({
        "summary_id", "session_id", "occurrence_id", "summary_kind",
        "summary_hash", "updated_at",
    }),
    "content_occurrences": frozenset({
        "occurrence_id", "source_object_id", "blob_id", "active",
    }),
    "content_blobs": frozenset({"blob_id", "text"}),
    "source_objects": frozenset({"source_object_id", "active"}),
    "content_evidence_links": frozenset({
        "link_id", "session_id", "status", "invalidated_at",
    }),
    "content_tombstones": frozenset({
        "tombstone_id", "source_object_id", "occurrence_id", "blob_id",
        "reason", "scan_id", "metadata_json", "created_at", "restored_at", "active",
    }),
    "history_mutation_receipts": frozenset({
        "idempotency_key", "operation", "payload_digest", "result_json", "created_at",
    }),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _normalize_project_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return os.path.normcase(str(Path(text).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(text)


def _project_metadata(value: Any) -> dict[str, str]:
    ref = str(value or "")
    key = hashlib.sha256(_normalize_project_ref(ref).encode("utf-8")).hexdigest()[:16]
    path = Path(ref) if ref else None
    label = path.name if path is not None and path.name else ref
    parent = str(path.parent) if path is not None and ref else ""
    status = "available" if path is not None and path.exists() else ("removed" if ref else "unknown")
    return {
        "project_key": key,
        "project_ref": ref,
        "project_label": label,
        "project_status": status,
        "project_parent": parent,
    }


def _safe_session_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["owner_agent_instance_id"] = str(item.get("agent_instance_id") or "")
    item.update(_project_metadata(item.get("project_ref")))
    return item


@dataclass(frozen=True)
class V2HistoryScope:
    agent_instance_id: str
    project_ref: str = ""
    provider: str = ""
    share_group_id: str = ""
    authorized_agent_ids: tuple[str, ...] = ()
    shared_read: bool = False

    def __post_init__(self) -> None:
        agent = str(self.agent_instance_id or "").strip()
        if not agent:
            raise ValueError("agent_instance_id_required")
        object.__setattr__(self, "agent_instance_id", agent[:256])
        object.__setattr__(self, "project_ref", _normalize_project_ref(self.project_ref))
        object.__setattr__(self, "provider", str(self.provider or "").strip().lower()[:64])
        object.__setattr__(self, "share_group_id", str(self.share_group_id or "").strip()[:256])
        members = tuple(sorted({str(item).strip()[:256] for item in self.authorized_agent_ids if str(item).strip()}))
        object.__setattr__(self, "authorized_agent_ids", members)


class V2HistoryAccessResolver:
    """Resolve history visibility exclusively from V2 group-control state."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    def resolve(self, trusted_agent_id: str, requested: Mapping[str, Any] | None = None) -> V2HistoryScope:
        agent = str(trusted_agent_id or "").strip()
        if not agent:
            raise PermissionError("trusted_agent_scope_required")
        request = dict(requested or {})
        try:
            service = GroupControlService(self.workspace, write=False)
            binding = service.active_binding_for_agent(agent)
        except GroupControlError as exc:
            raise PermissionError(exc.code) from exc
        if binding is None:
            raise PermissionError("history_active_binding_required")
        group = str(binding.get("share_group_id") or "")
        shared = bool(group and group != personal_group_id(agent))
        members = (agent,)
        if shared:
            members = tuple(
                sorted({
                    str(item.get("agent_instance_id") or "")
                    for item in service.list_bindings(include_inactive=False).get("bindings", [])
                    if str(item.get("share_group_id") or "") == group
                    and str(item.get("agent_instance_id") or "")
                })
            ) or (agent,)
        return V2HistoryScope(
            agent_instance_id=agent,
            project_ref=str(request.get("project_ref") or ""),
            provider=str(request.get("provider") or ""),
            share_group_id=group if shared else "",
            authorized_agent_ids=members,
            shared_read=shared,
        )


def content_history_schema_status(path: Path) -> str:
    if not path.is_file():
        return "missing"
    try:
        uri = "file:" + quote(str(path), safe="/:\\") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            row = conn.execute(
                "SELECT value FROM content_schema_meta WHERE key='version'"
            ).fetchone()
            if row is None:
                return "invalid"
            marker = str(row[0])
            if marker.isdigit() and int(marker) > CONTENT_HISTORY_SCHEMA_VERSION:
                return "future"
            if marker != str(CONTENT_HISTORY_SCHEMA_VERSION):
                return "unsupported"
            tables = {
                str(item[0])
                for item in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not set(_REQUIRED_COLUMNS) <= tables:
                return "invalid"
            for table, required in _REQUIRED_COLUMNS.items():
                columns = {
                    str(item[1])
                    for item in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                }
                if not required <= columns:
                    return "invalid"
            if str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower() != "ok":
                return "invalid"
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return "invalid"
    return "valid"


class ContentHistoryStore:
    """Bounded history reads and tombstone deletes against Content V2."""

    supports_durable_idempotency = True

    def __init__(self, workspace: str | Path, *, readonly: bool) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.db_path = WorkspaceV2Layout(self.workspace).content_db
        self.readonly = bool(readonly)

    @contextmanager
    def _connect(self):
        if content_history_schema_status(self.db_path) != "valid":
            raise sqlite3.DatabaseError("history_schema_invalid")
        if self.readonly:
            uri = "file:" + quote(str(self.db_path), safe="/:\\") + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        else:
            conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _scope_where(scope: V2HistoryScope, *, owner: bool = False) -> tuple[str, list[Any]]:
        members = (scope.agent_instance_id,) if owner else (scope.authorized_agent_ids or (scope.agent_instance_id,))
        sql = "s.active=1 AND s.agent_instance_id IN (" + ",".join("?" for _ in members) + ")"
        args: list[Any] = list(members)
        if scope.project_ref:
            sql += " AND s.project_ref=?"
            args.append(scope.project_ref)
        if scope.provider:
            sql += " AND s.provider=?"
            args.append(scope.provider)
        return sql, args

    @staticmethod
    def _summary_join() -> str:
        return (
            "LEFT JOIN conversation_summaries cs ON cs.session_id=s.session_id "
            "LEFT JOIN content_occurrences so ON so.occurrence_id=cs.occurrence_id AND so.active=1 "
            "LEFT JOIN content_blobs sb ON sb.blob_id=so.blob_id"
        )

    def list_sessions(self, scope: V2HistoryScope, *, limit: int = 50, offset: int = 0,
                      extracted: bool | None = None, date_from: str = "", date_to: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE)); offset = max(0, int(offset))
        where, args = self._scope_where(scope)
        if date_from:
            where += " AND COALESCE(s.created_at,s.imported_at)>=?"; args.append(date_from)
        if date_to:
            where += " AND COALESCE(s.created_at,s.imported_at)<=?"; args.append(date_to)
        evidence = ""
        if extracted is True:
            evidence = " AND EXISTS (SELECT 1 FROM content_evidence_links ce WHERE ce.session_id=s.session_id AND ce.status='valid')"
        elif extracted is False:
            evidence = " AND NOT EXISTS (SELECT 1 FROM content_evidence_links ce WHERE ce.session_id=s.session_id AND ce.status='valid')"
        sql = (
            "SELECT s.session_id,s.external_id,s.title,s.provider,s.agent_instance_id,s.project_ref,s.share_group_id,"
            "s.created_at,s.imported_at,COALESCE(MAX(sb.text),'') summary,"
            "COUNT(DISTINCT CASE WHEN o.active=1 THEN t.turn_id END) turn_count,"
            "COUNT(DISTINCT CASE WHEN e.status='valid' THEN e.link_id END) evidence_count "
            "FROM conversation_sessions s " + self._summary_join() + " "
            "LEFT JOIN conversation_turns t ON t.session_id=s.session_id "
            "LEFT JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id "
            "LEFT JOIN content_evidence_links e ON e.session_id=s.session_id "
            f"WHERE {where}{evidence} GROUP BY s.session_id "
            "ORDER BY COALESCE(s.created_at,s.imported_at) DESC,s.session_id DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, [*args, limit, offset]).fetchall()
            total = int(conn.execute(f"SELECT COUNT(*) FROM conversation_sessions s WHERE {where}{evidence}", args).fetchone()[0])
        sessions = [_safe_session_projection(dict(row)) for row in rows]
        groups: dict[str, dict[str, Any]] = {}
        for item in sessions:
            key = str(item["project_key"])
            group = groups.setdefault(key, {**_project_metadata(item.get("project_ref")), "session_count": 0, "agents": [], "latest_at": ""})
            group["session_count"] += 1
            agent = str(item.get("agent_instance_id") or "")
            if agent and agent not in group["agents"]:
                group["agents"].append(agent)
            group["latest_at"] = max(str(group["latest_at"]), str(item.get("created_at") or item.get("imported_at") or ""))
        return {"sessions": sessions, "project_groups": list(groups.values()), "total": total, "limit": limit, "offset": offset}

    def search(self, scope: V2HistoryScope, query: str, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        tokens = re.findall(r"[\w\u4e00-\u9fff]{2,}", str(query or ""))[:12]
        if not tokens:
            raise ValueError("history_query_required")
        limit = max(1, min(int(limit), MAX_PAGE)); offset = max(0, int(offset))
        where, args = self._scope_where(scope)
        token_sql = " OR ".join("LOWER(b.text) LIKE ? OR LOWER(s.title) LIKE ? OR LOWER(COALESCE(sb.text,'')) LIKE ?" for _ in tokens)
        token_args: list[str] = []
        for token in tokens:
            value = "%" + token.casefold() + "%"
            token_args.extend((value, value, value))
        sql = (
            "SELECT DISTINCT s.session_id,s.external_id,t.turn_id,'turn' result_type,s.title,s.provider,"
            "s.agent_instance_id,s.project_ref,s.created_at,COALESCE(sb.text,'') summary,t.role,"
            "t.created_at turn_created_at,t.content_type "
            "FROM conversation_sessions s " + self._summary_join() + " "
            "JOIN conversation_turns t ON t.session_id=s.session_id "
            "JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id AND o.active=1 "
            "JOIN content_blobs b ON b.blob_id=o.blob_id "
            f"WHERE {where} AND ({token_sql}) ORDER BY t.created_at DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, [*args, *token_args, limit, offset]).fetchall()
        results = []
        for row in rows:
            item = _safe_session_projection(dict(row))
            item.update({"anchor_turn_id": str(item.get("turn_id") or ""), "can_timeline": True, "read_target": "turn"})
            results.append(item)
        return {"query": query, "results": results, "limit": limit, "offset": offset}

    def timeline(self, scope: V2HistoryScope, session_id: str, anchor_turn_id: str, *, radius: int = 4) -> dict[str, Any]:
        radius = max(0, min(int(radius), MAX_TIMELINE_RADIUS))
        where, args = self._scope_where(scope)
        with self._connect() as conn:
            anchor = conn.execute(
                f"SELECT t.ordinal FROM conversation_turns t JOIN conversation_sessions s ON s.session_id=t.session_id WHERE t.turn_id=? AND t.session_id=? AND {where}",
                [anchor_turn_id, session_id, *args],
            ).fetchone()
            if anchor is None:
                raise LookupError("history_anchor_not_found")
            rows = conn.execute(
                "SELECT t.turn_id,t.ordinal,t.role,t.created_at,t.content_type,b.text content "
                "FROM conversation_turns t JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id AND o.active=1 "
                "JOIN content_blobs b ON b.blob_id=o.blob_id WHERE t.session_id=? AND t.ordinal BETWEEN ? AND ? ORDER BY t.ordinal",
                (session_id, int(anchor[0]) - radius, int(anchor[0]) + radius),
            ).fetchall()
        turns = []
        for row in rows:
            item = {key: value for key, value in dict(row).items() if key != "content"}
            item["content_preview"] = _short(row["content"])
            turns.append(item)
        return {"session_id": session_id, "anchor_turn_id": anchor_turn_id, "radius": radius, "turns": turns}

    def read(self, scope: V2HistoryScope, *, session_id: str = "", turn_id: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if bool(session_id) == bool(turn_id):
            raise ValueError("exactly_one_of_session_id_or_turn_id_required")
        where, args = self._scope_where(scope)
        limit = max(1, min(int(limit), 250)); offset = max(0, int(offset))
        turn_select = (
            "SELECT t.turn_id,t.session_id,t.ordinal,t.role,b.text content,t.created_at,t.content_type "
            "FROM conversation_turns t JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id AND o.active=1 "
            "JOIN content_blobs b ON b.blob_id=o.blob_id"
        )
        with self._connect() as conn:
            if turn_id:
                row = conn.execute(turn_select + f" JOIN conversation_sessions s ON s.session_id=t.session_id WHERE t.turn_id=? AND {where}", [turn_id, *args]).fetchone()
                if row is None:
                    raise LookupError("history_turn_not_found")
                return {"turn": dict(row)}
            session = conn.execute(
                "SELECT s.session_id,s.title,s.provider,s.project_ref,s.created_at,COALESCE(sb.text,'') summary FROM conversation_sessions s "
                + self._summary_join() + f" WHERE s.session_id=? AND {where} GROUP BY s.session_id",
                [session_id, *args],
            ).fetchone()
            if session is None:
                raise LookupError("history_session_not_found")
            rows = conn.execute(turn_select + " WHERE t.session_id=? ORDER BY t.ordinal LIMIT ? OFFSET ?", (session_id, limit, offset)).fetchall()
        return {"session": dict(session), "turns": [dict(row) for row in rows], "limit": limit, "offset": offset}

    def extract_preview(self, scope: V2HistoryScope, session_id: str, *, turn_ids: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        where, args = self._scope_where(scope)
        limit = max(1, min(int(limit), MAX_PAGE))
        with self._connect() as conn:
            session = conn.execute(f"SELECT s.title,s.provider FROM conversation_sessions s WHERE s.session_id=? AND {where}", [session_id, *args]).fetchone()
            if session is None:
                raise LookupError("history_session_not_found")
            params: list[Any] = [session_id]
            extra = ""
            if turn_ids:
                cleaned = [str(item) for item in turn_ids[:limit] if str(item)]
                if not cleaned:
                    raise ValueError("turn_ids_required")
                extra = " AND t.turn_id IN (" + ",".join("?" for _ in cleaned) + ")"
                params.extend(cleaned)
            rows = conn.execute(
                "SELECT t.turn_id,t.role,b.text content,t.created_at FROM conversation_turns t "
                "JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id AND o.active=1 "
                "JOIN content_blobs b ON b.blob_id=o.blob_id WHERE t.session_id=?" + extra + " ORDER BY t.ordinal LIMIT ?",
                [*params, limit],
            ).fetchall()
        candidates = []
        for row in rows:
            body = _short(row["content"], 1200)
            if body:
                candidates.append({
                    "title": _short(session["title"], 80), "body": body,
                    "kind_hint": "episode", "confidence": 0.3,
                    "evidence": {"session_id": session_id, "turn_id": row["turn_id"], "provider": session["provider"], "created_at": row["created_at"]},
                    "provenance": [{"source_object_id": session_id, "locator": f"history:turn:{row['turn_id']}", "excerpt_hash": hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()}],
                })
        return {"session_id": session_id, "candidates": candidates, "written_to_long_term_memory": False}

    def export(self, scope: V2HistoryScope, *, session_ids: Iterable[str]) -> dict[str, Any]:
        ids = [str(item) for item in session_ids if str(item)][:MAX_PAGE]
        if not ids:
            raise ValueError("session_ids_required")
        return {"format": "memoryguard-history-v2", "exported_at": _now(), "sessions": [self.read(scope, session_id=item) for item in ids]}

    def delete(self, scope: V2HistoryScope, *, session_ids: list[str], invalidate_evidence: bool = False,
               idempotency_key: str = "", operation_digest: str = "") -> dict[str, int | bool]:
        unique = list(dict.fromkeys(str(item) for item in session_ids if str(item)))[:MAX_PAGE]
        if not unique:
            raise ValueError("history_delete_scope_required")
        if not idempotency_key or not operation_digest:
            raise ValueError("history_delete_idempotency_digest_required")
        where, args = self._scope_where(scope, owner=True)
        with self._connect() as conn:
            with transaction(conn):
                prior = conn.execute(
                    "SELECT operation,payload_digest,result_json FROM history_mutation_receipts WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if prior is not None:
                    if str(prior[0]) != "delete" or str(prior[1]) != operation_digest:
                        raise ValueError("mutation_idempotency_conflict")
                    result = json.loads(str(prior[2]))
                    result["idempotent_replay"] = True
                    return result
                deleted = invalidated = 0
                now = _now()
                for session_id in unique:
                    session = conn.execute(
                        f"SELECT s.source_object_id FROM conversation_sessions s WHERE s.session_id=? AND {where}",
                        [session_id, *args],
                    ).fetchone()
                    if session is None:
                        continue
                    source_object_id = str(session[0])
                    occurrences = conn.execute(
                        "SELECT o.occurrence_id,o.blob_id FROM conversation_turns t JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id WHERE t.session_id=? AND o.active=1",
                        (session_id,),
                    ).fetchall()
                    cur = conn.execute("UPDATE content_evidence_links SET status='invalid',invalidated_at=? WHERE session_id=? AND status='valid'", (now, session_id))
                    invalidated += int(cur.rowcount)
                    for occurrence in occurrences:
                        occurrence_id, blob_id = str(occurrence[0]), str(occurrence[1] or "")
                        tombstone_id = "tomb-" + hashlib.sha256(f"history-delete\x1f{source_object_id}\x1f{occurrence_id}".encode()).hexdigest()[:24]
                        conn.execute(
                            "INSERT INTO content_tombstones(tombstone_id,source_object_id,occurrence_id,blob_id,reason,scan_id,metadata_json,created_at,restored_at,active) VALUES(?,?,?,?,?,'','{}',?,'',1) "
                            "ON CONFLICT(source_object_id,occurrence_id,reason) DO UPDATE SET active=1,restored_at=''",
                            (tombstone_id, source_object_id, occurrence_id, blob_id, "history_delete", now),
                        )
                    conn.execute("UPDATE content_occurrences SET active=0 WHERE occurrence_id IN (SELECT occurrence_id FROM conversation_turns WHERE session_id=?)", (session_id,))
                    conn.execute("UPDATE source_objects SET active=0 WHERE source_object_id=?", (source_object_id,))
                    deleted += int(conn.execute("UPDATE conversation_sessions SET active=0 WHERE session_id=? AND active=1", (session_id,)).rowcount)
                result: dict[str, int | bool] = {
                    "deleted_sessions": deleted,
                    "invalidated_evidence_links": invalidated,
                    "long_term_memories_deleted": 0,
                    "idempotent_replay": False,
                }
                conn.execute(
                    "INSERT INTO history_mutation_receipts(idempotency_key,operation,payload_digest,result_json,created_at) VALUES(?,?,?,?,?)",
                    (idempotency_key, "delete", operation_digest, json.dumps(result, sort_keys=True, separators=(",", ":")), now),
                )
                return result


__all__ = [
    "CONTENT_HISTORY_SCHEMA_VERSION", "ContentHistoryStore", "MAX_PAGE",
    "MAX_TIMELINE_RADIUS", "V2HistoryAccessResolver", "V2HistoryScope",
    "content_history_schema_status", "_normalize_project_ref",
]
