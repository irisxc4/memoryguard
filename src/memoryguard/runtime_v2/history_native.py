"""Native V2 adapter for conversation history in the Content V2 plane.

The adapter is intentionally small at the transport boundary and strict at
the scope boundary.  It does not import the shared-memory store, and it never
opens the legacy history database. Reads use the canonical Content V2
database in read-only mode; deletes create V2 tombstones and durable receipts.

``NativeHistoryService.dispatch`` is the registry-facing API.  The seven
operation methods are also public to keep host integrations easy to inject and
test.  Search/timeline/extract/list return metadata only.  Full turn bodies
are emitted by the explicit read/export operations only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Callable, Mapping
from ..storage.layout import WorkspaceV2Layout
from .history_store import (
    ContentHistoryStore,
    MAX_PAGE,
    MAX_TIMELINE_RADIUS,
    V2HistoryAccessResolver,
    V2HistoryScope as HistoryScope,
    content_history_schema_status,
)

try:  # Native authority is process-local and cannot be forged through JSON.
    from .native_ports import (
        NativeContextError,
        _NATIVE_CONTEXT_CAPABILITY,
        resolve_native_transport_context,
    )
except Exception:  # pragma: no cover - defensive package import fallback
    _NATIVE_CONTEXT_CAPABILITY = object()

    class NativeContextError(RuntimeError):
        pass

    def resolve_native_transport_context(context: Any) -> Any:
        raise NativeContextError("trusted_context_capability_required")


HISTORY_OPERATIONS = (
    "search",
    "timeline",
    "read",
    "extract_preview",
    "list_sessions",
    "export",
    "delete",
)

_ALIASES = {
    **{name: name for name in HISTORY_OPERATIONS},
    **{f"memoryguard_history_{name}": name for name in HISTORY_OPERATIONS},
    "history_search": "search",
    "history_timeline": "timeline",
    "history_read": "read",
    "history_extract_preview": "extract_preview",
    "history_list_sessions": "list_sessions",
    "history_export": "export",
    "history_delete": "delete",
}
_IDENTITY_KEYS = frozenset({
    "workspace_id", "workspace", "agent_instance_id", "agent_id", "agent",
    "trusted_agent_id", "trusted_agent", "share_group_id", "group_id", "group",
    "trusted_group_id", "trusted_group", "project_ref", "project_id", "project",
    "trusted_project_ref", "trusted_project", "provider", "trusted_provider",
    "runtime_role", "runtime", "trusted_runtime_role", "trusted_runtime",
    "scope", "identity", "trusted_identity", "trusted_context",
})
_REDACTED_KEYS = frozenset({"content", "content_preview", "body", "raw_content"})
_NATIVE_ERROR_CODES = frozenset({
    "history_schema_future", "history_schema_invalid", "history_schema_partial",
    "history_schema_unsupported", "history_schema_version_invalid",
    "mutation_idempotency_conflict", "history_delete_failed",
    "history_delete_scope_required", "history_store_failure",
})
_TEST_CAPABILITY_TOKEN = object()
_REPARSE_POINT = 0x0400


class _NativeHistoryTestCapability:
    """Process-local wrapper required for dependency injection in tests."""

    __slots__ = ("_value", "_token")

    def __init__(self, value: Any) -> None:
        self._value = value
        self._token = _TEST_CAPABILITY_TOKEN

    @property
    def value(self) -> Any:
        if self._token is not _TEST_CAPABILITY_TOKEN:
            raise NativeHistoryError("native_test_capability_required")
        return self._value


def _native_history_test_capability(value: Any) -> _NativeHistoryTestCapability:
    """Private test-only DI wrapper; intentionally omitted from ``__all__``."""
    return _NativeHistoryTestCapability(value)


def _unwrap_test_capability(value: Any, *, kind: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, _NativeHistoryTestCapability):
        raise NativeHistoryError("native_test_capability_required")
    raw = value.value
    if isinstance(raw, Mapping):
        raise NativeHistoryError(f"invalid_{kind}_capability")
    if kind == "scope_resolver" and not (
        callable(raw) or callable(getattr(raw, "resolve", None))
    ):
        raise NativeHistoryError("invalid_scope_resolver_capability")
    if kind == "store_factory" and not callable(raw):
        raise NativeHistoryError("invalid_store_factory_capability")
    if kind == "history_store" and raw is not None and isinstance(raw, (str, bytes, Path)):
        raise NativeHistoryError("invalid_history_store_capability")
    return raw


def _lexical_absolute(value: str | Path) -> Path:
    """Absolute lexical path without resolving symlink/reparse components."""
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _has_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _assert_safe_lexical_path(value: str | Path, *, allow_missing: bool = True) -> Path:
    """Reject symlink/reparse/dangling components before any ``resolve`` call."""
    path = _lexical_absolute(value)
    cursor = path
    missing_seen = False
    while True:
        try:
            cursor.lstat()
        except FileNotFoundError:
            missing_seen = True
        except OSError as exc:
            raise NativeHistoryError("history_path_unavailable") from exc
        else:
            if _has_reparse_or_symlink(cursor):
                raise NativeHistoryError("history_path_reparse_or_symlink")
            if missing_seen and cursor.exists():
                # A missing child below an existing parent is ordinary; a
                # dangling link would have been caught by lstat above.
                pass
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if not allow_missing and not path.exists():
        raise NativeHistoryError("history_path_missing")
    return path


def _assert_history_artifacts_safe(db_path: Path) -> None:
    """Preflight the DB and every SQLite sidecar before opening read-only."""
    _assert_safe_lexical_path(db_path)
    for suffix in ("-wal", "-shm", "-journal"):
        _assert_safe_lexical_path(Path(str(db_path) + suffix))


def _stable_store_code(exc: BaseException, *, fallback: str = "history_store_failure") -> str:
    """Map dependency/store failures to a bounded, non-leaking code."""
    if isinstance(exc, sqlite3.DatabaseError):
        return "history_store_database_error"
    if isinstance(exc, TypeError):
        return "history_store_type_error"
    if isinstance(exc, ValueError):
        return "history_store_value_error"
    if isinstance(exc, OSError):
        return "history_store_os_error"
    return fallback


def _call_with_signature(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call an injected function once, selecting the compatible signature.

    TypeError raised *inside* the injected function must never trigger a
    second call with dropped arguments.  ``inspect.signature`` is therefore
    used as the sole dispatch decision; when a signature is unavailable we
    make one full call and let the caller handle its error.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(*args, **kwargs)
    try:
        signature.bind(*args, **kwargs)
    except TypeError:
        # Resolver callables from older hosts may accept only the trusted
        # principal. Select that arity from the signature; never use a
        # caught TypeError from the function body as a reason to retry.
        fallback_args = args[:-1] if len(args) > 1 else args
        try:
            signature.bind(*fallback_args)
        except TypeError:
            if kwargs:
                try:
                    signature.bind(*args)
                except TypeError:
                    raise
                return func(*args)
            raise
        return func(*fallback_args)
    return func(*args, **kwargs)


class NativeHistoryError(RuntimeError):
    """Stable non-leaking error raised at the native history boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "native_history_error")
        super().__init__(self.code)


