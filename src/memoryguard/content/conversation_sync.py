"""Phase 3 conversation synchronization on the V2 Content Plane.

This module is deliberately a *shadow* synchronizer.  It accepts normalized
conversation events from a provider adapter, but does not activate or replace
any of the legacy history importers.  The important invariant is that a run's
reservation, batch application, manifest staging and CAS state update happen
on the same ``content.db`` transaction.

There is no lifetime turn limit.  Providers choose a batch budget and resume
with the opaque cursor returned by :meth:`ConversationSync.stage_batch`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..storage.database import open_database
from ..storage.transaction import transaction
from .store import ContentError, ContentReadScope, ContentStore, canonicalize_text, stable_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ConversationSyncError(ContentError):
    """Base class for synchronization failures."""


class SyncConflictError(ConversationSyncError):
    """The source revision or active run owner is stale."""


class SyncBusyError(SyncConflictError):
    """A different run currently owns the source CAS reservation."""


class SyncCursorError(SyncConflictError):
    """A continuation token is missing, forged, stale, or cross-run."""


@dataclass(frozen=True)
class ConversationEvent:
    """Provider-neutral conversation event.

    ``event_id`` is the provider's stable event ID.  When absent,
    ``capture_id`` is generated for this occurrence only; it is intentionally
    not derived from the text, so equal text from two events never collapses
    their occurrence identities.
    """

    external_object_key: str
    content: str | bytes
    role: str = "user"
    ordinal: int = 0
    event_id: str = ""
    capture_id: str = ""
    source_revision: str = ""
    title: str = ""
    provider: str = ""
    workspace_id: str = ""
    agent_instance_id: str = ""
    project_ref: str = ""
    share_group_id: str = ""
    sensitivity: str = "normal"
    policy_class: str = "private"
    content_type: str = "text"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    locator: Mapping[str, Any] = field(default_factory=dict)

    @property
    def event_key(self) -> str:
        return self.event_id or self.capture_id


@dataclass(frozen=True)
class SyncRun:
    source_id: str
    run_id: str
    revision: int
    owner_id: str
    state: str = "scanning"
    cursor: str = ""
    expected_revision: int = 0

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "run_id": self.run_id,
            "revision": self.revision,
            "owner_id": self.owner_id,
            "state": self.state,
            "cursor": self.cursor,
            "expected_revision": self.expected_revision,
        }


@dataclass(frozen=True)
class SyncBatchResult:
    run_id: str
    source_id: str
    applied: int
    changed: int
    blob_ids: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    continuation_cursor: str
    chars: int
    coverage_digest: str

    @property
    def next_cursor(self) -> str:
        return self.continuation_cursor

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "applied": self.applied,
            "changed": self.changed,
            "blob_ids": list(self.blob_ids),
            "occurrence_ids": list(self.occurrence_ids),
            "continuation_cursor": self.continuation_cursor,
            "next_cursor": self.continuation_cursor,
            "chars": self.chars,
            "coverage_digest": self.coverage_digest,
        }


@dataclass(frozen=True)
class SyncResult:
    run_id: str
    source_id: str
    state: str
    revision: int
    tombstoned: int = 0
    restored: int = 0
    applied: int = 0
    changed: int = 0
    continuation_cursor: str = ""
    manifest_digest: str = ""
    coverage_digest: str = ""

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_id": self.source_id,
            "state": self.state,
            "revision": self.revision,
            "tombstoned": self.tombstoned,
            "restored": self.restored,
            "applied": self.applied,
            "changed": self.changed,
            "continuation_cursor": self.continuation_cursor,
            "manifest_digest": self.manifest_digest,
            "coverage_digest": self.coverage_digest,
        }


def _cursor_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _new_cursor() -> str:
    # The raw token is returned to the provider exactly once.  Only its
    # digest is persisted; no replayable secret or content is stored in the
    # database.
    return "c1." + secrets.token_urlsafe(32)


def _hash_text(text: str | bytes) -> tuple[str, str]:
    canonical = canonicalize_text(text)
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event(value: ConversationEvent | Mapping[str, Any], ordinal: int) -> ConversationEvent:
    if isinstance(value, ConversationEvent):
        if value.event_key:
            return value
        return ConversationEvent(**{**value.__dict__, "capture_id": uuid.uuid4().hex})
    data = dict(value)
    content = data.get("content", data.get("text", data.get("body", "")))
    external = (
        data.get("external_object_key")
        or data.get("session_external_id")
        or data.get("conversation_id")
        or data.get("external_id")
        or data.get("session_id")
    )
    if not external:
        raise ValueError("conversation event requires external_object_key/session_id")
    event_id = str(data.get("event_id", data.get("event_key", data.get("id", ""))) or "")
    capture_id = str(data.get("capture_id", "") or "") or ("capture-" + uuid.uuid4().hex)
    allowed = {
        "external_object_key", "content", "role", "ordinal", "event_id", "capture_id",
        "source_revision", "title", "provider", "workspace_id", "agent_instance_id",
        "project_ref", "share_group_id", "sensitivity", "policy_class", "content_type",
        "metadata", "locator",
    }
    kwargs = {key: data[key] for key in allowed if key in data}
    kwargs.update(external_object_key=str(external), content=content, ordinal=int(data.get("ordinal", ordinal) or ordinal))
    kwargs["event_id"] = event_id
    kwargs["capture_id"] = capture_id
    if "metadata" not in kwargs:
        kwargs["metadata"] = data.get("metadata_json", {}) or {}
    return ConversationEvent(**kwargs)


class ConversationSync:
    """Transactional shadow synchronizer backed by one ``ContentStore``."""

    def __init__(self, store: ContentStore):
        if not isinstance(store, ContentStore):
            raise TypeError("ConversationSync requires ContentStore")
        self.store = store

    def _ensure_evidence_columns(self, conn) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(content_evidence_links)")}
        if "blob_id" not in columns:
            conn.execute("ALTER TABLE content_evidence_links ADD COLUMN blob_id TEXT NOT NULL DEFAULT ''")
        if "source_revision" not in columns:
            conn.execute("ALTER TABLE content_evidence_links ADD COLUMN source_revision TEXT NOT NULL DEFAULT ''")

    def begin_sync(
        self,
        source_id: str,
        expected_revision: int | None = None,
        *,
        owner_id: str = "",
        owner: str | None = None,
        run_id: str | None = None,
        provider: str = "conversation",
        source_type: str = "conversation",
        external_root_key: str = "",
        workspace_id: str | None = None,
    ) -> SyncRun:
        """Reserve a source with an SQL compare-and-swap.

        ``active_run_id`` is the owner token.  Every subsequent write checks
        it in the same transaction, so a stale run cannot apply a batch or
        tombstone objects after a newer run has won the reservation.
        """

        source_id = str(source_id)
        owner_id = str(owner if owner is not None else owner_id or "owner")
        run_id = str(run_id or stable_id("sync", source_id, owner_id, uuid.uuid4().hex))
        with open_database(self.store.db_path) as conn:
            with transaction(conn):
                self.store.upsert_source_connector(
                    source_id=source_id,
                    provider=provider,
                    source_type=source_type,
                    external_root_key=external_root_key or source_id,
                    workspace_id=workspace_id or self.store.workspace_id,
                    conn=conn,
                )
                conn.execute(
                    "INSERT INTO source_sync_state(source_id) VALUES(?) ON CONFLICT(source_id) DO NOTHING",
                    (source_id,),
                )
                conflicting_run = conn.execute(
                    "SELECT source_id FROM source_sync_state WHERE active_run_id=? AND source_id<>? LIMIT 1",
                    (run_id, source_id),
                ).fetchone()
                if conflicting_run is not None:
                    raise SyncConflictError("run_id is already bound to another source")
                row = conn.execute(
                    "SELECT active_run_id,owner_id,state,revision FROM source_sync_state WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                current_revision = int(row[3])
                if expected_revision is None:
                    expected_revision = current_revision
                if current_revision != int(expected_revision):
                    raise SyncConflictError(
                        f"stale source revision for {source_id}: expected {expected_revision}, current {current_revision}"
                    )
                active_run = str(row[0] or "")
                active_owner = str(row[1] or "")
                state = str(row[2])
                if active_run == run_id and active_owner and active_owner != owner_id:
                    raise SyncConflictError("synchronization run owner mismatch")
                if active_run and active_run != run_id and state in {"scanning", "applying"}:
                    raise SyncBusyError(f"source {source_id} is owned by another run")
                expected_manifest_digest = self._manifest_digest(conn, source_id)
                updated = conn.execute(
                    "UPDATE source_sync_state SET active_run_id=?,owner_id=?,state='scanning',cursor='',cursor_digest='',cursor_source_id='',cursor_run_id='',cursor_owner_id='',cursor_revision=0,cursor_position=0,cursor_batch_digest='',expected_revision=?,expected_manifest_digest=?,last_started_at=?,last_error_code='',revision=revision+1 WHERE source_id=? AND revision=? AND (active_run_id='' OR active_run_id=? OR state IN ('idle','partial','failed','complete'))",
                    (run_id, owner_id, int(expected_revision), expected_manifest_digest, _now(), source_id, int(expected_revision), run_id),
                ).rowcount
                if updated != 1:
                    raise SyncConflictError("source reservation CAS failed")
                revision = int(conn.execute("SELECT revision FROM source_sync_state WHERE source_id=?", (source_id,)).fetchone()[0])
                # A new owner gets a clean staging area.  A caller resuming its
                # own partial run retains the prior batches.
                if active_run != run_id:
                    conn.execute("DELETE FROM source_manifest_staging WHERE source_id=? AND run_id<>?", (source_id, run_id))
                return SyncRun(source_id, run_id, revision, owner_id, "scanning", "", int(expected_revision))

    def _check_owner(
        self,
        conn,
        source_id: str,
        run_id: str,
        owner_id: str,
        *,
        revision: int | None = None,
        expected_revision: int | None = None,
    ) -> tuple[str, int, str]:
        row = conn.execute(
            "SELECT active_run_id,owner_id,state,revision,expected_revision FROM source_sync_state WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if row is None or str(row[0]) != run_id or str(row[1]) != owner_id:
            raise SyncConflictError("stale or unknown synchronization run owner")
        if revision is not None and int(row[3]) != int(revision):
            raise SyncConflictError("synchronization revision changed")
        if expected_revision is not None and int(row[4]) != int(expected_revision):
            raise SyncConflictError("synchronization expected revision changed")
        state = str(row[2])
        if state not in {"scanning", "applying", "partial"}:
            raise SyncConflictError(f"run is not writable in state {state!r}")
        return str(row[0]), int(row[3]), state

    def _resolve_run(
        self,
        conn,
        run: SyncRun | str,
        *,
        owner_id: str = "",
        expected_revision: int | None = None,
    ) -> tuple[str, str, int, int, dict[str, Any]]:
        """Resolve a caller run claim against the durable owner/CAS row."""

        if isinstance(run, SyncRun):
            run_id = str(run.run_id)
            claimed_source = str(run.source_id)
            claimed_owner = str(run.owner_id)
            claimed_revision = int(run.revision)
            claimed_expected = int(run.expected_revision)
            if owner_id and str(owner_id) != claimed_owner:
                raise SyncConflictError("synchronization run owner mismatch")
            if expected_revision is not None and int(expected_revision) != claimed_expected:
                raise SyncConflictError("synchronization expected revision mismatch")
        else:
            run_id = str(run)
            claimed_source = ""
            claimed_owner = str(owner_id or "")
            claimed_revision = None
            claimed_expected = expected_revision
            if not claimed_owner:
                raise SyncConflictError("owner_id is required when run is not a SyncRun")
        row = conn.execute(
            "SELECT source_id,active_run_id,owner_id,state,revision,expected_revision,expected_manifest_digest,coverage_digest,cursor_digest,cursor_source_id,cursor_run_id,cursor_owner_id,cursor_revision,cursor_position,cursor_batch_digest FROM source_sync_state WHERE active_run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise SyncConflictError("unknown synchronization run")
        source_id = str(row[0])
        if claimed_source and claimed_source != source_id:
            raise SyncConflictError("synchronization source mismatch")
        if str(row[1]) != run_id or str(row[2]) != claimed_owner:
            raise SyncConflictError("stale or unknown synchronization run owner")
        if claimed_revision is not None and int(row[4]) != claimed_revision:
            raise SyncConflictError("synchronization revision changed")
        if claimed_expected is not None and int(row[5]) != claimed_expected:
            raise SyncConflictError("synchronization expected revision changed")
        state = str(row[3])
        if state not in {"scanning", "applying", "partial"}:
            raise SyncConflictError(f"run is not writable in state {state!r}")
        context = {
            "source_id": source_id,
            "run_id": run_id,
            "owner_id": claimed_owner,
            "state": state,
            "revision": int(row[4]),
            "expected_revision": int(row[5]),
            "expected_manifest_digest": str(row[6] or ""),
            "coverage_digest": str(row[7] or ""),
            "cursor_digest": str(row[8] or ""),
            "cursor_source_id": str(row[9] or ""),
            "cursor_run_id": str(row[10] or ""),
            "cursor_owner_id": str(row[11] or ""),
            "cursor_revision": int(row[12] or 0),
            "cursor_position": int(row[13] or 0),
            "cursor_batch_digest": str(row[14] or ""),
        }
        return source_id, run_id, int(row[4]), int(row[5]), context

    def _consume_cursor(self, conn, context: Mapping[str, Any], token: str) -> int:
        """Consume the expected server token with a compare-and-swap."""

        expected = str(context.get("cursor_digest") or "")
        supplied = str(token or "")
        if expected:
            if supplied and _cursor_digest(supplied) != expected:
                raise SyncCursorError("invalid or stale continuation cursor")
            if (
                str(context.get("cursor_source_id")) != str(context.get("source_id"))
                or str(context.get("cursor_run_id")) != str(context.get("run_id"))
                or str(context.get("cursor_owner_id")) != str(context.get("owner_id"))
                or int(context.get("cursor_revision", -1)) != int(context.get("revision", -2))
                or not str(context.get("cursor_batch_digest") or "")
            ):
                raise SyncCursorError("continuation cursor binding mismatch")
            consumed = conn.execute(
                "UPDATE source_sync_state SET cursor_digest='',cursor_source_id='',cursor_run_id='',cursor_owner_id='',cursor_revision=0,cursor_position=cursor_position,cursor_batch_digest='' WHERE source_id=? AND active_run_id=? AND owner_id=? AND revision=? AND cursor_digest=?",
                (context["source_id"], context["run_id"], context["owner_id"], context["revision"], expected),
            ).rowcount
            if consumed != 1:
                raise SyncCursorError("continuation cursor was already consumed")
            return int(context.get("cursor_position", 0))
        if supplied:
            raise SyncCursorError("unexpected continuation cursor")
        return int(context.get("cursor_position", 0))

    def _batch_digest(self, canonical_values: Sequence[tuple[ConversationEvent, str, str]], coverage_status: str) -> str:
        payload = [
            (item.external_object_key, item.event_key, int(item.ordinal), item.source_revision, digest, coverage_status)
            for item, _canonical, digest in canonical_values
        ]
        return hashlib.sha256(_json(payload).encode()).hexdigest()

    def stage_batch(
        self,
        run: SyncRun | str,
        events: Iterable[ConversationEvent | Mapping[str, Any]],
        *,
        max_turns: int = 1000,
        max_chars: int = 1_000_000,
        continuation_cursor: str = "",
        coverage_status: str = "covered",
        owner_id: str = "",
        expected_revision: int | None = None,
    ) -> SyncBatchResult:
        """Apply one bounded batch and stage its manifest rows atomically."""

        run_id = run.run_id if isinstance(run, SyncRun) else str(run)
        values = list(events)
        if max_turns <= 0 or max_chars <= 0:
            raise ValueError("max_turns and max_chars must be positive")
        normalized = [_event(value, index) for index, value in enumerate(values)]
        if len(normalized) > max_turns:
            raise ValueError("batch exceeds max_turns; resume with a continuation cursor")
        canonical_values: list[tuple[ConversationEvent, str, str]] = []
        chars = 0
        for item in normalized:
            canonical, digest = _hash_text(item.content)
            chars += len(canonical)
            if chars > max_chars:
                raise ValueError("batch exceeds max_chars; resume with a continuation cursor")
            canonical_values.append((item, canonical, digest))

        with open_database(self.store.db_path) as conn:
            with transaction(conn):
                self._ensure_evidence_columns(conn)
                source_id, run_id, revision, persisted_expected, context = self._resolve_run(
                    conn, run, owner_id=owner_id, expected_revision=expected_revision
                )
                position = self._consume_cursor(conn, context, continuation_cursor)
                conn.execute("UPDATE source_sync_state SET state='applying' WHERE source_id=? AND active_run_id=?", (source_id, run_id))
                blobs: list[str] = []
                occurrences: list[str] = []
                changed = 0
                for item, canonical, digest in canonical_values:
                    if not item.event_key:
                        raise ValueError("event key generation failed")
                    session_id = stable_id("session", source_id, item.external_object_key)
                    source_object_id = stable_id("source-object", source_id, item.external_object_key)
                    effective_ordinal = int(item.ordinal)
                    # Providers are allowed to omit ordinals when paging.  Do
                    # not let the Phase-1 (session_ref, ordinal) compatibility
                    # key turn a second batch into an integrity error.
                    occupied = conn.execute(
                        "SELECT turn_id FROM conversation_turns WHERE session_ref=? AND ordinal=?",
                        (session_id, effective_ordinal),
                    ).fetchone()
                    candidate_turn_id = stable_id("turn", session_id, item.event_key)
                    if occupied is not None and str(occupied[0]) != candidate_turn_id:
                        max_row = conn.execute("SELECT COALESCE(MAX(ordinal),-1) FROM conversation_turns WHERE session_ref=?", (session_id,)).fetchone()
                        effective_ordinal = int(max_row[0]) + 1
                    namespace = self.store.ensure_namespace(
                        workspace_id=item.workspace_id or self.store.workspace_id,
                        trust_domain="conversation",
                        sensitivity=item.sensitivity or "normal",
                        conn=conn,
                    )
                    blob_id = self.store.put_blob(canonical, namespace_id=namespace.namespace_id, conn=conn)
                    if blob_id is None:
                        # Empty turns have no canonical blob by Content Plane
                        # contract and cannot produce a valid FK occurrence.
                        raise ConversationSyncError("empty conversation event is unreadable")
                    old = conn.execute(
                        "SELECT o.blob_id,o.source_revision FROM content_occurrences o WHERE o.source_object_id=? AND o.occurrence_key=?",
                        (source_object_id, item.event_key),
                    ).fetchone()
                    occurrence_id = self.store.upsert_occurrence(
                        source_object_id=source_object_id,
                        occurrence_key=item.event_key,
                        blob_id=blob_id,
                        namespace_id=namespace.namespace_id,
                        source_id=source_id,
                        source_kind="conversation",
                        external_object_key=item.external_object_key,
                        object_type="conversation",
                        source_revision=item.source_revision,
                        ordinal=effective_ordinal,
                        locator=item.locator,
                        content_role="conversation",
                        sensitivity=item.sensitivity,
                        workspace_id=item.workspace_id or self.store.workspace_id,
                        agent_instance_id=item.agent_instance_id,
                        project_ref=item.project_ref,
                        share_group_id=item.share_group_id,
                        policy_class=item.policy_class,
                        provider=item.provider or "conversation",
                        access_scope=item.metadata,
                        conn=conn,
                    )
                    conn.execute(
                        "INSERT INTO conversation_sessions(session_id,source_object_id,external_id,title,provider,workspace_id,agent_instance_id,project_ref,share_group_id,policy_class,created_at,imported_at,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(source_object_id) DO UPDATE SET external_id=excluded.external_id,title=excluded.title,provider=excluded.provider,workspace_id=excluded.workspace_id,agent_instance_id=excluded.agent_instance_id,project_ref=excluded.project_ref,share_group_id=excluded.share_group_id,policy_class=excluded.policy_class,imported_at=excluded.imported_at,active=1",
                        (session_id, source_object_id, item.external_object_key, item.title, item.provider or "conversation", item.workspace_id or self.store.workspace_id, item.agent_instance_id, item.project_ref, item.share_group_id, item.policy_class, _now(), _now()),
                    )
                    turn_id = stable_id("turn", session_id, item.event_key)
                    conn.execute(
                        "INSERT INTO conversation_turns(turn_id,occurrence_id,session_ref,role,ordinal,metadata_json,created_at,session_id,event_key,content_type,source_revision) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET occurrence_id=excluded.occurrence_id,role=excluded.role,ordinal=excluded.ordinal,metadata_json=excluded.metadata_json,session_id=excluded.session_id,event_key=excluded.event_key,content_type=excluded.content_type,source_revision=excluded.source_revision",
                        (turn_id, occurrence_id, session_id, item.role or "user", effective_ordinal, _json(item.metadata), _now(), session_id, item.event_key, item.content_type or "text", item.source_revision),
                    )
                    conn.execute(
                        "INSERT INTO source_manifest_staging(run_id,source_id,external_object_key,occurrence_key,source_revision,content_hash,coverage_status,reason) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,external_object_key,occurrence_key) DO UPDATE SET source_revision=excluded.source_revision,content_hash=excluded.content_hash,coverage_status=excluded.coverage_status,reason=excluded.reason",
                        (run_id, source_id, item.external_object_key, item.event_key, item.source_revision, digest, coverage_status, ""),
                    )
                    blobs.append(str(blob_id)); occurrences.append(str(occurrence_id))
                    if old is None or str(old[0]) != str(blob_id) or str(old[1]) != item.source_revision:
                        changed += 1
                batch_digest = self._batch_digest(canonical_values, coverage_status)
                next_cursor = ""
                next_position = position
                if normalized:
                    next_position = position + len(normalized)
                    next_cursor = _new_cursor()
                    conn.execute(
                        "UPDATE source_sync_state SET cursor='',cursor_digest=?,cursor_source_id=?,cursor_run_id=?,cursor_owner_id=?,cursor_revision=?,cursor_position=?,cursor_batch_digest=? WHERE source_id=? AND active_run_id=? AND owner_id=? AND revision=? AND expected_revision=?",
                        (_cursor_digest(next_cursor), source_id, run_id, context["owner_id"], revision, next_position, batch_digest, source_id, run_id, context["owner_id"], revision, persisted_expected),
                    )
                coverage_digest = self._staging_digest(conn, run_id, source_id)
                conn.execute("UPDATE source_sync_state SET state='scanning',cursor='',coverage_digest=? WHERE source_id=? AND active_run_id=? AND owner_id=? AND revision=? AND expected_revision=?", (coverage_digest, source_id, run_id, context["owner_id"], revision, persisted_expected))
                return SyncBatchResult(run_id, source_id, len(normalized), changed, tuple(blobs), tuple(occurrences), next_cursor, chars, coverage_digest)

    def _staging_digest(self, conn, run_id: str, source_id: str | None = None) -> str:
        if source_id is None:
            rows = conn.execute("SELECT external_object_key,occurrence_key,source_revision,content_hash,coverage_status FROM source_manifest_staging WHERE run_id=? ORDER BY external_object_key,occurrence_key", (run_id,)).fetchall()
        else:
            rows = conn.execute("SELECT source_id,external_object_key,occurrence_key,source_revision,content_hash,coverage_status FROM source_manifest_staging WHERE run_id=? AND source_id=? ORDER BY external_object_key,occurrence_key", (run_id, source_id)).fetchall()
        return hashlib.sha256(_json([tuple(row) for row in rows]).encode()).hexdigest()

    def _manifest_digest(self, conn, source_id: str) -> str:
        rows = conn.execute("SELECT external_object_key,occurrence_key,source_revision,content_hash,active FROM source_manifest_items WHERE source_id=? ORDER BY external_object_key,occurrence_key", (source_id,)).fetchall()
        return hashlib.sha256(_json([tuple(row) for row in rows]).encode()).hexdigest()

    def finish_sync(
        self,
        run: SyncRun | str,
        *,
        status: str = "complete",
        coverage_complete: bool = True,
        continuation_cursor: str = "",
        error_code: str = "",
        owner_id: str = "",
        expected_revision: int | None = None,
    ) -> SyncResult:
        """Finish a run only after proving durable coverage and CAS state.

        ``coverage_complete`` is retained for API compatibility but is never
        used as deletion authority.  The authority is the persisted staging
        ledger plus its digest, the previous manifest digest, and the owner
        and expected-revision CAS row.
        """

        if status not in {"complete", "partial", "failed"}:
            raise ValueError("status must be complete, partial, or failed")
        with open_database(self.store.db_path) as conn:
            with transaction(conn):
                source_id, run_id, revision, persisted_expected, context = self._resolve_run(
                    conn, run, owner_id=owner_id, expected_revision=expected_revision
                )
                if continuation_cursor:
                    # A finish call may present a cursor for an adapter that
                    # has already staged its final page, but it must still be
                    # the current server-bound token.
                    if _cursor_digest(continuation_cursor) != str(context.get("cursor_digest") or ""):
                        raise SyncCursorError("invalid or stale continuation cursor")
                staging = conn.execute(
                    "SELECT source_id,external_object_key,occurrence_key,source_revision,content_hash,coverage_status,reason FROM source_manifest_staging WHERE run_id=? ORDER BY external_object_key,occurrence_key",
                    (run_id,),
                ).fetchall()
                coverage_digest = self._staging_digest(conn, run_id, source_id)
                stored_coverage_digest = str(context.get("coverage_digest") or "")
                manifest_digest_before = self._manifest_digest(conn, source_id)
                expected_manifest_digest = str(context.get("expected_manifest_digest") or "")
                rows_are_owned = all(str(row[0]) == source_id for row in staging)
                complete_statuses = {"complete", "covered"}
                rows_are_complete = bool(staging) and rows_are_owned and all(
                    str(row[5]).lower() in complete_statuses
                    and not str(row[6] or "")
                    and bool(str(row[4] or ""))
                    for row in staging
                )
                staged_identity_count = int(conn.execute(
                    "SELECT COUNT(*) FROM source_manifest_staging s JOIN source_objects so ON so.source_id=s.source_id AND so.external_object_key=s.external_object_key JOIN content_occurrences o ON o.source_object_id=so.source_object_id AND o.occurrence_key=s.occurrence_key JOIN content_blobs b ON b.blob_id=o.blob_id WHERE s.run_id=? AND s.source_id=? AND o.active=1 AND o.source_revision=s.source_revision AND b.canonical_hash=s.content_hash",
                    (run_id, source_id),
                ).fetchone()[0])
                rows_are_complete = rows_are_complete and staged_identity_count == len(staging)
                revision_cas_ok = int(revision) == int(persisted_expected) + 1
                proof_complete = (
                    status == "complete"
                    and rows_are_complete
                    and bool(stored_coverage_digest)
                    and coverage_digest == stored_coverage_digest
                    and bool(expected_manifest_digest)
                    and manifest_digest_before == expected_manifest_digest
                    and revision_cas_ok
                )
                tombstoned = 0
                restored = int(conn.execute("SELECT COUNT(*) FROM content_tombstones t JOIN source_objects so ON so.source_object_id=t.source_object_id JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id WHERE so.source_id=? AND t.active=0 AND t.restored_at<>'' AND EXISTS (SELECT 1 FROM source_manifest_staging s WHERE s.run_id=? AND s.external_object_key=so.external_object_key AND s.occurrence_key=o.occurrence_key)", (source_id, run_id)).fetchone()[0])
                if proof_complete:
                    # A complete run owns deletion authority.  Partial and
                    # failed/unreadable runs never execute this branch.
                    missing = conn.execute(
                        "SELECT m.external_object_key,m.occurrence_key,o.occurrence_id FROM source_manifest_items m JOIN source_objects so ON so.source_id=m.source_id AND so.external_object_key=m.external_object_key JOIN content_occurrences o ON o.source_object_id=so.source_object_id AND o.occurrence_key=m.occurrence_key LEFT JOIN source_manifest_staging s ON s.run_id=? AND s.source_id=m.source_id AND s.external_object_key=m.external_object_key AND s.occurrence_key=m.occurrence_key WHERE m.source_id=? AND m.active=1 AND s.occurrence_key IS NULL",
                        (run_id, source_id),
                    ).fetchall()
                    for external_key, occurrence_key, occurrence_id in missing:
                        self.store.tombstone_occurrence(str(occurrence_id), reason="source_deleted", scan_id=run_id, metadata={"source_id": source_id}, conn=conn)
                        tombstoned += 1
                    scan_id = run_id
                    conn.execute("UPDATE source_manifest_items SET active=0,last_complete_scan_id=? WHERE source_id=?", (scan_id, source_id))
                    for _staging_source, external_key, occurrence_key, source_revision, content_hash, _item_status, _reason in staging:
                        conn.execute(
                            "INSERT INTO source_manifest_items(source_id,external_object_key,occurrence_key,source_revision,content_hash,active,last_complete_scan_id) VALUES(?,?,?,?,?,1,?) ON CONFLICT(source_id,external_object_key,occurrence_key) DO UPDATE SET source_revision=excluded.source_revision,content_hash=excluded.content_hash,active=1,last_complete_scan_id=excluded.last_complete_scan_id",
                        (source_id, external_key, occurrence_key, source_revision, content_hash, scan_id),
                        )
                    conn.execute("DELETE FROM source_manifest_staging WHERE run_id=?", (run_id,))
                    manifest_digest = self._manifest_digest(conn, source_id)
                    updated = conn.execute("UPDATE source_sync_state SET active_run_id='',owner_id='',state='complete',cursor='',cursor_digest='',cursor_source_id='',cursor_run_id='',cursor_owner_id='',cursor_revision=0,cursor_position=0,cursor_batch_digest='',last_complete_scan_id=?,manifest_digest=?,coverage_digest=?,last_finished_at=?,last_error_code='' WHERE source_id=? AND active_run_id=? AND owner_id=? AND revision=? AND expected_revision=?", (scan_id, manifest_digest, coverage_digest, _now(), source_id, run_id, context["owner_id"], revision, persisted_expected)).rowcount
                    if updated != 1:
                        raise SyncConflictError("synchronization finish CAS failed")
                    return SyncResult(run_id, source_id, "complete", revision, tombstoned=tombstoned, restored=restored, continuation_cursor="", manifest_digest=manifest_digest, coverage_digest=coverage_digest)
                # Incomplete proof is fail-closed.  Keep the staging ledger so
                # a provider can diagnose/retry; never write deleted_scan_id.
                result_state = status if status in {"partial", "failed"} else "partial"
                failure_code = error_code or ("coverage_proof_incomplete" if status == "complete" else "")
                updated = conn.execute("UPDATE source_sync_state SET state=?,cursor='',last_finished_at=?,last_error_code=? WHERE source_id=? AND active_run_id=? AND owner_id=? AND revision=? AND expected_revision=?", (result_state, _now(), failure_code, source_id, run_id, context["owner_id"], revision, persisted_expected)).rowcount
                if updated != 1:
                    raise SyncConflictError("synchronization finish CAS failed")
                return SyncResult(run_id, source_id, result_state, revision, restored=restored, continuation_cursor="", coverage_digest=coverage_digest)

    complete_sync = finish_sync

    def sync(
        self,
        source_id: str,
        events: Iterable[ConversationEvent | Mapping[str, Any]],
        *,
        expected_revision: int | None = None,
        owner_id: str = "owner",
        max_turns: int = 1000,
        max_chars: int = 1_000_000,
        coverage_complete: bool = True,
    ) -> SyncResult:
        """Convenience one-batch shadow sync."""

        run = self.begin_sync(source_id, expected_revision, owner_id=owner_id)
        batch = self.stage_batch(run, events, max_turns=max_turns, max_chars=max_chars)
        result = self.finish_sync(run, status="complete", coverage_complete=coverage_complete)
        return SyncResult(**{**result.as_dict(), "applied": batch.applied, "changed": batch.changed})

    def add_evidence_link(
        self,
        memory_id: str,
        *,
        session_id: str = "",
        turn_id: str = "",
        occurrence_id: str = "",
        scope: ContentReadScope | None = None,
    ) -> str:
        """Attach immutable occurrence/blob/source-revision evidence and hold it."""

        if not memory_id:
            raise ValueError("memory_id is required")
        with open_database(self.store.db_path) as conn:
            with transaction(conn):
                self._ensure_evidence_columns(conn)
                if turn_id:
                    row = conn.execute("SELECT t.session_id,t.turn_id,t.occurrence_id,o.blob_id,o.source_revision,o.workspace_id,o.agent_instance_id,o.project_ref,o.provider,o.share_group_id,o.sensitivity,o.policy_class,b.namespace_id FROM conversation_turns t JOIN content_occurrences o ON o.occurrence_id=t.occurrence_id JOIN content_blobs b ON b.blob_id=o.blob_id WHERE t.turn_id=?", (turn_id,)).fetchone()
                elif occurrence_id:
                    row = conn.execute("SELECT '', '',o.occurrence_id,o.blob_id,o.source_revision,o.workspace_id,o.agent_instance_id,o.project_ref,o.provider,o.share_group_id,o.sensitivity,o.policy_class,b.namespace_id FROM content_occurrences o JOIN content_blobs b ON b.blob_id=o.blob_id WHERE o.occurrence_id=?", (occurrence_id,)).fetchone()
                else:
                    raise ValueError("turn_id or occurrence_id is required")
                if row is None:
                    raise ValueError("unknown conversation evidence target")
                if scope is not None:
                    expected = (scope.workspace_id, scope.agent_instance_id, scope.project_ref, scope.provider, scope.share_group_id, scope.sensitivity, scope.policy_class, scope.namespace_id)
                    actual = tuple(str(row[index]) for index in (5, 6, 7, 8, 9, 10, 11, 12))
                    if expected != actual:
                        raise PermissionError("evidence scope denied")
                sid = str(session_id or row[0] or "")
                tid = str(turn_id or row[1] or "")
                oid = str(row[2]); blob_id = str(row[3]); source_revision = str(row[4] or "")
                link_id = stable_id("evidence-link", memory_id, sid, tid, oid, blob_id, source_revision)
                conn.execute("INSERT INTO content_evidence_links(link_id,memory_id,session_id,turn_id,occurrence_id,blob_id,source_revision,status,created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(link_id) DO NOTHING", (link_id, memory_id, sid, tid, oid, blob_id, source_revision, "valid", _now(), ""))
                self.store.hold_blob(blob_id, reason="evidence", source_ref=link_id, conn=conn)
                return link_id

    link_evidence = add_evidence_link


__all__ = [
    "ConversationEvent", "ConversationSync", "ConversationSyncError", "SyncBatchResult",
    "SyncBusyError", "SyncConflictError", "SyncCursorError", "SyncResult", "SyncRun",
]