class _ReadOnlyHistoryStore(ContentHistoryStore):
    """Compatibility name for the V2-native Content history store."""


@dataclass(frozen=True)
class _TrustedScope:
    scope: HistoryScope | None
    capability: bool
    facts: Mapping[str, Any]


class NativeHistoryService:
    """Dependency-injected native adapter for seven history operations.

    ``scope_resolver`` may be a ``V2HistoryAccessResolver`` or a callable with
    ``(trusted_agent_id, requested_scope)``.  ``history_store`` is optional;
    when omitted the adapter opens only the existing history database, and
    never through the constructor that initializes/repairs it.
    """

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        source_workspace: str | Path | None = None,
        history_store: Any = None,
        scope_resolver: Any = None,
        resolver: Any = None,
        store_factory: Callable[..., Any] | None = None,
    ) -> None:
        if isinstance(workspace, WorkspaceV2Layout):
            if source_workspace is None:
                raise NativeHistoryError("source_workspace_required")
            self.source_workspace = _assert_safe_lexical_path(source_workspace)
            layout_workspace = _lexical_absolute(workspace.workspace)
            if self.source_workspace != layout_workspace:
                raise NativeHistoryError("source_workspace_mismatch")
            self.layout = workspace
        else:
            if source_workspace is not None:
                raise NativeHistoryError("source_workspace_requires_layout")
            self.source_workspace = _assert_safe_lexical_path(workspace)
            self.layout = None
        self.workspace = self.source_workspace
        self.db_path = WorkspaceV2Layout(self.workspace).content_db
        _assert_history_artifacts_safe(self.db_path)
        self._history_store = _unwrap_test_capability(history_store, kind="history_store")
        injected_resolver = scope_resolver if scope_resolver is not None else resolver
        self._scope_resolver = _unwrap_test_capability(injected_resolver, kind="scope_resolver")
        self._scope_resolver = self._scope_resolver or V2HistoryAccessResolver(self.workspace)
        self._store_factory = _unwrap_test_capability(store_factory, kind="store_factory")

    # ---- context/scope -------------------------------------------------
    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(k): v for k, v in value.items()}
        values: dict[str, Any] = {}
        for key in (
            "workspace_id", "workspace", "trusted_agent_id", "agent_instance_id",
            "share_group_id", "group_id", "project_ref", "project_id", "provider",
            "runtime_role", "session_id", "session_source", "session_trusted",
            "is_admin", "admin",
        ):
            if hasattr(value, key):
                values[key] = getattr(value, key)
        if hasattr(value, "trusted_agent_id") and "agent_instance_id" not in values:
            values["agent_instance_id"] = getattr(value, "trusted_agent_id")
        return values

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None else str(value).strip()

    @classmethod
    def _first(cls, source: Mapping[str, Any], *keys: str) -> str:
        for key in keys:
            value = cls._text(source.get(key))
            if value:
                return value
        return ""

    def _trusted_scope(self, context: Any) -> _TrustedScope | None:
        if context is None:
            raise NativeHistoryError("trusted_context_required")

        # Resolve the immutable process-issued authority before consulting any
        # scope resolver.  Public mapping aliases (including agent/group/
        # workspace claims) are compatibility projections only and must never
        # influence authorization or deletion.
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError:
            # Preserve neutral reads for callers without a native envelope;
            # destructive operations receive an explicit capability error in
            # ``_execute`` below.  Crucially, no resolver is called with an
            # attacker-controlled agent id in this branch.
            return _TrustedScope(None, False, {})

        source = authority.to_dict()
        agent = authority.agent_instance_id
        if not agent:
            raise NativeHistoryError("trusted_context_required")
        workspace_id = authority.workspace_id or str(self.source_workspace)
        try:
            context_workspace = _assert_safe_lexical_path(workspace_id)
        except NativeHistoryError:
            raise NativeHistoryError("context_workspace_mismatch")
        if context_workspace != self.source_workspace:
            raise NativeHistoryError("context_workspace_mismatch")

        # The active binding, not a request body or caller-claimed group, is
        # authoritative.  A missing/inactive/ambiguous binding is neutralized
        # by returning None, avoiding an existence oracle.
        requested = {"mode": "agent", "agent_instance_id": agent}
        try:
            resolver = self._scope_resolver
            if hasattr(resolver, "resolve"):
                resolved = _call_with_signature(resolver.resolve, agent, requested)
            else:
                resolved = _call_with_signature(resolver, agent, requested)
        except (PermissionError, LookupError, ValueError):
            return None
        except Exception:
            return None
        scope = self._scope_from_resolution(resolved, source, agent)
        return _TrustedScope(scope, True, source) if scope is not None else None

    def _scope_from_resolution(
        self, resolved: Any, source: Mapping[str, Any], agent: str,
    ) -> HistoryScope | None:
        if all(hasattr(resolved, name) for name in ("agent_instance_id", "project_ref", "provider", "share_group_id")):
            base = resolved
            active_group = str(getattr(base, "share_group_id", "") or "")
            authorized = tuple(getattr(base, "authorized_agent_ids", ()) or ())
        else:
            ok = getattr(resolved, "ok", None)
            if ok is False:
                return None
            mapping = resolved if isinstance(resolved, Mapping) else {}
            runtime_scope = getattr(resolved, "scope", None)
            runtime_scope = runtime_scope or mapping.get("scope")
            if runtime_scope is None:
                return None
            if isinstance(runtime_scope, Mapping):
                mode = self._text(runtime_scope.get("mode"))
                active_group = self._text(runtime_scope.get("share_group_id"))
            else:
                mode = self._text(getattr(runtime_scope, "mode", ""))
                active_group = self._text(getattr(runtime_scope, "share_group_id", ""))
            authorized = tuple(
                str(item) for item in (
                    getattr(resolved, "authorized_agent_ids", None)
                    or mapping.get("authorized_agent_ids", ())
                ) if str(item).strip()
            )
            if mode not in {"agent", "share_group"}:
                return None
            base = None

        claimed_group = self._first(source, "share_group_id", "group_id", "group")
        if claimed_group and active_group and claimed_group != active_group:
            return None
        if claimed_group and not active_group:
            # Personal bindings are deliberately not made shareable by a
            # context mapping that claims an unrelated group.
            return None
        project = self._first(source, "project_ref", "project_id", "project")
        provider = self._first(source, "provider")
        if base is not None:
            return HistoryScope(
                # Resolver output supplies active-group membership only; the
                # caller identity remains the canonical native authority.
                agent_instance_id=agent,
                project_ref=project or str(getattr(base, "project_ref", "") or ""),
                provider=provider or str(getattr(base, "provider", "") or ""),
                share_group_id=active_group,
                authorized_agent_ids=authorized or tuple(getattr(base, "authorized_agent_ids", ()) or ()),
                shared_read=bool(getattr(base, "shared_read", False)),
            )
        return HistoryScope(
            agent_instance_id=agent,
            project_ref=project,
            provider=provider,
            share_group_id=active_group,
            authorized_agent_ids=authorized or (agent,),
            shared_read=bool(active_group),
        )

    # ---- store lifecycle ------------------------------------------------
    def _schema_state(self) -> str:
        try:
            _assert_history_artifacts_safe(self.db_path)
        except NativeHistoryError:
            return "invalid"
        return content_history_schema_status(self.db_path)

    def _schema_exists(self) -> bool:
        return self._schema_state() == "valid"

    @staticmethod
    def _readonly_protocol(store: Any) -> bool:
        # A dependency-injected store must opt in explicitly.  Merely having
        # read-shaped methods is insufficient because a writable store can
        # mutate schema or content behind this boundary.
        return getattr(store, "readonly", None) is True

    def _store_path_schema_matches(self, store: Any) -> bool:
        """Require an injected store to point at this exact safe history DB."""
        try:
            store_workspace = _assert_safe_lexical_path(getattr(store, "workspace"))
            store_db = _assert_safe_lexical_path(getattr(store, "db_path"))
            _assert_history_artifacts_safe(store_db)
        except (AttributeError, TypeError, NativeHistoryError):
            return False
        if store_workspace != self.source_workspace or store_db != self.db_path:
            return False
        return self._schema_state() == "valid"

    def _validate_injected_store(self, store: Any, *, readonly: bool) -> Any:
        if readonly and not self._readonly_protocol(store):
            raise NativeHistoryError("readonly_history_store_required")
        if not self._store_path_schema_matches(store):
            raise NativeHistoryError("history_store_path_or_schema_mismatch")
        if not readonly and not self._durable_mutation_protocol(store):
            raise NativeHistoryError("durable_idempotency_unavailable")
        return store

    @staticmethod
    def _durable_mutation_protocol(store: Any) -> bool:
        return getattr(store, "supports_durable_idempotency", None) is True

    def _store(self, *, readonly: bool) -> Any | None:
        if self._history_store is not None:
            return self._validate_injected_store(self._history_store, readonly=readonly)
        schema_state = self._schema_state()
        if schema_state == "future":
            # Future databases are a hard blocker, not an existence-neutral
            # miss.  No writable/open-repair path is attempted.
            raise NativeHistoryError("history_schema_future")
        if schema_state == "invalid" or schema_state == "unsupported":
            raise NativeHistoryError("history_schema_invalid")
        if schema_state != "valid":
            return None
        if self._store_factory is not None:
            try:
                store = _call_with_signature(self._store_factory, self.workspace, readonly=readonly)
            except Exception as exc:
                raise NativeHistoryError("history_store_unavailable") from exc
            return self._validate_injected_store(store, readonly=readonly)
        return _ReadOnlyHistoryStore(self.workspace, readonly=readonly)

    # ---- result shaping -------------------------------------------------
    @staticmethod
    def _payload(payload: Any, values: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if payload is None:
            result: dict[str, Any] = {}
        elif isinstance(payload, Mapping):
            result = {str(k): v for k, v in payload.items()}
        else:
            try:
                result = {str(k): v for k, v in vars(payload).items()}
            except TypeError as exc:
                raise NativeHistoryError("invalid_history_arguments") from exc
        if values:
            result.update({str(k): v for k, v in values.items()})
        return result

    @staticmethod
    def _neutral(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if operation in {"search"}:
            return {"query": str(payload.get("query") or ""), "results": [], "limit": 0, "offset": 0, "neutral": True}
        if operation == "timeline":
            return {"session_id": "", "anchor_turn_id": "", "radius": 0, "turns": [], "neutral": True}
        if operation == "read":
            return {"turn": None, "session": None, "turns": [], "neutral": True}
        if operation == "extract_preview":
            return {"session_id": "", "candidates": [], "written_to_long_term_memory": False, "neutral": True}
        if operation == "list_sessions":
            return {"sessions": [], "project_groups": [], "total": 0, "limit": 0, "offset": 0, "neutral": True}
        if operation == "export":
            return {"format": "memoryguard-history-v1", "sessions": [], "neutral": True}
        return {"deleted_sessions": 0, "invalidated_evidence_links": 0, "long_term_memories_deleted": 0, "neutral": True}

    @classmethod
    def _strip_raw(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(k): cls._strip_raw(v) for k, v in value.items() if str(k) not in _REDACTED_KEYS}
        if isinstance(value, list):
            return [cls._strip_raw(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._strip_raw(item) for item in value)
        return value

    @classmethod
    def _required_text(cls, payload: Mapping[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise NativeHistoryError(f"invalid_{field}")
        return value.strip()

    @classmethod
    def _bounded_int(
        cls, payload: Mapping[str, Any], field: str, *, default: int,
        minimum: int = 0, maximum: int = MAX_PAGE,
    ) -> int:
        value = default if field not in payload else payload.get(field)
        if type(value) is not int or value < minimum or value > maximum:
            raise NativeHistoryError(f"invalid_{field}")
        return value

    @classmethod
    def _id_list(cls, payload: Mapping[str, Any], field: str, *, required: bool = False) -> list[str]:
        value = payload.get(field)
        if value is None:
            if required:
                raise NativeHistoryError(f"invalid_{field}")
            return []
        if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
            raise NativeHistoryError(f"invalid_{field}")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise NativeHistoryError(f"invalid_{field}")
            text = item.strip()
            if text not in result:
                result.append(text)
        if required and not result:
            raise NativeHistoryError(f"invalid_{field}")
        if len(result) > MAX_PAGE:
            raise NativeHistoryError(f"invalid_{field}")
        return result

    @classmethod
    def _normalize_payload(cls, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        if operation == "search":
            normalized["query"] = cls._required_text(payload, "query")
            normalized["limit"] = cls._bounded_int(payload, "limit", default=20, minimum=1)
            normalized["offset"] = cls._bounded_int(payload, "offset", default=0)
        elif operation == "timeline":
            normalized["session_id"] = cls._required_text(payload, "session_id")
            normalized["anchor_turn_id"] = cls._required_text(payload, "anchor_turn_id")
            normalized["radius"] = cls._bounded_int(
                payload, "radius", default=4, maximum=MAX_TIMELINE_RADIUS,
            )
        elif operation == "read":
            # MCP/GUI compatibility envelopes may include the inactive
            # selector as an empty placeholder. Treat it as omitted before
            # enforcing the one-target read contract.
            session_id = payload.get("session_id")
            turn_id = payload.get("turn_id")
            if session_id is not None and not cls._text(session_id):
                session_id = None
            if turn_id is not None and not cls._text(turn_id):
                turn_id = None
            if (session_id is None) == (turn_id is None):
                raise NativeHistoryError("exactly_one_of_session_id_or_turn_id_required")
            if session_id is not None:
                normalized["session_id"] = cls._required_text(payload, "session_id")
            if turn_id is not None:
                normalized["turn_id"] = cls._required_text(payload, "turn_id")
            normalized["limit"] = cls._bounded_int(payload, "limit", default=100, minimum=1, maximum=250)
            normalized["offset"] = cls._bounded_int(payload, "offset", default=0)
        elif operation == "extract_preview":
            normalized["session_id"] = cls._required_text(payload, "session_id")
            if "turn_ids" in payload:
                normalized["turn_ids"] = cls._id_list(payload, "turn_ids", required=True)
            normalized["limit"] = cls._bounded_int(payload, "limit", default=20, minimum=1)
        elif operation == "list_sessions":
            normalized["limit"] = cls._bounded_int(payload, "limit", default=50, minimum=1)
            normalized["offset"] = cls._bounded_int(payload, "offset", default=0)
            if "extracted" in payload and payload.get("extracted") is not None and type(payload.get("extracted")) is not bool:
                raise NativeHistoryError("invalid_extracted")
            for field in ("date_from", "date_to"):
                if field in payload and payload.get(field) is not None and not isinstance(payload.get(field), str):
                    raise NativeHistoryError(f"invalid_{field}")
                normalized[field] = str(payload.get(field) or "").strip()
        elif operation == "export":
            normalized["session_ids"] = cls._id_list(payload, "session_ids", required=True)
        elif operation == "delete":
            normalized["session_ids"] = cls._id_list(payload, "session_ids", required=True)
            for field, default in (("confirmed", True), ("invalidate_evidence", True)):
                value = payload.get(field, default)
                if type(value) is not bool:
                    raise NativeHistoryError(f"invalid_{field}")
                normalized[field] = value
        return normalized

    def _execute(self, operation: str, payload: Mapping[str, Any], *, context: Any,
                 include_raw: bool | None = None, generation: Any = None,
                 state: Any = None, mutation_receipt: Any = None,
                 idempotency_key: str = "") -> dict[str, Any]:
        trusted = self._trusted_scope(context)
        if trusted is None or trusted.scope is None:
            if operation == "delete" and trusted is not None and not trusted.capability:
                raise NativeHistoryError("native_trusted_capability_required")
            return self._neutral(operation, payload)
        payload = self._normalize_payload(operation, payload)
        scope = trusted.scope
        if operation == "delete":
            return self._delete(
                payload, scope, trusted.capability, generation=generation,
                state=state, mutation_receipt=mutation_receipt,
                idempotency_key=idempotency_key,
                context_facts=trusted.facts,
            )
        store = self._store(readonly=True)
        if store is None:
            return self._neutral(operation, payload)
        try:
            if operation == "search":
                result = store.search(scope, self._text(payload.get("query")), limit=payload.get("limit", 20), offset=payload.get("offset", 0))
            elif operation == "timeline":
                result = store.timeline(scope, self._text(payload.get("session_id")), self._text(payload.get("anchor_turn_id")), radius=payload.get("radius", 4))
            elif operation == "read":
                result = store.read(scope, session_id=self._text(payload.get("session_id")), turn_id=self._text(payload.get("turn_id")), limit=payload.get("limit", 100), offset=payload.get("offset", 0))
            elif operation == "extract_preview":
                result = store.extract_preview(scope, self._text(payload.get("session_id")), turn_ids=payload.get("turn_ids"), limit=payload.get("limit", 20))
            elif operation == "list_sessions":
                result = store.list_sessions(scope, limit=payload.get("limit", 50), offset=payload.get("offset", 0), extracted=payload.get("extracted"), date_from=self._text(payload.get("date_from")), date_to=self._text(payload.get("date_to")))
            elif operation == "export":
                result = store.export(scope, session_ids=[str(item) for item in (payload.get("session_ids") or [])])
            else:  # pragma: no cover - dispatch validates the operation
                raise NativeHistoryError("unknown_history_operation")
        except (sqlite3.DatabaseError, OSError, TypeError, ValueError) as exc:
            # Dependency/store failures are never existence-neutral and never
            # echo exception text.  Validation errors raised above this call
            # remain their explicit ``invalid_*`` enum codes.
            raise NativeHistoryError(_stable_store_code(exc)) from exc
        except (LookupError, PermissionError):
            return self._neutral(operation, payload)
        # Timeline and extraction are preview/metadata operations.  They do
        # not qualify as the explicit raw read/export permission path.
        if operation == "search" and isinstance(result, Mapping):
            # ``ConversationHistoryStore.search`` exposes a bounded FTS
            # snippet under ``matched_summary``.  That snippet is still raw
            # turn text, so the native boundary drops it; callers can use the
            # stable title/summary/anchor identifiers to request an explicit
            # read or export next.
            result = dict(result)
            result["results"] = [
                {key: value for key, value in item.items() if key != "matched_summary"}
                for item in result.get("results", [])
                if isinstance(item, Mapping)
            ]
        elif operation in {"timeline", "extract_preview"}:
            result = self._strip_raw(result)
        elif operation == "read" and include_raw is False:
            result = self._strip_raw(result)
        elif operation == "export" and include_raw is False:
            result = self._strip_raw(result)
        return result

    # ---- public operation methods --------------------------------------
    def search(self, payload: Any = None, *, context: Any = None, **values: Any) -> dict[str, Any]:
        return self._execute("search", self._payload(payload, values), context=context)

    def timeline(self, payload: Any = None, *, context: Any = None, **values: Any) -> dict[str, Any]:
        return self._execute("timeline", self._payload(payload, values), context=context)

    def read(self, payload: Any = None, *, context: Any = None, include_raw: bool = True, **values: Any) -> dict[str, Any]:
        return self._execute("read", self._payload(payload, values), context=context, include_raw=include_raw)

    def extract_preview(self, payload: Any = None, *, context: Any = None, **values: Any) -> dict[str, Any]:
        return self._execute("extract_preview", self._payload(payload, values), context=context)

    def list_sessions(self, payload: Any = None, *, context: Any = None, **values: Any) -> dict[str, Any]:
        return self._execute("list_sessions", self._payload(payload, values), context=context)

    def export(self, payload: Any = None, *, context: Any = None, include_raw: bool = True, **values: Any) -> dict[str, Any]:
        return self._execute("export", self._payload(payload, values), context=context, include_raw=include_raw)

    def _delete(self, payload: Mapping[str, Any], scope: HistoryScope, capability: bool,
                *, generation: Any, state: Any, mutation_receipt: Any,
                idempotency_key: str, context_facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not capability:
            raise NativeHistoryError("native_trusted_capability_required")
        state_text = self._text(getattr(state, "value", state)).upper()
        if state_text != "V2_ACTIVE":
            raise NativeHistoryError("v2_not_active")
        if type(generation) is not int or generation < 0:
            raise NativeHistoryError("invalid_manifest_generation")
        confirmed = bool(payload.get("confirmed", True))
        if not confirmed:
            raise NativeHistoryError("history_delete_confirmation_required")
        invalidate_evidence = bool(payload.get("invalidate_evidence", True))
        key = self._text(idempotency_key or payload.get("idempotency_key"))
        if not key:
            raise NativeHistoryError("mutation_idempotency_required")
        receipt = mutation_receipt or payload.get("mutation_receipt") or payload.get("receipt")
        if receipt is None:
            raise NativeHistoryError("mutation_receipt_required")
        receipt_map = self._mapping(receipt)
        receipt_id = self._first(receipt_map, "receipt_id", "id", "token")
        if not receipt_id:
            raise NativeHistoryError("mutation_receipt_required")
        session_ids = self._id_list(payload, "session_ids", required=True)
        context = dict(context_facts or {})
        runtime = self._first(context, "runtime_role", "runtime", "trusted_runtime_role", "trusted_runtime")
        # Include every fact that can alter the authorization or mutation
        # outcome.  The receipt is hashed, never echoed, so changing even an
        # otherwise opaque receipt field cannot replay under the same key.
        digest_facts = {
            "operation": "delete",
            "receipt_id": receipt_id,
            "receipt": receipt_map,
            "workspace": str(self.workspace),
            "share_group_id": scope.share_group_id,
            "agent_instance_id": scope.agent_instance_id,
            "project_ref": scope.project_ref,
            "provider": scope.provider,
            "runtime": runtime,
            "confirmed": confirmed,
            "invalidate_evidence": invalidate_evidence,
            "session_ids": session_ids,
            "generation": generation,
            "state": state_text,
            "idempotency_key": key,
            "context_session_id": self._first(context, "session_id"),
            "context_session_source": self._first(context, "session_source"),
            "context_session_trusted": bool(context.get("session_trusted", False)),
        }
        try:
            encoded_facts = json.dumps(
                digest_facts, sort_keys=True, separators=(",", ":"), default=str,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise NativeHistoryError("invalid_mutation_receipt") from exc
        digest = hashlib.sha256(encoded_facts).hexdigest()
        store = self._store(readonly=False)
        if store is None:
            # A fully authorized delete against a workspace with no active
            # History V2 store is an existence-neutral no-op.  All capability,
            # CAS, confirmation, receipt and idempotency gates above still run;
            # no database is created merely to record a mutation that changed
            # nothing.
            return self._neutral("delete", payload)
        delete = getattr(store, "delete", None)
        if delete is None or not self._durable_mutation_protocol(store):
            raise NativeHistoryError("durable_idempotency_unavailable")
        try:
            # Verify the extended mutation protocol before invoking it.  This
            # avoids a TypeError retry that could silently drop the replay
            # fence arguments on an injected store.
            sig = inspect.signature(delete)
            sig.bind(
                scope,
                session_ids=session_ids,
                invalidate_evidence=invalidate_evidence,
                idempotency_key=key,
                operation_digest=digest,
            )
        except (TypeError, ValueError):
            raise NativeHistoryError("durable_idempotency_unavailable")
        try:
            result = delete(
                scope,
                session_ids=session_ids,
                invalidate_evidence=invalidate_evidence,
                idempotency_key=key,
                operation_digest=digest,
            )
        except ValueError as exc:
            code = _stable_store_code(exc)
            if code == "history_store_value_error" and str(exc) in {"mutation_idempotency_conflict", "history_delete_scope_required"}:
                code = str(exc)
            raise NativeHistoryError(code) from exc
        except TypeError as exc:
            raise NativeHistoryError("history_store_type_error") from exc
        except (LookupError, PermissionError):
            raise NativeHistoryError("history_delete_failed")
        except (sqlite3.DatabaseError, OSError) as exc:
            raise NativeHistoryError("history_delete_failed") from exc
        if not isinstance(result, Mapping):
            raise NativeHistoryError("history_delete_failed")
        return {
            "deleted_sessions": int(result.get("deleted_sessions", 0)),
            "invalidated_evidence_links": int(result.get("invalidated_evidence_links", 0)),
            "long_term_memories_deleted": 0,
            "idempotent_replay": bool(result.get("idempotent_replay", False)),
        }

    def delete(self, payload: Any = None, *, context: Any = None,
               generation: Any = None, state: Any = None,
               mutation_receipt: Any = None, idempotency_key: str = "",
               **values: Any) -> dict[str, Any]:
        return self._execute(
            "delete", self._payload(payload, values), context=context,
            generation=generation, state=state,
            mutation_receipt=mutation_receipt, idempotency_key=idempotency_key,
        )

    # ---- registry-facing dispatch --------------------------------------
    def dispatch(self, operation: str, payload: Any = None, *, context: Any = None,
                 trusted_context: Any = None, generation: Any = None,
                 state: Any = None, mutation_receipt: Any = None,
                 idempotency_key: str = "", raw_permission: bool | None = None,
                 mutation: bool = False) -> dict[str, Any]:
        del mutation  # operation classification is authoritative here
        name = _ALIASES.get(str(operation or "").strip().casefold())
        if name is None:
            return {"ok": False, "status": "error", "operation": str(operation or ""), "code": "unknown_history_operation"}
        effective_context = context if context is not None else trusted_context
        data = self._payload(payload)
        try:
            # Identity fields in ``data`` are intentionally never forwarded to
            # scope resolution.  raw_permission is a host-level kwarg, not a
            # request-body switch that an RPC caller can forge.
            include_raw = raw_permission
            if include_raw is None and name in {"read", "export"}:
                include_raw = True
            result = self._execute(
                name, data, context=effective_context, include_raw=include_raw,
                generation=generation, state=state,
                mutation_receipt=mutation_receipt,
                idempotency_key=idempotency_key,
            )
            status = "neutral" if bool(result.get("neutral")) else "ok"
            return {"ok": True, "status": status, "operation": name, "data": result}
        except NativeHistoryError as exc:
            return {"ok": False, "status": "error", "operation": name, "code": exc.code, "error": exc.code}

    call = dispatch


__all__ = ["HISTORY_OPERATIONS", "NativeHistoryError", "NativeHistoryService"]
