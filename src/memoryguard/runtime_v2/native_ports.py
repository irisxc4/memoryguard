"""Native V2 runtime ports and surface registry.

The cutover facade owns the manifest state machine.  This module owns the
production-side dispatch table: it binds the public MCP/GUI/CLI/Hook names to
V2 stores/services, and returns an explicit activation blocker for every name
that has not yet been migrated.  It deliberately does not import a legacy
store, transport server, GUI implementation, or host-hook implementation.

The port is dependency-injected and lazy.  A read-only call never creates a
missing database or repairs a partial schema; a write call only opens a V2
store after a read-only schema preflight has succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
import sqlite3
import weakref
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cutover_v2.surfaces import (
    CLI_COMMAND_NAMES,
    GUI_MUTATION_NAMES,
    GUI_METHOD_NAMES,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
)
from ..storage.layout import WorkspaceV2Layout
from ..storage.database import connect_database, open_database


_HOOK_NAMES = frozenset({"bootstrap_hook"})
_NATIVE_CONTEXT_CAPABILITY = object()
# Native authority is intentionally process-local.  The registry is an
# identity table (rather than an equality check) so shallow/deep copies,
# hand-written lookalikes and deserialised values cannot become capabilities.
# Keep only weak references so request-scoped envelopes do not pin every
# authority issued by a long-running MCP/GUI host forever.  The envelope (or
# another active caller) remains the strong owner for the duration of a call.
_NATIVE_BOUND_CONTEXTS: weakref.WeakValueDictionary[int, "NativeBoundContext"] = weakref.WeakValueDictionary()
# A separate process-local token gates dependency injection.  The transport
# capability above authenticates request identity; this token authenticates
# test/host wiring and must never be accepted from a plain Mapping/JSON value.
_NATIVE_INJECTION_CAPABILITY = object()
_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "workspace_id": ("workspace_id", "workspace"),
    "agent_instance_id": ("agent_instance_id", "agent_id", "agent", "trusted_agent_id", "trusted_agent"),
    "share_group_id": ("share_group_id", "group_id", "group", "trusted_group_id", "trusted_group"),
    "project_ref": ("project_ref", "project_id", "project", "trusted_project_ref", "trusted_project"),
    "provider": ("provider", "trusted_provider"),
    "runtime_role": ("runtime_role", "runtime", "trusted_runtime_role", "trusted_runtime"),
}
_IDENTITY_PAYLOAD_KEYS = frozenset(
    alias for aliases in _IDENTITY_ALIASES.values() for alias in aliases
) | frozenset(
    {
        "runtime_agent_id",
        "parent_agent_id",
        "session_id",
        "session_source",
        "session_trusted",
        "context_hash",
        "trusted_identity",
        "trusted_context",
        "identity",
        "entrypoint",
    }
)

_PHASE9_GUI_HANDLERS = frozenset({
    "history_search", "history_timeline", "history_read",
    "history_extract_preview", "history_list_sessions", "history_export",
    "history_delete", "list_sources", "import_preview", "scan_summary",
    "memory_versions", "memory_supersede_chain",
})
_PHASE9_GUI_READ_HANDLERS = _PHASE9_GUI_HANDLERS - {"history_delete"}


# Native mutation is allowed to open a writable store only after this
# read-only contract has passed.  The phase-2 stores intentionally keep their
# marker/version in a domain-specific metadata table (SQLite user_version is
# still zero in existing workspaces), so preflight checks both metadata rows
# and the concrete tables/columns used by GovernanceV2.
_NATIVE_SCHEMA_SPECS: Mapping[str, Mapping[str, Any]] = {
    "memory": {
        "meta": (
            ("schema_meta", "memory", "memoryguard-v2-phase1"),
            ("memory_schema_meta", "memory", "memoryguard-v2-phase2-memory"),
        ),
        "tables": {
            "atoms": {"atom_id", "memory_id", "body", "kind", "status", "confidence", "locked", "injection_policy", "priority", "canonical_hash", "dedup_domain", "supersedes_json", "provenance_json", "metadata_json", "revision", "visibility", "created_at", "updated_at", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role"},
            "atom_revisions": {"revision_id", "atom_id", "revision", "body", "status", "canonical_hash", "revision_digest", "metadata_json", "created_at"},
            "atom_deltas": {"delta_id", "atom_id", "from_revision", "to_revision", "delta_json", "created_at"},
            "supersession_edges": {"edge_id", "old_atom_id", "new_atom_id", "reason", "source_ref", "created_at"},
            "scope_acl": {"acl_id", "atom_id", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "effect", "metadata_json", "created_at"},
            "source_mappings": {"mapping_id", "atom_id", "source_domain", "source_ref", "source_record_id", "source_revision", "digest", "provenance_json", "created_at"},
            "domain_outbox": {"event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"},
            "outbox_checkpoints": {"domain", "last_sequence", "updated_at"},
            "evidence_projection_receipts": {"event_id", "evidence_id", "projected_at", "error_json"},
            "domain_state": {"domain", "state", "generation", "updated_at", "metadata_json"},
        },
    },
    "evidence": {
        "meta": (
            ("schema_meta", "evidence", "memoryguard-v2-phase1"),
            ("evidence_schema_meta", "evidence", "memoryguard-v2-phase2-evidence"),
        ),
        "tables": {
            "evidence": {"evidence_id", "evidence_type", "source_ref", "revision", "digest", "authority", "status", "metadata_json", "observed_at", "created_at"},
            "evidence_links": {"link_id", "evidence_id", "subject_type", "subject_id", "relation", "metadata_json", "created_at"},
            "migration_map": {"map_id", "source_domain", "source_ref", "source_id", "target_type", "target_id", "metadata_json", "created_at"},
            "domain_outbox": {"event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"},
            "outbox_checkpoints": {"domain", "last_sequence", "updated_at"},
            "audit_refs": {"audit_id", "source_ref", "digest", "metadata_json", "created_at"},
        },
    },
}
_GOVERNANCE_LEDGER_TABLE = "decisions"
_GOVERNANCE_LEDGER_COLUMNS = frozenset({
    "decision_id", "operation", "target_json", "reason", "confidence", "undo_hash",
    "context_json", "before_json", "after_json", "status", "created_at",
    "idempotency_key", "request_fingerprint",
})


class NativePortError(RuntimeError):
    """Stable, non-leaking native-port failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = str(code or "native_port_error")
        self.detail = str(detail or "")
        super().__init__(self.code)


class NativeContextError(NativePortError):
    """The trusted identity/context boundary rejected a request."""


class _Weakrefable:
    """Compatibility base adding ``__weakref__`` on Python 3.10."""

    __slots__ = ("__weakref__",)


@dataclass(frozen=True, slots=True)
class NativeBoundContext(_Weakrefable):
    """Immutable, process-issued authority used by native transports.

    Public fields are a compatibility projection only.  Native authorization
    reads the exact object registered by :func:`bind_native_transport_context`;
    mutating/copying a surrounding mapping never changes these values.  The
    private issuer token and identity registry also make pickle/hand-crafted
    instances fail closed.
    """

    workspace_id: str
    share_group_id: str
    agent_instance_id: str
    project_ref: str
    provider: str
    runtime_role: str
    admin: bool
    is_admin: bool
    session_id: str
    session_source: str
    session_trusted: bool
    runtime_agent_id: str = ""
    parent_agent_id: str = ""
    context_hash: str = ""
    entrypoint: str = ""
    # Keep the private issuer in its historical positional slot.  New scope
    # fields follow it so old in-process constructor call sites remain
    # compatible while still requiring the issuer sentinel.
    _issuer: object = field(default=None, repr=False, compare=False)
    # Knowledge reads have a narrower ACL tuple than the general runtime
    # identity.  Keep it on the process-issued authority so a request cannot
    # broaden namespace/sensitivity/policy selectors by editing its payload.
    # These remain optional for non-Knowledge surfaces for compatibility;
    # Knowledge handlers require all three explicitly.
    namespace_id: str = ""
    sensitivity: str = ""
    policy_class: str = ""

    def __post_init__(self) -> None:
        if self._issuer is not _NATIVE_CONTEXT_CAPABILITY:
            raise NativeContextError("trusted_context_capability_required")
        object.__setattr__(self, "workspace_id", _text(self.workspace_id))
        object.__setattr__(self, "share_group_id", _text(self.share_group_id))
        object.__setattr__(self, "agent_instance_id", _text(self.agent_instance_id))
        object.__setattr__(self, "project_ref", _text(self.project_ref))
        object.__setattr__(self, "provider", _text(self.provider))
        object.__setattr__(self, "runtime_role", _text(self.runtime_role))
        object.__setattr__(self, "session_id", _text(self.session_id))
        object.__setattr__(self, "session_source", _text(self.session_source).casefold())
        object.__setattr__(self, "runtime_agent_id", _text(self.runtime_agent_id))
        object.__setattr__(self, "parent_agent_id", _text(self.parent_agent_id))
        object.__setattr__(self, "context_hash", _text(self.context_hash))
        object.__setattr__(self, "entrypoint", _text(self.entrypoint))
        object.__setattr__(self, "namespace_id", _text(self.namespace_id))
        object.__setattr__(self, "sensitivity", _text(self.sensitivity))
        object.__setattr__(self, "policy_class", _text(self.policy_class))
        _NATIVE_BOUND_CONTEXTS[id(self)] = self

    def __copy__(self) -> "NativeBoundContext":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")

    def __deepcopy__(self, memo: dict[int, object]) -> "NativeBoundContext":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")

    def __reduce_ex__(self, protocol: int) -> Any:  # pragma: no cover - attack guard
        raise TypeError("native bound context is not serializable")

    def to_dict(self) -> dict[str, Any]:
        """Return a public projection; never include issuer/authority token."""
        return {
            "workspace_id": self.workspace_id,
            "share_group_id": self.share_group_id,
            "agent_instance_id": self.agent_instance_id,
            "trusted_agent_id": self.agent_instance_id,
            "project_ref": self.project_ref,
            "provider": self.provider,
            "runtime_role": self.runtime_role,
            "admin": bool(self.admin),
            "is_admin": bool(self.is_admin),
            "session_id": self.session_id,
            "session_source": self.session_source,
            "session_trusted": bool(self.session_trusted),
            "runtime_agent_id": self.runtime_agent_id,
            "parent_agent_id": self.parent_agent_id,
            "context_hash": self.context_hash,
            "entrypoint": self.entrypoint,
            "namespace_id": self.namespace_id,
            "sensitivity": self.sensitivity,
            "policy_class": self.policy_class,
        }


class NativeContextEnvelope(dict[str, Any]):
    """Mutable compatibility mapping carrying an immutable authority object."""

    __slots__ = ("_bound_context",)

    def __init__(self, bound_context: NativeBoundContext) -> None:
        if not _is_valid_bound_context(bound_context):
            raise NativeContextError("trusted_context_capability_required")
        super().__init__(bound_context.to_dict())
        # Mapping copies used by MCP/GUI retain this exact object.  The key is
        # stripped by public serializers and is never an authorization input.
        self["__native_bound_context"] = bound_context
        self["__native_transport_capability"] = _NATIVE_CONTEXT_CAPABILITY
        object.__setattr__(self, "_bound_context", bound_context)

    @property
    def bound_context(self) -> NativeBoundContext:
        return self._bound_context

    def __copy__(self) -> "NativeContextEnvelope":
        # A shallow envelope copy still carries the original authority; it is
        # not a forged capability.  Deep copy/pickle fail via bound context.
        return NativeContextEnvelope(self._bound_context)

    def __deepcopy__(self, memo: dict[int, object]) -> "NativeContextEnvelope":  # pragma: no cover - attack guard
        raise TypeError("native bound context is not copyable")


def _is_valid_bound_context(value: Any) -> bool:
    return (
        # Exact type is intentional: subclasses can override properties or
        # lifecycle hooks and therefore are not process-issued authorities.
        type(value) is NativeBoundContext
        and _NATIVE_BOUND_CONTEXTS.get(id(value)) is value
        and value._issuer is _NATIVE_CONTEXT_CAPABILITY
    )


def resolve_native_transport_context(context: Any) -> NativeBoundContext:
    """Resolve the canonical process-issued authority for a native context.

    The compatibility envelope may be copied/dictified, but every accepted
    mapping must retain the exact bound object *and* the private process token.
    Public identity fields are never consulted.  Sentinel-only mappings,
    forged lookalikes, stale/GC'd objects, and envelopes whose private object
    was tampered with all fail closed.
    """

    if type(context) is NativeContextEnvelope:
        authority = context.bound_context
        if context.get("__native_bound_context") is not authority:
            raise NativeContextError("trusted_context_capability_required")
    elif type(context) is dict:
        authority = context.get("__native_bound_context")
    elif type(context) is NativeBoundContext:
        # Internal callers may pass the authority directly; it is still
        # checked against the process registry and issuer below.
        authority = context
    else:
        raise NativeContextError("trusted_context_capability_required")
    if context is not authority and type(context) is not NativeBoundContext:
        if type(context) not in {dict, NativeContextEnvelope} or context.get("__native_transport_capability") is not _NATIVE_CONTEXT_CAPABILITY:
            raise NativeContextError("trusted_context_capability_required")
    if not _is_valid_bound_context(authority):
        raise NativeContextError("trusted_context_capability_required")
    return authority


@dataclass
class _SchemaLease:
    """Read-only schema/identity lease held across writable Store init."""

    domain: str
    path: Path
    identity: tuple[int, int, int, int]
    connection: sqlite3.Connection

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:
            pass

    def __enter__(self) -> "_SchemaLease":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.close()
        return False


@dataclass(frozen=True)
class SurfaceSpec:
    """One explicit public operation entry."""

    name: str
    status: str  # implemented | neutral-read | retired | blocker
    handler: str
    mutation: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"implemented", "neutral-read", "retired", "blocker"}:
            raise ValueError("invalid native surface status")
        if self.status == "retired" and not self.reason:
            raise ValueError("retired native surface requires an explicit reason")
        if not self.handler:
            raise ValueError("native surface handler is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "handler": self.handler,
            "mutation": bool(self.mutation),
            "reason": self.reason,
        }


def _plain(value: Any) -> Any:
    if isinstance(value, NativeBoundContext):
        # Authority objects are process-local and must never enter RPC output.
        return "<native-bound-context>"
    if isinstance(value, Mapping):
        return {
            str(k): _plain(v)
            for k, v in value.items()
            if not str(k).startswith("__native_")
        }
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _plain(to_dict())
        except Exception:
            return str(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        try:
            return _plain(as_dict())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(k): v for k, v in value.items() if str(k) != "func"}
    if isinstance(value, (list, tuple)):
        # GUI/CLI compatibility callers pass one business object positionally.
        # Normalize that shape before semantic handlers inspect fields; retain
        # ``args`` for genuine multi-positional calls.
        if len(value) == 1 and isinstance(value[0], Mapping):
            return {str(k): v for k, v in value[0].items() if str(k) != "func"}
        return {"args": list(value)}
    try:
        return {str(k): v for k, v in vars(value).items() if str(k) != "func"}
    except TypeError as exc:
        raise NativePortError("invalid_native_arguments") from exc


def _phase9_gui_payload(surface: str, name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize SafeBridge's positional GUI arguments for native services.

    The browser bridge preserves the historical JS signatures as an ``args``
    list.  Native MCP callers already send mappings, so this adapter is scoped
    to the Phase 9 GUI aliases and never treats nested ``scope`` data as an
    authorization source; canonical authority remains the bound context.
    """

    if surface != "gui":
        return dict(payload)
    values = payload.get("args")
    if not isinstance(values, (list, tuple)):
        return dict(payload)
    args = list(values)
    result: dict[str, Any] = {}

    def assign(names: tuple[str, ...]) -> None:
        for index, key in enumerate(names):
            if index < len(args):
                result[key] = args[index]

    if name == "list_history_sessions":
        assign(("scope", "limit", "offset", "extracted", "date_from", "date_to"))
    elif name == "search_history":
        assign(("query", "scope", "limit", "offset"))
    elif name == "history_timeline":
        assign(("session_id", "anchor_turn_id", "scope", "radius"))
    elif name == "history_read":
        assign(("session_id", "turn_id", "scope", "limit", "offset"))
        # The historical GUI signature uses an empty-string placeholder for
        # whichever selector is not used.  NativeHistoryService distinguishes
        # omitted/None from an invalid empty identifier.
        for key in ("session_id", "turn_id"):
            if result.get(key) == "":
                result[key] = None
    elif name == "history_extract_preview":
        assign(("session_id", "turn_ids", "scope", "limit"))
    elif name == "export_history":
        assign(("session_ids", "scope"))
    elif name == "delete_history":
        assign(("session_ids", "scope", "invalidate_evidence", "confirmed", "mutation_receipt", "idempotency_key"))
    elif name == "list_memory_versions":
        assign(("share_group_id", "limit", "offset"))
    elif name == "get_supersede_chain":
        assign(("memory_id", "share_group_id"))
    elif name == "edit_memory":
        assign(("memory_id", "body"))
    elif name in {"lock_memory", "unlock_memory", "restore_memory", "delete_memory"}:
        assign(("memory_id",))
    elif name == "set_memory_injection_policy":
        assign(("memory_id", "injection_policy", "priority"))
    elif name == "rollback_memory":
        assign(("version_id",))
    elif name == "accept_candidates":
        assign(("extract_id", "candidate_ids"))
    elif name == "apply_enrichments":
        assign(("results",))
    elif name == "extract_preview":
        assign(("root_id", "relative_path", "max_segments"))
    elif name == "extract_preview_by_path":
        assign(("source_path",))
    elif name == "create_rule_from_text":
        assign(("text",))
    elif name == "submit_rule_feedback":
        assign(("receipt_id", "outcome", "evidence", "confidence"))
    elif name == "undo_rule_decision":
        assign(("decision_id",))
    elif name == "read_rule_decision":
        assign(("decision_id",))
    elif name == "list_rule_match_receipts":
        assign(("share_group_id", "memory_id", "agent_instance_id", "limit"))
        result.pop("share_group_id", None)
        result.pop("agent_instance_id", None)
    elif name == "list_rule_exceptions":
        assign(("share_group_id", "parent_rule"))
        result.pop("share_group_id", None)
    elif name == "update_rule_audience":
        assign(("memory_id", "assignments", "share_group_id", "injection_policy", "priority", "confirmed"))
        result.pop("share_group_id", None)
    elif name in {"get_rule_scope_options", "preview_effective_rules", "list_rules_habits", "list_rule_cockpit", "list_rule_decisions", "get_rule_auto_scope_metrics"}:
        # Browser-selected scope/agent fields are intentionally ignored; the
        # process-issued SafeBridge context is authoritative.
        return result
    elif name == "import_external_mcp_entries":
        assign(("descriptor_json", "server_id"))
    elif name in {"preview_source", "preview_import"}:
        if args:
            result["path"] = args[0]
        if name == "preview_source" and len(args) > 1:
            result["source_type"] = args[1]
    elif name == "add_source":
        assign(("path", "source_type", "display_name", "confirmed"))
    elif name == "remove_source":
        assign(("source_id", "confirmed"))
    elif name == "pick_path":
        assign(("for_files",))
    elif name == "detect_external_mcp":
        assign(("server_id", "descriptor"))
    elif name == "preview_external_mcp_import":
        assign(("server_id", "descriptor"))
    elif name in {"list_sources", "scan_sources"}:
        return result
    else:
        return dict(payload)
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bind_native_transport_context(
    access_context: Any,
    *,
    workspace_id: str = "",
    share_group_id: str = "",
    project_ref: str = "",
    provider: str = "",
    runtime_role: str = "",
    runtime_agent_id: str = "",
    parent_agent_id: str = "",
    context_hash: str = "",
    entrypoint: str = "",
    namespace_id: str = "",
    sensitivity: str = "",
    policy_class: str = "",
) -> NativeContextEnvelope:
    """Issue a process-local capability envelope for a trusted transport.

    Native mutation handlers never accept a plain identity mapping as proof of
    provenance.  Hosts call this helper with their process-created
    ``AccessContext`` object; the private sentinel cannot be reproduced by
    JSON/RPC payloads.  Knowledge callers additionally provide the exact
    ``namespace_id``, ``sensitivity`` and ``policy_class`` tuple here.  The
    returned mapping is still safe to pass through existing facade signatures.
    """
    try:
        from ..access_context import AccessContext
    except Exception as exc:  # pragma: no cover - package import failure
        raise NativeContextError("trusted_context_capability_unavailable") from exc
    # Do not accept lookalike/subclass contexts whose properties could be
    # overridden after the host's trust decision.
    if type(access_context) is not AccessContext:
        raise NativeContextError("trusted_context_capability_required")
    authority = NativeBoundContext(
        workspace_id=_text(workspace_id),
        share_group_id=_text(share_group_id),
        agent_instance_id=_text(access_context.trusted_agent_id),
        project_ref=_text(project_ref),
        provider=_text(provider),
        runtime_role=_text(runtime_role),
        admin=bool(access_context.is_admin),
        is_admin=bool(access_context.is_admin),
        session_id=_text(access_context.session_id),
        session_source=_text(access_context.session_source),
        session_trusted=bool(access_context.session_trusted),
        runtime_agent_id=_text(runtime_agent_id),
        parent_agent_id=_text(parent_agent_id),
        context_hash=_text(context_hash),
        entrypoint=_text(entrypoint),
        namespace_id=_text(namespace_id),
        sensitivity=_text(sensitivity),
        policy_class=_text(policy_class),
        _issuer=_NATIVE_CONTEXT_CAPABILITY,
    )
    return NativeContextEnvelope(authority)


@dataclass(frozen=True)
class _NativeInjectionCapability:
    """Process-local opt-in envelope for tests/host adapters.

    This object is intentionally not serializable as a capability: callers
    must receive it from one of the helpers below in this process.  The public
    native factory never creates one and therefore never enables injection.
    """

    token: object
    services: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    stores: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.token is not _NATIVE_INJECTION_CAPABILITY:
            raise NativePortError("native_injection_capability_required")


def bind_native_test_capability(
    *,
    services: Mapping[str, Callable[..., Any]] | None = None,
    stores: Mapping[str, Any] | None = None,
    **named_stores: Any,
) -> _NativeInjectionCapability:
    """Issue an explicit process-local injection capability for tests.

    Production factories do not call this helper.  ``named_stores`` accepts
    domain names such as ``memory_store``/``evidence_store`` to keep existing
    constructor call sites readable while retaining an unforgeable envelope.
    """

    normalized: dict[str, Any] = {str(key): value for key, value in dict(stores or {}).items()}
    for key, value in named_stores.items():
        if key.endswith("_store"):
            normalized[key[:-6]] = value
    return _NativeInjectionCapability(
        _NATIVE_INJECTION_CAPABILITY,
        services=dict(services or {}),
        stores=normalized,
    )


def bind_native_test_services(services: Mapping[str, Callable[..., Any]]) -> _NativeInjectionCapability:
    """Convenience wrapper for explicit service injection in tests."""

    return bind_native_test_capability(services=services)


def bind_native_test_store(domain: str, store: Any) -> _NativeInjectionCapability:
    """Convenience wrapper for one explicit, schema-validated test store."""

    return bind_native_test_capability(stores={str(domain): store})


class NativeV2RuntimePort:
    """Single native V2 dispatch port for all four public surfaces.

    ``services`` may provide operation-specific callables keyed by either
    ``"surface:name"`` or just ``name``.  The callable receives the sanitized
    payload as its first argument and may accept keyword-only ``context``,
    ``generation`` and ``mutation``.  Supplying a callable is useful for a
    host-specific V2 service while retaining one auditable registry.
    """

    supports_rule_mutation_context = True
    # The cutover facade may bind its trusted manifest provider when it owns
    # this port.  Direct callers must inject one explicitly; a caller-supplied
    # ``state=V2_ACTIVE`` value is never sufficient for a mutation.
    requires_state_provider = True

    # Operations with a concrete V2 implementation in this repository.  GUI
    # names below are aliases to these semantic handlers; all other names are
    # explicit blockers rather than accidental success paths.
    _MCP_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        # Phase 9 native evidence/source/diagnostics services.  These are
        # production builtins (never generic ``services=`` overrides).  Keep
        # list/scan conservative writes because the canonical MCP ledger still
        # classifies them as mutation surfaces until that ledger is revised.
        "memoryguard_audit": ("reference_audit", "implemented", True),
        "memoryguard_explain": ("explain", "implemented", False),
        "memoryguard_list_sources": ("list_sources", "implemented", True),
        "memoryguard_scan_summary": ("scan_summary", "implemented", True),
        "memoryguard_import_preview": ("import_preview", "implemented", False),
        "memoryguard_runtime_processes": ("runtime_processes", "implemented", False),
        "memoryguard_history_search": ("history_search", "implemented", False),
        "memoryguard_history_timeline": ("history_timeline", "implemented", False),
        "memoryguard_history_read": ("history_read", "implemented", False),
        "memoryguard_history_extract_preview": ("history_extract_preview", "implemented", False),
        "memoryguard_history_list_sessions": ("history_list_sessions", "implemented", False),
        "memoryguard_history_export": ("history_export", "implemented", False),
        "memoryguard_history_delete": ("history_delete", "implemented", True),
        "memoryguard_memory_read": ("memory_read", "implemented", False),
        "memoryguard_memory_search": ("memory_search", "implemented", False),
        "memoryguard_memory_status": ("memory_status", "implemented", False),
        "memoryguard_memory_write": ("memory_write", "implemented", True),
        "memoryguard_memory_update": ("memory_update", "implemented", True),
        "memoryguard_memory_delete": ("memory_delete", "implemented", True),
        "memoryguard_context_bootstrap": ("context_bootstrap", "implemented", False),
        "memoryguard_rule_create_auto": ("rule_create_auto", "implemented", True),
        "memoryguard_rule_feedback": ("rule_feedback", "implemented", True),
        "memoryguard_rule_undo": ("rule_undo", "implemented", True),
        "memoryguard_rule_decision_read": ("rule_decision_read", "implemented", False),
        "memoryguard_rule_scope_stats": ("rule_scope_stats", "implemented", False),
        "memoryguard_binding_list": ("binding_list", "implemented", False),
        "memoryguard_binding_create": ("binding_create", "implemented", True),
        "memoryguard_rule_merge_capability_issue": ("rule_merge_capability_issue", "implemented", True),
        "memoryguard_rule_merge_approve": ("rule_merge_approve", "implemented", True),
        "memoryguard_rule_merge_acknowledge": ("rule_merge_acknowledge", "implemented", True),
        "memoryguard_rule_merge_cooldown_clear": ("rule_merge_cooldown_clear", "implemented", True),
        "memoryguard_extract_memories": ("extract_memories", "implemented", True),
        "memoryguard_accept_candidates": ("accept_candidates", "implemented", True),
        "memoryguard_list_pending_enrichments": ("list_pending_enrichments", "implemented", False),
        "memoryguard_apply_enrichments": ("apply_enrichments", "implemented", True),
        "memoryguard_enrichment_status": ("enrichment_status", "implemented", False),
        "memoryguard_build_and_enrich": ("build_and_enrich", "implemented", True),
        "memoryguard_resolve_group": ("resolve_group", "implemented", False),
        "memoryguard_external_mcp_list": ("external_mcp_list", "implemented", False),
        "memoryguard_external_mcp_import": ("external_mcp_import", "implemented", True),
        "memoryguard_projection_status": ("projection_status", "implemented", False),
        "memoryguard_canonical_status": ("canonical_status", "implemented", False),
        "memoryguard_diagnostics_snapshot": ("diagnostics_snapshot", "implemented", False),
        "memoryguard_knowledge_list": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_search": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_read": ("knowledge_read", "implemented", False),
        "memoryguard_knowledge_book": ("knowledge_book", "implemented", False),
        "memoryguard_knowledge_candidates": ("knowledge_candidates", "implemented", False),
        "memoryguard_neuron_graph": ("codegraph_graph", "implemented", False),
        "memoryguard_semantic_check": ("semantic_check", "implemented", False),
        "memoryguard_provider_install": ("provider_install", "implemented", True),
        "memoryguard_codegraph_status": ("codegraph_status", "neutral-read", False),
        "memoryguard_asset_status": ("asset_status", "neutral-read", False),
        "memoryguard_skill_status": ("skill_status", "neutral-read", False),
    }
    _GUI_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        # Phase 9 GUI aliases to the same production native services used by
        # MCP.  ``delete_history`` is intentionally mutation-gated by the
        # canonical GUI ledger (V2_ACTIVE/CAS + durable receipt/idempotency).
        "list_history_sessions": ("history_list_sessions", "implemented", False),
        "search_history": ("history_search", "implemented", False),
        "history_timeline": ("history_timeline", "implemented", False),
        "history_read": ("history_read", "implemented", False),
        "history_extract_preview": ("history_extract_preview", "implemented", False),
        "export_history": ("history_export", "implemented", False),
        "delete_history": ("history_delete", "implemented", "delete_history" in GUI_MUTATION_NAMES),
        "list_sources": ("list_sources", "implemented", False),
        "preview_source": ("import_preview", "implemented", False),
        "scan_sources": ("scan_summary", "implemented", False),
        "preview_import": ("import_preview", "implemented", False),
        "list_external_mcp_servers": ("external_mcp_list", "implemented", False),
        "preview_external_mcp_import": ("external_mcp_preview", "implemented", False),
        "detect_external_mcp": ("external_mcp_detect", "implemented", False),
        "list_memory": ("memory_list", "implemented", False),
        "get_memory": ("memory_read", "implemented", False),
        "search_memory": ("memory_search", "implemented", False),
        "get_memory_status": ("memory_status", "implemented", False),
        "list_memory_versions": ("memory_versions", "implemented", False),
        "get_supersede_chain": ("memory_supersede_chain", "implemented", False),
        # Global status is intentionally reduced to the same fixed
        # availability envelope as the scoped status route.  The native port
        # must not aggregate every tenant's store merely to satisfy this
        # compatibility spelling.
        "get_global_memory_status": ("memory_status", "implemented", False),
        "list_bindings": ("binding_list", "implemented", False),
        "get_governance_snapshot": ("diagnostics_snapshot", "implemented", False),
        # The GUI registry endpoint is a read-only native coverage view.  It
        # exposes no legacy security store, paths, or contents.
        "get_api_method_registry": ("coverage", "implemented", False),
        "get_governance_scope": ("scope_echo", "implemented", False),
        "get_governance_scope_state": ("scope_echo", "implemented", False),
        "get_host_hook_status": ("hook_status", "implemented", False),
        "get_sandbox_status": ("sandbox_status", "implemented", False),
        "get_host_enrichment_guide": ("host_enrichment_guide", "implemented", False),
        "list_host_llm_agents": ("host_llm_agents", "implemented", False),
        "get_neuron_graph": ("codegraph_graph", "implemented", False),
        "list_pending_enrichments": ("list_pending_enrichments", "implemented", False),
        "get_enrichment_status": ("enrichment_status", "implemented", False),
        "get_audit": ("reference_audit", "implemented", False),
        "run_audit": ("reference_audit", "implemented", False),
        "get_storage_overview": ("diagnostics_snapshot", "implemented", False),
        "list_history": ("history_list_sessions", "implemented", False),
        "accept_candidates": ("accept_candidates", "implemented", True),
        "apply_enrichments": ("apply_enrichments", "implemented", True),
        "extract_preview": ("gui_extract_preview", "implemented", True),
        "extract_preview_by_path": ("gui_extract_by_path", "implemented", True),
        "create_rule_from_text": ("gui_rule_create", "implemented", True),
        "submit_rule_feedback": ("gui_rule_feedback", "implemented", True),
        "undo_rule_decision": ("gui_rule_undo", "implemented", True),
        "import_external_mcp_entries": ("external_mcp_import", "implemented", True),
        "knowledge_book": ("knowledge_book", "implemented", False),
        "knowledge_candidates_list": ("knowledge_candidates", "implemented", False),
        "call_readonly": ("bridge_transport", "implemented", False),
        "request_mutation": ("bridge_transport", "implemented", True),
        "pick_path": ("gui_pick_path", "implemented", False),
        "add_source": ("gui_source_add", "implemented", True),
        "remove_source": ("gui_source_remove", "implemented", True),
        "get_memory_source_map": ("gui_memory_source_map", "implemented", False),
        "list_share_groups": ("gui_groups", "implemented", False),
        # SafeBridge supplies a process-issued native capability, so these
        # writes can use the same GovernanceV2 mutation ledger as MCP.
        "edit_memory": ("gui_memory_edit", "implemented", True),
        "lock_memory": ("gui_memory_lock", "implemented", True),
        "unlock_memory": ("gui_memory_unlock", "implemented", True),
        "set_memory_injection_policy": ("gui_memory_policy", "implemented", True),
        "restore_memory": ("gui_memory_restore", "implemented", True),
        "delete_memory": ("gui_memory_delete", "implemented", True),
        "list_rules_habits": ("gui_rule_snapshot", "implemented", False),
        "list_rule_decisions": ("gui_rule_decisions", "implemented", False),
        "read_rule_decision": ("rule_decision_read", "implemented", False),
        "get_rule_auto_scope_metrics": ("rule_scope_stats", "implemented", False),
        "list_rule_cockpit": ("gui_rule_snapshot", "implemented", False),
        "get_rule_scope_options": ("gui_rule_scope_options", "implemented", False),
        "preview_effective_rules": ("gui_rule_effective", "implemented", False),
        "list_rule_match_receipts": ("gui_rule_receipts", "implemented", False),
        "list_rule_exceptions": ("gui_rule_exceptions", "implemented", False),
        "update_rule_audience": ("gui_rule_audience_update", "implemented", True),
        "knowledge_list": ("knowledge_read", "implemented", False),
        "knowledge_search": ("knowledge_read", "implemented", False),
        "knowledge_read": ("knowledge_read", "implemented", False),
    }

    # Explicit V1-only GUI workflows retained as stable compatibility names.
    # ``retired`` is a production-safe terminal classification: the method
    # remains discoverable but dispatch returns v2_operation_retired with this
    # reason and performs no legacy fallback or write.
    _GUI_RETIRED_REASONS: dict[str, str] = {}
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "apply_build", "choose_publish_target_path", "create_build_plan",
        "get_build_progress", "list_native_memory_releases", "list_publish_targets",
        "list_releases", "publish_reconstructed_memory",
        "rollback_native_memory_release", "rollback_release", "verify_release",
    }, "legacy reconstructed/native-memory release workflow is replaced by V2 Memory, Projection and governed domain receipts"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "apply_plan", "generate_plan", "undo_change",
    }, "legacy report-patch workflow is replaced by V2 ReferenceAudit, validator/readiness and governed domain mutations"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "apply_memoryguard_gc", "plan_memoryguard_gc",
    }, "legacy artifact GC is replaced by V2 storage lease/sweep/compact maintenance"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "archive_memory_group", "bind_agent", "bind_agents_to_shared_group",
        "check_binding_drift", "clear_memory_group", "commit_selection",
        "commit_shared_memory_governance", "dissolve_shared_group",
        "ensure_personal_memory_group", "enter_multi_agent_mode", "exit_multi_agent_mode",
        "export_memory_group", "get_shared_group_preview", "import_native_memories_to_group",
        "install_shared_group_mcp_redirects", "leave_shared_group_to_personal", "unbind_agent",
    }, "legacy SharedMemoryStore group lifecycle is retired; V2 scope comes from trusted bindings/rules and provider integration"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "build_projection", "cancel_build_projection", "delete_projection",
        "get_projection_source_map", "neuron_decide", "set_projection_source_enabled",
        "start_build_projection",
    }, "legacy reconstructed projection workflow is replaced by V2 Projection/CodeGraph domains"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "create_child_exception", "create_rule_exception", "revoke_rule_exception",
    }, "direct rule-exception editing is retired; V2 exceptions are evidence/receipt-driven through the native rule lifecycle"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "backfill_local_history", "discover_local_history_sources",
    }, "legacy history discovery/backfill is replaced by the V2 Content/History synchronization domain"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "create_import",
    }, "legacy import staging is replaced by V2 Content/History/Knowledge ingestion and extraction surfaces"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "delete_quarantine", "get_auto_actions", "get_conflicts", "get_memory_ir",
        "get_quarantine", "get_raw_memory", "get_recent_events", "get_source_file_content",
        "get_supersede_decisions", "release_quarantine", "resolve_conflict",
    }, "legacy IR/quarantine/raw-body workflow is replaced by V2 governed Memory revisions, source mappings and scoped evidence reads"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "get_request_status", "list_pending_requests", "submit_request",
    }, "legacy GUI request queue is retired; SafeBridge mutations execute only through the V2 manifest/context gate"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "knowledge_add", "knowledge_candidate_review", "knowledge_candidate_targets",
        "knowledge_deleted_list", "knowledge_job_status", "knowledge_purge_deleted",
        "knowledge_rebuild_smart", "knowledge_reingest", "knowledge_remove",
        "knowledge_restore", "knowledge_update_settings",
    }, "Knowledge V2 is reference-only by contract; body mutations must enter through Content ingestion/extraction"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "archive_agent_dir", "delete_archived_agent", "discover_agents", "get_agent_data",
        "get_residual_cleanup", "get_selection_tree", "list_agent_candidates", "list_agents",
        "list_archived_agents", "list_cleanup_history", "mark_agent_uninstalled",
        "open_agent_folder", "restore_archived_agent", "unmark_agent_uninstalled",
    }, "legacy AgentLocator cleanup/selection workflow is retired from the V2 data plane; provider/source control remains available through native control surfaces"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "set_governance_scope",
    }, "browser scope preferences are not V2 authority; scope is derived from the process-issued trusted binding context"))
    _GUI_RETIRED_REASONS.update(dict.fromkeys({
        "set_host_hook_mode", "uninstall_host_hook",
    }, "GUI hook mutation is retired in V2; use the gated native CLI hooks control surface"))
    _GUI_RETIRED_REASONS["rollback_memory"] = (
        "legacy whole-version rollback has no lossless V2 equivalent; use append-only revisions and operation-specific governed compensating mutations"
    )

    _CLI_HANDLERS: Mapping[str, tuple[str, str, bool]] = {
        "audit": ("cli_audit", "implemented", False),
        "explain": ("cli_explain", "implemented", False),
        "scan": ("cli_scan", "implemented", False),
        "source": ("cli_source", "implemented", False),
        "provider": ("cli_provider", "implemented", True),
        "hooks": ("cli_hooks", "implemented", False),
        "groups": ("cli_groups", "implemented", False),
        "mcp-status": ("cli_mcp_status", "implemented", False),
        "doctor": ("cli_doctor", "implemented", False),
        # Host UI/executor actions run only after this native manifest gate.
        "gui": ("cli_gui", "implemented", False),
        "open": ("cli_open", "implemented", False),
        "desktop": ("cli_desktop", "implemented", True),
        # MaintenanceRuntimePort owns authoritative V2 storage mutation.
        "storage": ("maintenance", "implemented", False),
        # V1 report-patch/import/GC workflows have explicit V2 replacements.
        "plan": ("retired", "retired", False),
        "apply": ("retired", "retired", True),
        "verify": ("retired", "retired", False),
        "undo": ("retired", "retired", True),
        "import": ("retired", "retired", False),
        "gc": ("retired", "retired", False),
    }
    _CLI_RETIRED_REASONS: Mapping[str, str] = {
        "plan": "legacy report patch planning is replaced by V2 ReferenceAudit and readiness remediation",
        "apply": "legacy source-file patch apply is replaced by explicit V2 domain governance/maintenance operations",
        "verify": "legacy report-file verification is replaced by V2 validator, ReferenceAudit and readiness evidence",
        "undo": "legacy backup-file undo is replaced by V2 domain receipts, compensating mutations and maintenance rollback",
        "import": "legacy offline import staging is replaced by V2 Content/History/Knowledge ingestion surfaces",
        "gc": "legacy artifact GC is replaced by the V2 storage lease/sweep/compact maintenance command",
    }

    def __init__(
        self,
        workspace: str | Path,
        *,
        maintenance_port: Any = None,
        context_engine: Any = None,
        recall_planner: Any = None,
        memory_store: Any = None,
        evidence_store: Any = None,
        governance: Any = None,
        rule_store: Any = None,
        asset_store: Any = None,
        skill_store: Any = None,
        knowledge_adapter: Any = None,
        content_store: Any = None,
        codegraph_store: Any = None,
        projection_store: Any = None,
        rule_merge_store: Any = None,
        services: Mapping[str, Callable[..., Any]] | _NativeInjectionCapability | None = None,
        state_provider: Any = None,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        self.layout = WorkspaceV2Layout(self.workspace)
        self.maintenance_port = maintenance_port
        self.context_engine = context_engine
        self.recall_planner = recall_planner
        injection_services: Mapping[str, Callable[..., Any]] = {}
        injection_stores: Mapping[str, Any] = {}
        if isinstance(services, _NativeInjectionCapability):
            injection_services = services.services
            injection_stores = services.stores
        elif services is not None:
            # A plain Mapping is indistinguishable from a JSON/RPC payload and
            # therefore cannot carry a native service capability.  Even
            # neutral reads must opt into the explicit process-local seam.
            raise NativePortError("native_service_injection_capability_required")
        if governance is not None and not isinstance(governance, _NativeInjectionCapability):
            # Keep a stable governance-specific code for callers while still
            # requiring explicit capability wrapping for test seams.
            raise NativePortError("native_governance_injection_forbidden")
        store_arguments = {
            "memory": memory_store,
            "evidence": evidence_store,
            "governance": governance,
            "rules": rule_store,
            "assets": asset_store,
            "skills": skill_store,
            "knowledge": knowledge_adapter,
            "content": content_store,
            "codegraph": codegraph_store,
            "projection": projection_store,
            "rule_merge": rule_merge_store,
        }
        # No raw store/facade object may enter the native boundary.  Explicit
        # test seams must carry the process-local injection capability, even
        # for read-only domains; this keeps constructor call sites from
        # silently becoming mutation bypasses as handlers evolve.
        injected_stores = {
            domain: self._unwrap_store_injection(domain, value, injection_stores)
            for domain, value in store_arguments.items()
        }
        self._stores: dict[str, Any] = {
            **injected_stores,
        }
        # Named store wrappers can be supplied through the generic capability;
        # direct store kwargs are deliberately rejected above.
        for domain in ("memory", "evidence"):
            if self._stores.get(domain) is not None:
                self._stores[domain] = self._validate_injected_store(domain, self._stores[domain])
        self._services = self._validate_service_injection(injection_services, capability=isinstance(services, _NativeInjectionCapability))
        # These adapters are deliberately constructed by the production native
        # port itself rather than accepted through the generic service DI seam.
        # Their implementations are lazy/side-effect-free for reads and return
        # stable NO_SOURCE/neutral/blocked envelopes when backing state is
        # missing or unavailable.
        self._history_service: Any = None
        self._source_read_service: Any = None
        self._import_preview_service: Any = None
        self._runtime_diagnostics_service: Any = None
        self._external_mcp_service: Any = None
        self._rule_merge_service: Any = None
        self._rule_lifecycle_service: Any = None
        self._extraction_service: Any = None
        self._knowledge_service: Any = None
        self._native_service_init_errors: dict[str, str] = {}
        self.state_provider = state_provider
        self._registry = self._make_registry()

    @staticmethod
    def _unwrap_store_injection(domain: str, value: Any, capability_stores: Mapping[str, Any]) -> Any:
        if value is None:
            return capability_stores.get(domain)
        if not isinstance(value, _NativeInjectionCapability):
            raise NativePortError("native_store_injection_capability_required")
        injected = value.stores.get(domain)
        if injected is None:
            raise NativePortError("native_store_injection_domain_required")
        return injected

    @classmethod
    def _validate_service_injection(
        cls,
        services: Mapping[str, Callable[..., Any]],
        *,
        capability: bool,
    ) -> dict[str, Callable[..., Any]]:
        result = dict(services)
        # No service override may replace a mutation boundary.  Test/host
        # injection is deliberately limited to non-mutating reads; maintenance
        # writes have their own private ``maintenance_port`` capability.
        mutating_names = set(MCP_MUTATION_NAMES)
        mutating_names.update(
            name for name, (_, _, mutation) in cls._MCP_HANDLERS.items() if mutation
        )
        mutating_names.update(
            handler for _, (handler, _, mutation) in cls._MCP_HANDLERS.items() if mutation
        )
        mutating_names.update(
            name for name, (_, _, mutation) in cls._GUI_HANDLERS.items() if mutation
        )
        mutating_names.update(
            handler for _, (handler, _, mutation) in cls._GUI_HANDLERS.items() if mutation
        )
        mutating_names.update({"maintenance", "binding_create", "rule_create", "rule_update", "rule_delete"})
        for key in result:
            operation = str(key).split(":", 1)[-1]
            if operation in mutating_names:
                raise NativePortError("native_mutation_service_override_forbidden")
        for key, fn in result.items():
            if not callable(fn):
                raise NativePortError("native_service_callable_required")
        return result

    def _validate_injected_store(self, domain: str, store: Any) -> Any:
        """Validate an explicitly injected read-only store against this DB."""

        if store is None:
            return None
        if getattr(store, "readonly", None) is not True and getattr(store, "read_only", None) is not True:
            raise NativePortError("readonly_store_capability_required")
        expected = self.layout.db_paths(domain)[0]
        actual = getattr(store, "db_path", None) or getattr(store, "path", None)
        if actual is None or Path(actual).expanduser().resolve() != expected.resolve():
            raise NativePortError("injected_store_identity_mismatch")
        # Re-run the same read-only validator used by lazy builtins.  This is
        # intentionally done before the store becomes reachable by a handler.
        self._preflight_domain(domain)
        return store

    # ---- registry / evidence -------------------------------------------------
    def _make_registry(self) -> dict[str, dict[str, SurfaceSpec]]:
        registry: dict[str, dict[str, SurfaceSpec]] = {"mcp": {}, "gui": {}, "cli": {}, "hook": {}}
        for name in sorted(MCP_TOOL_NAMES):
            handler, status, mutation = self._MCP_HANDLERS.get(
                name, ("unsupported", "blocker", name in MCP_MUTATION_NAMES),
            )
            reason = "" if status != "blocker" else "v2 operation has no native V2 service"
            registry["mcp"][name] = SurfaceSpec(name, status, handler, mutation, reason)
        for name in sorted(GUI_METHOD_NAMES):
            configured = self._GUI_HANDLERS.get(name)
            if configured is not None:
                handler, status, mutation = configured
                reason = "" if status != "blocker" else "v2 GUI semantic handler is not activated"
            elif name in self._GUI_RETIRED_REASONS:
                handler, status, mutation = "retired", "retired", name in GUI_MUTATION_NAMES
                reason = self._GUI_RETIRED_REASONS[name]
            else:
                handler, status, mutation = "unsupported", "blocker", name in GUI_MUTATION_NAMES
                reason = "v2 GUI semantic handler is not activated"
            registry["gui"][name] = SurfaceSpec(name, status, handler, mutation, reason)
        for name in sorted(CLI_COMMAND_NAMES):
            handler, status, mutation = self._CLI_HANDLERS.get(
                name, ("unsupported", "blocker", False),
            )
            if status == "blocker":
                reason = "v2 CLI command is not activated"
            elif status == "retired":
                reason = str(self._CLI_RETIRED_REASONS.get(name) or "legacy CLI operation retired in V2")
            else:
                reason = ""
            registry["cli"][name] = SurfaceSpec(name, status, handler, mutation, reason)
        registry["hook"]["bootstrap_hook"] = SurfaceSpec(
            "bootstrap_hook", "implemented", "hook_bootstrap", False,
        )
        return registry

    @property
    def surface_registry(self) -> Mapping[str, Mapping[str, SurfaceSpec]]:
        return {surface: dict(entries) for surface, entries in self._registry.items()}

    def registry(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            surface: {name: spec.to_dict() for name, spec in entries.items()}
            for surface, entries in self._registry.items()
        }

    def coverage(self) -> dict[str, Any]:
        surfaces: dict[str, Any] = {}
        canonical: dict[str, Any] = {}
        for surface in ("mcp", "gui", "cli", "hook"):
            entries = [self._registry[surface][name].to_dict() for name in sorted(self._registry[surface])]
            counts = {status: sum(item["status"] == status for item in entries) for status in ("implemented", "neutral-read", "retired", "blocker")}
            surfaces[surface] = {"total": len(entries), **counts, "entries": entries}
            canonical[surface] = entries
        digest = hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()
        total = sum(item["total"] for item in surfaces.values())
        counts = {status: sum(item[status] for item in surfaces.values()) for status in ("implemented", "neutral-read", "retired", "blocker")}
        # ``complete`` is the historical blocker-only compatibility field.
        # ``neutral-read`` is diagnostic-only and therefore excluded from the
        # production readiness claim carried by ``production_complete``.
        complete = counts["blocker"] == 0
        production_complete = complete and counts["neutral-read"] == 0
        return {
            "schema": "v2-native-coverage-1",
            "registry_digest": digest,
            "coverage_digest": digest,
            "surfaces": surfaces,
            "counts": {"total": total, **counts},
            "complete": complete,
            "production_complete": production_complete,
        }

    @property
    def coverage_digest(self) -> str:
        return str(self.coverage()["registry_digest"])

    @property
    def registry_digest(self) -> str:
        """Stable digest alias used by activation evidence assemblers."""
        return self.coverage_digest

    # ---- stable envelopes ----------------------------------------------------
    @staticmethod
    def _error(surface: str, name: str, code: str, *, status: str = "error", generation: int | None = None, reason: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "status": status,
            "surface": surface,
            "name": name,
            "path": "v2",
            "code": code,
            "error": code,
        }
        if generation is not None:
            payload["generation"] = generation
        if reason:
            payload["reason"] = reason
        return payload

    @staticmethod
    def _result(surface: str, name: str, data: Any, *, generation: int, status: str = "ok") -> dict[str, Any]:
        # Existing V2 service ports (notably MaintenanceRuntimePort) already
        # return a transport envelope.  Preserve its data at the facade's
        # top-level instead of nesting ``data.data`` and losing stable CLI
        # fields such as ``candidate_count``.
        if isinstance(data, Mapping):
            payload = dict(_plain(data))
            raw_status = _text(payload.get("status")).casefold()
            failed = (
                payload.get("ok") is False
                or raw_status in {"error", "blocked", "failed"}
                or bool(payload.get("error"))
            )
            if failed:
                # A service envelope may contain arbitrary diagnostic fields.
                # Failure classification must happen before the legacy
                # transport-envelope shape check; otherwise ``ok=false`` plus
                # one extra field gets nested as data and the outer call looks
                # successful.
                payload.update({"surface": surface, "name": name, "path": "v2", "generation": generation})
                payload["ok"] = False
                payload["status"] = raw_status if raw_status in {"error", "blocked", "failed"} else "error"
                payload.setdefault("code", payload.get("error") or "v2_native_handler_failed")
                payload.setdefault("error", payload.get("code"))
                return payload
            # Existing V2 service ports (notably MaintenanceRuntimePort) already
            # return a transport envelope.  Preserve its data at the facade's
            # top-level instead of nesting ``data.data`` and losing stable CLI
            # fields such as ``candidate_count``.
            if set(data).issubset({"ok", "status", "data", "code", "error", "reason"}) and "data" in data:
                payload.update({"surface": surface, "name": name, "path": "v2", "generation": generation})
                payload.setdefault("status", status)
                payload.setdefault("ok", payload.get("status") == "ok")
                return payload
            # Fixed availability responses are intentionally diagnostic-only.
            # Keep the business data shape stable while making the outer
            # transport status explicit for readiness/coverage consumers.
            diagnostic_only = payload.get("available") is True and set(payload).issubset({"available", "diagnostic_only"})
            return {
                "ok": True,
                "status": "diagnostic_only" if diagnostic_only else status,
                "surface": surface,
                "name": name,
                "path": "v2",
                "generation": generation,
                "data": payload,
            }
        return {
            "ok": status == "ok",
            "status": status,
            "surface": surface,
            "name": name,
            "path": "v2",
            "generation": generation,
            "data": _plain(data),
        }

    # ---- identity and scope --------------------------------------------------
    @staticmethod
    def _mapping_context(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if type(value) is NativeContextEnvelope or type(value) is NativeBoundContext:
            authority = resolve_native_transport_context(value)
            return {
                **authority.to_dict(),
                "__native_bound_context": authority,
                "__native_transport_capability": _NATIVE_CONTEXT_CAPABILITY,
            }
        if type(value) is dict:
            raw = dict(value)
            if "__native_bound_context" in raw:
                authority = resolve_native_transport_context(raw)
                # The public projection is advisory.  Always replace it with
                # the process-issued immutable authority values.
                return {
                    **authority.to_dict(),
                    "__native_bound_context": authority,
                    "__native_transport_capability": _NATIVE_CONTEXT_CAPABILITY,
                }
            return raw
        # Native context security must never consult an arbitrary object's
        # ``to_dict``/attributes: wrappers can return a forged authority map.
        raise NativeContextError("trusted_context_capability_required")

    @staticmethod
    def _resolve_identity(raw: Mapping[str, Any], canonical: str) -> str:
        values = [_text(raw[key]) for key in _IDENTITY_ALIASES[canonical] if key in raw and _text(raw[key])]
        if len(set(values)) > 1:
            raise NativeContextError("context_identity_conflict")
        return values[0] if values else ""

    def _context(self, raw: Any, payload: Mapping[str, Any] | None, *, required: bool, allow_partial: bool = False) -> dict[str, Any]:
        source = self._mapping_context(raw)
        if not source:
            if required:
                raise NativeContextError("native_context_required")
            return {}
        context: dict[str, Any] = {}
        for canonical in _IDENTITY_ALIASES:
            context[canonical] = self._resolve_identity(source, canonical)
        workspace_id = context["workspace_id"] or self.workspace
        if os.path.abspath(os.fspath(Path(workspace_id).expanduser())) != os.path.abspath(self.workspace):
            raise NativeContextError("context_workspace_mismatch")
        context["workspace_id"] = self.workspace
        if required and not context["agent_instance_id"]:
            raise NativeContextError("context_identity_required")
        if required and not allow_partial and not context["share_group_id"]:
            raise NativeContextError("context_identity_required")
        # A request body can carry business data, but never a second identity.
        for key, value in (payload or {}).items():
            if key not in _IDENTITY_PAYLOAD_KEYS or value in (None, ""):
                continue
            if key in {"trusted_identity", "trusted_context", "identity"}:
                raise NativeContextError("context_identity_spoof")
            canonical = next((name for name, aliases in _IDENTITY_ALIASES.items() if key in aliases), "")
            expected = context.get(canonical, "") if canonical else ""
            if expected and _text(value) != expected:
                raise NativeContextError("context_identity_spoof")
        # Keep provenance fields available to ContextEngine and governance
        # stores, but they are copied from the trusted context only.
        for key in (
            "admin", "is_admin", "authority", "automatic", "session_id",
            "session_source", "session_trusted", "context_hash",
            "runtime_agent_id", "parent_agent_id", "namespace_id",
            "sensitivity", "policy_class",
        ):
            if key in source:
                context[key] = source[key]
        # MaintenanceRuntimePort uses a process-local capability object.  It
        # is not an identity field and must survive this normalization intact;
        # callers cannot forge it by serializing a guessed value.
        for key, value in source.items():
            if str(key).startswith("__"):
                context[str(key)] = value
        context.setdefault("actor", context.get("agent_instance_id", ""))
        context.setdefault("authority", "admin" if bool(context.get("admin")) else "manual")
        # Maintenance transport still consumes its historical trusted-agent
        # spelling.  Mirror the canonical value; it is derived, never read
        # from an untrusted payload.
        context.setdefault("trusted_agent_id", context.get("agent_instance_id", ""))
        context["trusted_identity"] = {
            key: value for key, value in context.items() if key in {
                "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "workspace_id",
            }
        }
        return context

    def _scope(self, context: Mapping[str, Any]) -> dict[str, Any]:
        if not context.get("workspace_id") or not context.get("share_group_id"):
            raise NativeContextError("context_scope_required")
        return {
            "workspace_id": self.workspace,
            "share_group_id": context.get("share_group_id", ""),
            "agent_instance_id": context.get("agent_instance_id", ""),
            "project_ref": context.get("project_ref", ""),
            "provider": context.get("provider", ""),
            "runtime_role": context.get("runtime_role", ""),
        }

    def _mutation_context(self, context: Mapping[str, Any]) -> dict[str, Any]:
        scope = self._scope(context)
        scope.update({
            "actor": context.get("actor") or scope.get("agent_instance_id"),
            "admin": bool(context.get("admin", context.get("is_admin", False))),
            "authority": str(context.get("authority") or ("admin" if context.get("admin") else "manual")),
        })
        return scope

    @staticmethod
    def _trusted_admin(context: Mapping[str, Any]) -> bool:
        """Return true only for an admin AccessContext with trusted session provenance.

        ``is_admin`` alone is a client-controlled mapping when callers invoke
        the native port directly.  AccessContext serialisation carries the
        immutable session tuple; requiring it here keeps governance writes
        fail-closed even when the transport wrapper is bypassed.
        """
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError:
            return False
        # Read authority fields from the immutable object, never the mutable
        # compatibility mapping surrounding it.
        is_admin = bool(authority.admin)
        session_id = authority.session_id
        source = authority.session_source.casefold()
        return bool(
            is_admin
            and authority.session_trusted is True
            and session_id
            and source in {"host", "transport"}
        )

    # ---- lazy stores ---------------------------------------------------------
    @staticmethod
    def _schema_blocker(kind: str, path: Path, *, detail: str = "") -> NativePortError:
        """Return a stable, non-leaking schema preflight blocker."""

        return NativePortError(kind, detail or str(path.name))

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)), int(stat.st_size), int(stat.st_mtime_ns))

    def _validate_schema_connection(self, domain: str, path: Path, conn: sqlite3.Connection) -> None:
        """Validate one already-open read-only SQLite connection."""

        spec = _NATIVE_SCHEMA_SPECS.get(domain)
        if spec is None:
            raise NativePortError("v2_schema_spec_unavailable")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if not tables:
            raise self._schema_blocker("v2_schema_missing", path)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version > 1:
            raise self._schema_blocker("v2_schema_future", path)
        if user_version not in {0, 1}:
            raise self._schema_blocker("v2_schema_invalid", path)
        for table, expected_domain, expected_marker in spec["meta"]:
            if table not in tables:
                raise self._schema_blocker("v2_schema_partial", path)
            rows = conn.execute(f"SELECT domain, version, marker FROM {table}").fetchall()
            if not rows:
                raise self._schema_blocker("v2_schema_partial", path)
            if len(rows) != 1:
                raise self._schema_blocker("v2_schema_invalid", path)
            row_domain, row_version, row_marker = str(rows[0][0]), int(rows[0][1]), str(rows[0][2])
            if row_version > 1:
                raise self._schema_blocker("v2_schema_future", path)
            if row_domain != expected_domain or row_marker != expected_marker or row_version != 1:
                raise self._schema_blocker("v2_schema_invalid", path)
        for table, required_columns in spec["tables"].items():
            if table not in tables:
                raise self._schema_blocker("v2_schema_partial", path)
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required_columns <= columns:
                raise self._schema_blocker("v2_schema_partial", path)

    def _open_schema_lease(self, domain: str) -> _SchemaLease:
        """Open/validate RO DB and hold it through Store construction."""

        paths = self.layout.db_paths(domain)
        assert isinstance(paths, tuple)
        path = paths[0]
        self.layout.assert_database_path(path, domain)
        if not path.is_file() or path.stat().st_size == 0:
            raise self._schema_blocker("v2_schema_missing", path)
        conn: sqlite3.Connection | None = None
        try:
            conn = connect_database(path, readonly=True)
            self._validate_schema_connection(domain, path, conn)
            return _SchemaLease(domain, path, self._file_identity(path), conn)
        except NativePortError:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            raise self._schema_blocker("v2_schema_invalid", path) from exc

    def _assert_schema_lease(self, lease: _SchemaLease) -> None:
        try:
            if self._file_identity(lease.path) != lease.identity:
                raise self._schema_blocker("v2_schema_replaced", lease.path)
            self._validate_schema_connection(lease.domain, lease.path, lease.connection)
        except NativePortError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise self._schema_blocker("v2_schema_invalid", lease.path) from exc

    def _preflight_domain(self, domain: str) -> Path:
        """Read-only validate one phase-2 database without Store init."""

        with self._open_schema_lease(domain) as lease:
            return lease.path

    def _preflight_governance_ledger(self) -> Path:
        """Validate the durable GovernanceV2 idempotency/receipt ledger."""

        path = self.layout.root / "governance_v2" / "decisions.db"
        if not path.is_file() or path.stat().st_size == 0:
            raise self._schema_blocker("v2_governance_ledger_missing", path)
        with self._open_governance_lease(path) as lease:
            return lease.path

    def _open_governance_lease(self, path: Path) -> _SchemaLease:
        """Open and hold the GovernanceV2 ledger RO across boundary init."""

        conn: sqlite3.Connection | None = None
        try:
            conn = connect_database(path, readonly=True)
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if _GOVERNANCE_LEDGER_TABLE not in tables:
                raise self._schema_blocker("v2_governance_ledger_partial", path)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(decisions)")}
            if not _GOVERNANCE_LEDGER_COLUMNS <= columns:
                raise self._schema_blocker("v2_governance_ledger_partial", path)
            return _SchemaLease("governance", path, self._file_identity(path), conn)
        except NativePortError:
            if conn is not None:
                conn.close()
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            if conn is not None:
                conn.close()
            raise self._schema_blocker("v2_governance_ledger_invalid", path) from exc

    def _assert_governance_lease(self, lease: _SchemaLease) -> None:
        try:
            if self._file_identity(lease.path) != lease.identity:
                raise self._schema_blocker("v2_governance_ledger_replaced", lease.path)
            tables = {str(row[0]) for row in lease.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {str(row[1]) for row in lease.connection.execute("PRAGMA table_info(decisions)")}
            if _GOVERNANCE_LEDGER_TABLE not in tables or not _GOVERNANCE_LEDGER_COLUMNS <= columns:
                raise self._schema_blocker("v2_governance_ledger_partial", lease.path)
        except NativePortError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            raise self._schema_blocker("v2_governance_ledger_invalid", lease.path) from exc

    def _preflight_memory_governance(self) -> None:
        self._preflight_domain("memory")
        self._preflight_domain("evidence")
        self._preflight_governance_ledger()

    def _assert_existing_db(self, domain: str) -> Path:
        paths = self.layout.db_paths(domain)
        assert isinstance(paths, tuple)
        path = paths[0]
        self.layout.assert_database_path(path, domain)
        if not path.is_file():
            raise NativePortError("v2_store_unavailable")
        return path

    def _domain_store(self, domain: str, *, write: bool = False) -> Any:
        key = f"{domain}:{'write' if write else 'read'}"
        if key in self._stores and self._stores[key] is not None:
            return self._stores[key]
        injected = self._stores.get(domain)
        # Every injected store is a read-only test seam.  Writes always use
        # builtins constructed after schema/ledger leases; an explicit
        # capability cannot substitute a mutation path for any domain.
        if injected is not None and not write:
            self._stores[key] = injected
            return injected
        lease: _SchemaLease | None = None
        if domain in _NATIVE_SCHEMA_SPECS:
            lease = self._open_schema_lease(domain)
        else:
            self._assert_existing_db(domain)
        value: Any
        try:
            if lease is not None:
                self._assert_schema_lease(lease)
            if domain == "memory":
                from ..memory.store import MemoryAtomStore
                value = MemoryAtomStore(self.workspace, readonly=not write)
            elif domain == "evidence":
                from ..evidence.store import EvidenceStore
                value = EvidenceStore(self.workspace, readonly=not write)
            elif domain == "rules":
                from ..rules.v2_store import RuleV2Store
                value = RuleV2Store(self.workspace, read_only=not write)
            elif domain == "assets":
                from ..assets_v2.store import AssetStore
                value = AssetStore(self.workspace, readonly=not write, initialize=False)
            elif domain == "skills":
                from ..skills_v2.store import SkillStore
                value = SkillStore(self.workspace, readonly=not write)
            elif domain == "content":
                from ..content.store import ContentStore
                value = ContentStore(self.workspace, initialize=False)
            elif domain == "codegraph":
                from ..codegraph_v2.store import CodeGraphStore
                value = CodeGraphStore(self.workspace, initialize=False)
                # CodeGraphStore's writable constructor is intentionally not
                # used here.  Run its read-only schema preflight explicitly so
                # partial/future databases fail closed before graph reads.
                preflight = getattr(value, "_preflight", None)
                if not callable(preflight):
                    raise NativePortError("v2_codegraph_schema_unavailable")
                preflight()
            elif domain == "projection":
                from ..projection_v2.store import ProjectionStore
                value = ProjectionStore(self.workspace, initialize=False)
            else:
                raise NativePortError("v2_store_unavailable")
            if lease is not None:
                self._assert_schema_lease(lease)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_store_schema_invalid") from exc
        finally:
            # The lease intentionally spans the constructor above and the
            # second identity/schema check below; it is never held beyond the
            # resulting store's own lifecycle.
            if lease is not None:
                lease.close()
        self._stores[key] = value
        return value

    def _service(self, surface: str, name: str, handler: str) -> Callable[..., Any] | None:
        # Memory/evidence writes are the security boundary.  Even an explicit
        # test capability may not replace the builtin GovernanceV2 route.
        if handler in {
            "memory_write", "memory_update", "memory_delete",
            # Phase 9 services are production builtins.  A test/host service
            # mapping must not be able to replace the real history/source/
            # import/diagnostics implementation or promote coverage by DI.
            "list_sources", "scan_summary", "import_preview", "runtime_processes",
            "history_search", "history_timeline", "history_read",
            "history_extract_preview", "history_list_sessions", "history_export",
            "history_delete",
            "external_mcp_list", "external_mcp_detect", "external_mcp_preview", "external_mcp_import",
            "knowledge_book", "knowledge_candidates",
            "extract_memories", "accept_candidates", "list_pending_enrichments",
            "apply_enrichments", "enrichment_status", "build_and_enrich", "resolve_group",
            "sandbox_status", "host_enrichment_guide", "host_llm_agents",
            "coverage", "codegraph_graph", "semantic_check", "reference_audit", "provider_install",
            "rule_create_auto", "rule_feedback", "rule_undo", "rule_decision_read", "rule_scope_stats",
        }:
            return None
        return self._services.get(f"{surface}:{name}") or self._services.get(name) or self._services.get(handler)

    @staticmethod
    def _map_governance_error(error: Exception) -> NativePortError:
        """Translate governance failures to stable native transport codes."""

        try:
            from ..governance_v2.context import V2ContextError, V2ScopeError
            from ..governance_v2.boundary import V2GovernanceError
        except Exception:  # pragma: no cover - package import failure
            V2ContextError = V2ScopeError = V2GovernanceError = ()  # type: ignore[assignment]
        if isinstance(error, V2ScopeError):
            return NativePortError("mutation_scope_rejected")
        if isinstance(error, V2ContextError):
            return NativePortError("mutation_context_rejected")
        if isinstance(error, V2GovernanceError):
            message = str(error).casefold()
            if "idempotency" in message or "request" in message:
                return NativePortError("idempotency_conflict")
            return NativePortError("v2_governance_rejected")
        return NativePortError("v2_governance_rejected")

    def _governance_boundary(self) -> Any:
        """Return GovernanceV2 only after all native write preflights pass."""

        from ..governance_v2 import GovernanceV2

        memory = self._domain_store("memory", write=True)
        evidence = self._domain_store("evidence", write=True)
        ledger_path = self.layout.root / "governance_v2" / "decisions.db"
        ledger = self._open_governance_lease(ledger_path)
        try:
            self._assert_governance_lease(ledger)
            value = GovernanceV2(self.workspace, memory_store=memory, evidence_store=evidence)
            self._assert_governance_lease(ledger)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_governance_unavailable") from exc
        finally:
            ledger.close()
        self._stores["governance"] = value
        return value

    # ---- built-in semantic handlers -----------------------------------------
    def _memory_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        scope = self._scope(context)
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        result = store.get_atom(memory_id, scope=scope, atom_id=_text(payload.get("atom_id")))
        return result

    def _memory_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        return store.list_atoms(scope=self._scope(context), status=payload.get("status"))

    def _memory_search(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        store = self._domain_store("memory")
        query = _text(payload.get("query") or payload.get("text") or payload.get("q")).casefold()
        search = getattr(store, "search", None)
        if callable(search):
            return search(query=query, scope=self._scope(context), limit=payload.get("limit"))
        values = store.list_atoms(scope=self._scope(context), status=payload.get("status"))
        if not query:
            return values
        return [item for item in values if query in _text(getattr(item, "body", "")).casefold()]

    def _memory_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        scope = self._scope(context)
        if not self.layout.memory_db.is_file():
            return {
                "available": False,
                "status": "NO_SOURCE",
                "scope": {
                    "share_group_id": scope.get("share_group_id", ""),
                    "agent_instance_id": scope.get("agent_instance_id", ""),
                    "project_ref": scope.get("project_ref", ""),
                },
                "total_records": 0,
                "status_counts": {},
                "kind_counts": {},
                "evidence_link_count": 0,
            }
        try:
            store = self._domain_store("memory")
            atoms = list(store.list_atoms(scope=scope, include_building=False))
            by_status: dict[str, int] = {}
            by_kind: dict[str, int] = {}
            evidence_links = 0
            for atom in atoms:
                status = str(getattr(atom, "status", "") or "unknown")
                kind = str(getattr(atom, "kind", "") or "unknown")
                by_status[status] = by_status.get(status, 0) + 1
                by_kind[kind] = by_kind.get(kind, 0) + 1
                evidence_links += len(store.evidence_ids_for_atom(atom.atom_id))
            return {
                "available": True,
                "scope": {
                    "share_group_id": scope.get("share_group_id", ""),
                    "agent_instance_id": scope.get("agent_instance_id", ""),
                    "project_ref": scope.get("project_ref", ""),
                },
                "total_records": len(atoms),
                "status_counts": by_status,
                "kind_counts": by_kind,
                "evidence_link_count": evidence_links,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_memory_status_unavailable") from exc

    def _memory_versions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read redacted V2 revision metadata for one exact transport scope."""
        store = self._domain_store("memory")
        reader = getattr(store, "list_revisions", None)
        if not callable(reader):
            raise NativePortError("v2_store_schema_invalid")
        return {
            "versions": reader(
                scope=self._scope(context),
                memory_id=_text(payload.get("memory_id") or payload.get("id")),
                atom_id=_text(payload.get("atom_id")),
                limit=payload.get("limit", 100),
            ),
        }

    def _memory_supersede_chain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read direct V2 supersession edges without exposing atom bodies."""
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        store = self._domain_store("memory")
        reader = getattr(store, "supersede_chain", None)
        if not callable(reader):
            raise NativePortError("v2_store_schema_invalid")
        result = reader(memory_id, scope=self._scope(context))
        if result is None:
            raise NativePortError("memory_not_found")
        return result

    def _memory_write(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        from ..memory.store import MemoryAtom

        clean = dict(payload)
        memory_id = _text(clean.get("memory_id") or clean.get("id"))
        if not memory_id or "body" not in clean:
            raise NativePortError("memory_payload_required")
        idempotency_key = _text(clean.pop("idempotency_key", ""))
        if not idempotency_key:
            raise NativePortError("idempotency_key_required")
        evidence = clean.pop("evidence", None)
        evidence_ids = clean.pop("evidence_ids", None)
        source_mappings = clean.pop("source_mappings", None)
        reason = _text(clean.pop("reason", "")) or "native memory write"
        # Atom confidence and governance-decision confidence are different
        # facts.  Keep ``confidence`` on the MemoryAtom; transport adapters
        # may supply a separate internal decision_confidence.
        decision_confidence = clean.pop("decision_confidence", 1.0)
        scope = self._scope(context)
        clean.update({key: value for key, value in scope.items() if value})
        atom = MemoryAtom.from_value(clean)
        governance = self._governance_boundary()
        try:
            persisted, receipt = governance.put_atom(
                atom,
                context=self._mutation_context(context),
                evidence=evidence,
                evidence_ids=evidence_ids,
                source_mappings=source_mappings,
                reason=reason,
                confidence=decision_confidence,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise self._map_governance_error(exc) from exc
        return {"atom": persisted, "receipt": receipt}

    def _memory_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        clean = dict(payload)
        memory_id = _text(clean.get("memory_id") or clean.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        existing = self._memory_read({"memory_id": memory_id, "atom_id": clean.get("atom_id", "")}, context)
        if existing is None:
            raise NativePortError("memory_not_found")
        if hasattr(existing, "to_dict"):
            merged = dict(existing.to_dict())
        elif isinstance(existing, Mapping):
            merged = dict(existing)
        else:
            raise NativePortError("v2_memory_read_invalid")
        # Storage-assigned fields are allowed as compatibility input but the
        # writer will recompute/persist the new revision.  Explicit request
        # fields override the complete prior atom so partial GUI/MCP updates
        # never reset kind/confidence/metadata/scope to constructor defaults.
        merged.update(clean)
        merged["memory_id"] = memory_id
        return self._memory_write(merged, context)

    def _memory_delete(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        idempotency_key = _text(payload.get("idempotency_key"))
        if not idempotency_key:
            raise NativePortError("idempotency_key_required")
        reason = _text(payload.get("reason")) or "native memory delete"
        governance = self._governance_boundary()
        try:
            persisted, receipt = governance.tombstone(
                memory_id,
                context=self._mutation_context(context),
                reason=reason,
                confidence=payload.get("confidence", 1.0),
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise self._map_governance_error(exc) from exc
        return {"atom": persisted, "receipt": receipt}

    def _gui_memory_key(self, action: str, payload: Mapping[str, Any], context: Mapping[str, Any]) -> str:
        scope = self._scope(context)
        memory_id = _text(payload.get("memory_id") or payload.get("id"))
        if not memory_id:
            raise NativePortError("memory_id_required")
        current = None
        try:
            if self.layout.memory_db.is_file():
                current = self._domain_store("memory").get_atom(memory_id, scope=scope, include_building=True)
        except Exception:
            current = None
        revision = int(getattr(current, "revision", 0) or 0) if current is not None else 0
        fingerprint = {
            key: value for key, value in payload.items()
            if key not in {"idempotency_key", "actor", "admin", "is_admin"}
        }
        return "gui:" + hashlib.sha256(
            _canonical_json({"action": action, "memory_id": memory_id, "revision": revision, "payload": fingerprint}).encode("utf-8")
        ).hexdigest()

    def _gui_memory_update(self, action: str, payload: Mapping[str, Any], context: Mapping[str, Any]) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        clean: dict[str, Any] = {"memory_id": _text(payload.get("memory_id") or payload.get("id"))}
        if not clean["memory_id"]:
            raise NativePortError("memory_id_required")
        if action == "edit":
            if "body" not in payload or not isinstance(payload.get("body"), str):
                raise NativePortError("memory_body_required")
            body = str(payload.get("body") or "")
            try:
                from ..auto_organizer import SECRET_PATTERNS
                for pattern in SECRET_PATTERNS:
                    body = pattern.sub("[REDACTED]", body)
            except Exception:
                pass
            clean["body"] = body
        elif action == "lock":
            clean["locked"] = True
        elif action == "unlock":
            clean["locked"] = False
        elif action == "policy":
            policy = _text(payload.get("injection_policy"))
            if policy not in {"relevant", "always"}:
                raise NativePortError("invalid_injection_policy")
            clean["injection_policy"] = policy
            raw_priority = payload.get("priority", 0)
            if isinstance(raw_priority, bool):
                raise NativePortError("invalid_priority")
            try:
                priority = int(raw_priority)
            except (TypeError, ValueError) as exc:
                raise NativePortError("invalid_priority") from exc
            if not -100 <= priority <= 100:
                raise NativePortError("invalid_priority")
            clean["priority"] = priority
        elif action == "restore":
            clean["status"] = "active"
        else:
            raise NativePortError("unknown_memory_mutation")
        clean["reason"] = f"native GUI memory {action}"
        clean["decision_confidence"] = 1.0
        clean["idempotency_key"] = self._gui_memory_key(action, clean, context)
        return self._memory_update(clean, context)

    def _gui_memory_edit(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("edit", payload, context)

    def _gui_memory_lock(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("lock", payload, context)

    def _gui_memory_unlock(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("unlock", payload, context)

    def _gui_memory_policy(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("policy", payload, context)

    def _gui_memory_restore(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return self._gui_memory_update("restore", payload, context)

    def _gui_memory_delete(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        clean = {"memory_id": _text(payload.get("memory_id") or payload.get("id"))}
        if not clean["memory_id"]:
            raise NativePortError("memory_id_required")
        clean["reason"] = "native GUI memory delete"
        clean["confidence"] = 1.0
        clean["idempotency_key"] = self._gui_memory_key("delete", clean, context)
        return self._memory_delete(clean, context)

    def _gui_memory_rollback(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        version_id = _text(payload.get("version_id") or payload.get("revision_id"))
        if not version_id:
            raise NativePortError("version_id_required")
        scope = self._scope(context)
        store = self._domain_store("memory", write=True)
        try:
            with open_database(self.layout.memory_db, readonly=True) as conn:
                clauses = ["r.revision_id=?", "a.workspace_id=?", "a.share_group_id=?"]
                params: list[Any] = [version_id, self.workspace, scope.get("share_group_id", "")]
                for column, value in (
                    ("a.agent_instance_id", scope.get("agent_instance_id", "")),
                    ("a.project_ref", scope.get("project_ref", "")),
                    ("a.provider", scope.get("provider", "")),
                    ("a.runtime_role", scope.get("runtime_role", "")),
                ):
                    if value:
                        clauses.append(column + "=?")
                        params.append(value)
                row = conn.execute(
                    "SELECT r.atom_id,r.revision,a.memory_id,a.revision AS current_revision "
                    "FROM atom_revisions r JOIN atoms a ON a.atom_id=r.atom_id WHERE " + " AND ".join(clauses),
                    params,
                ).fetchone()
            if row is None:
                raise NativePortError("revision_not_found")
            restored = store.replay_revision(str(row[0]), int(row[1]))
            if restored is None:
                raise NativePortError("revision_not_found")
            governance = self._governance_boundary()
            persisted, receipt = governance.put_atom(
                restored,
                context=self._mutation_context(context),
                reason="native GUI memory revision rollback",
                confidence=1.0,
                idempotency_key="gui:rollback:" + hashlib.sha256(
                    f"{version_id}\0{int(row[3])}".encode("utf-8")
                ).hexdigest(),
            )
            return {"atom": persisted, "receipt": receipt, "version_id": version_id}
        except NativePortError:
            raise
        except Exception as exc:
            raise self._map_governance_error(exc) from exc

    def _context_bootstrap(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        engine = self.context_engine
        if engine is None:
            raise NativePortError("v2_context_engine_unavailable")
        # ``V2RuntimeFacade.bootstrap_hook`` uses the generic dispatch shape
        # ``{"request": event, "payload": request}``; direct callers pass a
        # request mapping as the first argument.  Normalize both without
        # treating an event label as a context request.
        nested_request = payload.get("request")
        nested_payload = payload.get("payload")
        if isinstance(nested_request, Mapping):
            request = dict(nested_request)
        elif isinstance(nested_payload, Mapping):
            request = dict(nested_payload)
        else:
            request = dict(payload)
        for key, value in payload.items():
            if key not in {"request", "payload"}:
                request.setdefault(key, value)
        request["trusted_identity"] = dict(context.get("trusted_identity") or {})
        request.update({
            "agent_instance_id": context.get("agent_instance_id", ""),
            "project_ref": context.get("project_ref", ""),
            "share_group_id": context.get("share_group_id", ""),
            "provider": context.get("provider", ""),
            "runtime_role": context.get("runtime_role", ""),
        })
        candidates = request.pop("candidates", None)
        fn = getattr(engine, "bootstrap", None) or getattr(engine, "build_context", None)
        if not callable(fn):
            raise NativePortError("v2_context_engine_unavailable")
        return fn(request, candidates)

    def _rule_decision_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Decisions are persisted without a share-group scope in the current
        # V2 schema.  Do not turn this neutral-read surface into a global
        # history/count oracle; callers receive only fixed availability until
        # a scoped decision service is provided.
        del payload, context
        return {"available": True}

    def _rule_scope_stats(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Rule definitions are global canonical rows and the store's fallback
        # stats API has no complete audience filter.  Fixed availability is
        # the fail-closed neutral response until a scoped stats service exists.
        del payload, context
        return {"available": True}

    def _binding_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._stores.get("rules") or self._stores.get("governance")
        if service is None:
            service = self._domain_store("rules")
        fn = getattr(service, "list_bindings", None)
        if not callable(fn):
            raise NativePortError("v2_binding_service_unavailable")
        return fn(
            definition_id=_text(payload.get("definition_id")) or None,
            share_group_id=_text(payload.get("share_group_id")) or context.get("share_group_id"),
            status=_text(payload.get("status")) or None,
        )

    def _binding_create(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        # Rule stores injected through the explicit test capability are
        # read-only seams.  Binding creation must always resolve the builtin
        # writer after its schema preflight.
        service = self._domain_store("rules", write=True)
        fn = getattr(service, "upsert_binding", None)
        if not callable(fn):
            raise NativePortError("v2_binding_service_unavailable")
        clean = dict(payload)
        # Drop all caller-controlled authorization/ownership aliases before
        # deriving the canonical audit fields below.  Unknown fields are not
        # harmless: custom Rules services may persist them or treat them as
        # policy inputs even when the built-in store ignores them.
        for key in (
            "created_by", "owner", "owner_id", "owner_agent", "owner_agent_id",
            "authorization", "authority", "admin", "is_admin",
        ):
            clean.pop(key, None)
        clean.update(self._scope(context))
        # Binding governance fields are derived from the trusted admin
        # capability.  A request body must not be able to self-identify as a
        # manual/admin actor or assign ownership to another agent.
        clean["created_by"] = "admin"
        clean["owner_agent_id"] = context.get("agent_instance_id", "")
        clean["authorization"] = f"native-admin:{context.get('session_id', '')}"
        return fn(clean)

    def _knowledge_scope(
        self,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> Any:
        """Bind a request to the exact scope issued by native capability.

        The three Knowledge ACL selectors are deliberately not inferred from
        payload defaults.  A process-issued ``NativeBoundContext`` must carry
        each selector and the request must repeat the exact same values.  This
        keeps a valid capability from being replayed against another namespace
        or broader sensitivity/policy class while preserving optional scope
        fields for non-Knowledge handlers.
        """

        # Knowledge is a scoped content read, not a neutral identity echo.
        # Resolve the process-issued authority here (before inspecting any
        # selectors or touching the service) so a plain identity mapping,
        # ``to_dict`` projection, or other lookalike can never become a read
        # capability.  Keep the stable native error used by the mutation and
        # Phase 9 scoped-read gates.
        try:
            authority = resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc

        capability_scope = tuple(
            _text(getattr(authority, key))
            for key in ("namespace_id", "sensitivity", "policy_class")
        )
        if not all(capability_scope):
            raise NativePortError("knowledge_scope_required")
        request_scope = tuple(
            _text(payload.get(key))
            for key in ("namespace_id", "sensitivity", "policy_class")
        )
        if not all(request_scope):
            raise NativePortError("knowledge_scope_required")
        if request_scope != capability_scope:
            raise NativePortError("knowledge_scope_mismatch")
        namespace, sensitivity, policy_class = capability_scope
        from ..content.store import ContentReadScope

        try:
            return ContentReadScope(
                namespace_id=namespace,
                workspace_id=self.workspace,
                agent_instance_id=_text(context.get("agent_instance_id")),
                project_ref=_text(context.get("project_ref")),
                provider=_text(context.get("provider")),
                share_group_id=_text(context.get("share_group_id")),
                sensitivity=sensitivity,
                policy_class=policy_class,
            )
        except (TypeError, ValueError) as exc:
            raise NativePortError("knowledge_scope_required") from exc

    @staticmethod
    def _knowledge_service_result(
        service: Any,
        operation: str,
        payload: Mapping[str, Any],
        *,
        scope: Any,
    ) -> Any:
        dispatch = getattr(service, "dispatch", None) or getattr(service, "call", None)
        if not callable(dispatch):
            raise NativePortError("v2_knowledge_service_unavailable")
        try:
            result = dispatch(operation, payload, scope=scope)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_knowledge_service_failed") from exc
        if not isinstance(result, Mapping):
            raise NativePortError("v2_knowledge_service_failed")
        if result.get("ok") is False or _text(result.get("status")).casefold() in {"error", "blocked", "failed"}:
            code = _text(result.get("code") or result.get("error"))
            raise NativePortError(code or "v2_knowledge_service_failed")
        return result

    def _knowledge_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **_: Any,
    ) -> Any:
        # Validate the native scope before resolving/constructing any service;
        # invalid selectors must not even reach a service injection seam.
        scope = self._knowledge_scope(payload, context)
        service = self._native_service("knowledge")
        if service is None:
            raise NativePortError("v2_knowledge_service_unavailable")
        # Pass the canonical selectors to the service, not the caller's
        # possibly conflicting values.  Authorization has already happened at
        # this native boundary and the service receives a stable exact scope.
        bound_payload = dict(payload)
        bound_payload.update({
            "namespace_id": scope.namespace_id,
            "sensitivity": scope.sensitivity,
            "policy_class": scope.policy_class,
        })
        return self._knowledge_service_result(service, operation, bound_payload, scope=scope)

    def _knowledge_book(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._knowledge_operation("memoryguard_knowledge_book", payload, context, **kwargs)

    def _knowledge_candidates(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._knowledge_operation("memoryguard_knowledge_candidates", payload, context, **kwargs)

    def _knowledge_read(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        scope = self._knowledge_scope(payload, context)
        adapter = self._stores.get("knowledge")
        if adapter is None:
            content = self._stores.get("content")
            if content is None:
                content = self._domain_store("content")
            from ..knowledge_v2.adapter import KnowledgeV2Adapter
            adapter = KnowledgeV2Adapter(content, namespace_id=scope.namespace_id)
            self._stores["knowledge"] = adapter
        occurrence_id = _text(payload.get("occurrence_id")) or None
        return adapter.read(scope, query=_text(payload.get("query")), limit=payload.get("limit", 100), occurrence_id=occurrence_id)

    @staticmethod
    def _audit_finding_id(code: str, domain: str, table: str) -> str:
        return "v2-" + hashlib.sha256(f"{code}\0{domain}\0{table}".encode("utf-8")).hexdigest()[:16]

    def _reference_audit(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Run the native V2 reference audit in strict read-only mode."""

        del payload, context
        try:
            from ..maintenance_v2.reference_audit import ReferenceAudit

            result = ReferenceAudit(self.workspace, mode="ro").audit()
            public = result.to_public_dict()
            public["blockers"] = [
                {
                    **dict(item),
                    "finding_id": self._audit_finding_id(
                        str(item.get("code") or ""),
                        str(item.get("domain") or ""),
                        str(item.get("table") or ""),
                    ),
                }
                for item in public.get("blockers", [])
                if isinstance(item, Mapping)
            ]
            return {"ok": True, "status": "ok", "data": public}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_reference_audit_unavailable") from exc

    def _explain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del context
        finding_id = _text(payload.get("finding_id"))
        if not finding_id:
            raise NativePortError("finding_id_required")
        try:
            from ..maintenance_v2.reference_audit import ReferenceAudit

            result = ReferenceAudit(self.workspace, mode="ro").audit()
            blocker = next(
                (
                    item for item in result.blockers
                    if finding_id in {
                        item.code,
                        self._audit_finding_id(item.code, item.domain, item.table),
                    }
                ),
                None,
            )
            if blocker is None:
                raise NativePortError("finding_not_found")
            code = str(blocker.code or "")
            if "schema" in code:
                suggestion = "repair or migrate the blocked V2 schema, then rerun reference audit and readiness"
            elif "reference" in code or "orphan" in code:
                suggestion = "repair the broken V2 reference or restore its authoritative target, then rerun the audit"
            elif "unknown" in code:
                suggestion = "classify the unknown authoritative data and add an explicit migration/disposition before promotion"
            else:
                suggestion = "resolve this V2 reference-audit blocker and rerun validator/readiness"
            return {
                "finding_id": self._audit_finding_id(blocker.code, blocker.domain, blocker.table),
                "code": code,
                "domain": str(blocker.domain or ""),
                "table": str(blocker.table or ""),
                "evidence": {"source": "v2_reference_audit", "read_only": True},
                "impact": "blocks proof of V2 reference integrity and therefore activation readiness",
                "suggestion": suggestion,
                "confidence": 1.0,
                "fixable": True,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_explain_unavailable") from exc

    def _codegraph_graph(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read bounded codegraph metadata for one exact trusted scope.

        CodeGraphStore exposes paths, symbols, hashes, and edges only; source
        bodies never enter this response.  Missing/partial/future schemas are
        hard errors from the store preflight and never trigger initialization.
        """

        scope_data = self._scope(context)
        try:
            from ..codegraph_v2 import CodeGraphScope

            graph_scope = CodeGraphScope.from_value({**scope_data, "trusted_context": True})
            store = self._domain_store("codegraph")
            preflight = getattr(store, "_preflight", None)
            if callable(preflight):
                preflight()
            raw_limit = payload.get("limit", 100)
            if isinstance(raw_limit, bool):
                raise ValueError("invalid limit")
            limit = max(1, min(int(raw_limit), 500))
            files = tuple(store.list_source_files(scope=graph_scope, active_only=True))
            if not files:
                return {
                    "status": "NO_SOURCE",
                    "scope_digest": graph_scope.digest,
                    "nodes": [],
                    "edges": [],
                    "node_count": 0,
                    "edge_count": 0,
                }
            nodes: list[dict[str, Any]] = []
            file_ids: set[str] = set()
            for source in files[:limit]:
                file_ids.add(source.file_id)
                nodes.append({
                    "id": source.file_id,
                    "node_kind": "file",
                    "label": source.path,
                    "path": source.path,
                    "language": source.language,
                    "content_hash": source.content_hash,
                    "source_revision": source.source_revision,
                })
                try:
                    symbols = store.get_symbols(source.file_id, scope=graph_scope)
                except Exception:
                    symbols = ()
                for symbol in tuple(symbols)[: max(1, min(50, limit))]:
                    nodes.append({
                        "id": symbol.symbol_id,
                        "node_kind": "symbol",
                        "label": symbol.name,
                        "kind": symbol.kind,
                        "signature": symbol.signature,
                        "file_id": symbol.file_id,
                        "line_start": symbol.line_start,
                        "line_end": symbol.line_end,
                    })
            raw_edges = store.list_edges(scope=graph_scope)
            edges = [
                {
                    "id": edge.edge_id,
                    "from_id": edge.from_id,
                    "to_id": edge.to_id,
                    "relation": edge.relation,
                    "weight": edge.weight,
                }
                for edge in tuple(raw_edges)[:limit]
                if edge.from_id in {node["id"] for node in nodes}
                and edge.to_id in {node["id"] for node in nodes}
            ]
            return {
                "status": "READY",
                "scope_digest": graph_scope.digest,
                "nodes": nodes,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_codegraph_read_unavailable") from exc

    def _provider_install(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Repair one provider integration without constructing V1 memory.

        Provider configuration is host control-plane state, not a V1/V2 data
        store.  The active AgentBinding is reused as the connection boundary;
        this route never creates a personal shared-memory group.
        """
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        provider = _text(payload.get("target_provider")).casefold()
        try:
            from ..provider_adapters import ClaudeAdapter, CodexAdapter, CursorAdapter, TraeAdapter

            adapter_cls = {
                "claude": ClaudeAdapter,
                "codex": CodexAdapter,
                "cursor": CursorAdapter,
                "trae": TraeAdapter,
            }.get(provider)
            if adapter_cls is None:
                raise NativePortError("unknown_provider")
            result = adapter_cls(self.workspace).install(
                self.workspace,
                share_group_id=_text(context.get("share_group_id")),
                agent_instance_id=_text(context.get("agent_instance_id")),
                global_scope=True,
            )
            if not isinstance(result, Mapping):
                raise NativePortError("provider_install_failed")
            # Do not expose absolute host config paths over the transport.
            return {
                "provider": provider,
                "status": str(result.get("status") or "configured"),
                "restart_required": bool(result.get("restart_required", True)),
                "runtime_verified": bool(result.get("runtime_verified", False)),
                "binding_id": str(result.get("binding_id") or ""),
                "hook_configured": bool(result.get("hook_configured", False)),
                "hook_runtime_verified": bool(result.get("hook_runtime_verified", False)),
                "warnings": list(result.get("warnings") or []),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("provider_install_failed") from exc

    def _semantic_check(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Compare input text against scoped V2 memory atoms without bodies out."""

        text = _text(payload.get("text"))
        if not text or len(text) > 8192:
            raise NativePortError("semantic_text_required")
        threshold_value = payload.get("threshold", 0.85)
        try:
            threshold = float(threshold_value)
        except (TypeError, ValueError) as exc:
            raise NativePortError("invalid_semantic_threshold") from exc
        if not 0.0 <= threshold <= 1.0:
            raise NativePortError("invalid_semantic_threshold")
        scope = self._scope(context)
        try:
            from ..semantic_dedup import HashBackend, cosine_similarity

            store = self._domain_store("memory")
            backend = HashBackend()
            new_vector = backend.embed_text(text)
            atoms = store.list_atoms(scope=scope, status="active")
            duplicates: list[dict[str, Any]] = []
            for atom in atoms:
                similarity = cosine_similarity(new_vector, backend.embed_text(str(atom.body or "")))
                if similarity < threshold:
                    continue
                kind = getattr(atom.kind, "value", str(atom.kind))
                duplicates.append({
                    "memory_id": atom.memory_id,
                    "similarity": similarity,
                    "kind": kind,
                })
            duplicates.sort(key=lambda item: (-float(item["similarity"]), str(item["memory_id"])))
            kind_filter = _text(payload.get("kind"))
            conflicts = [item for item in duplicates if kind_filter and item["kind"] != kind_filter]
            return {
                "duplicates": duplicates,
                "conflicts": conflicts,
                "checked_against": len(atoms),
                "threshold": threshold,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_semantic_read_unavailable") from exc

    @staticmethod
    def _scoped_read_context(context: Mapping[str, Any]) -> tuple[str, str]:
        agent = _text(context.get("agent_instance_id"))
        group = _text(context.get("share_group_id"))
        if not agent and not group:
            raise NativePortError("native_scope_required")
        return agent, group

    def _extraction_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("extraction")
        if service is None:
            raise NativePortError("v2_extraction_service_unavailable")
        return self._service_result(service, operation, payload, context=context, **kwargs)

    def _extract_memories(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_extract_memories", payload, context, **kwargs)

    def _gui_extract_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        root_id = _text(payload.get("root_id"))
        relative = _text(payload.get("relative_path"))
        if not root_id:
            raise NativePortError("source_root_required")
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        try:
            status, roots, code = service._load_roots()
            if status != "READY":
                raise NativePortError(code or "no_source")
            root = next((item for item in roots if _text(getattr(item, "root_id", "")) == root_id), None)
            if root is None or not service._authorized_for_context(root, context):
                raise NativePortError("source_root_not_authorized")
            state, root_path, root_code = service._validate_root(root)
            if state != "READY" or root_path is None:
                raise NativePortError(root_code or "source_root_unavailable")
            target = root_path if root_path.is_file() else (root_path / relative).resolve()
            try:
                if root_path.is_dir():
                    target.relative_to(root_path)
            except ValueError as exc:
                raise NativePortError("path_out_of_scope") from exc
            return self._extract_memories({"source_path": str(target)}, context, **kwargs)
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_extract_preview_unavailable") from exc

    def _gui_extract_by_path(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        source_path = _text(payload.get("source_path") or payload.get("path"))
        if not source_path:
            raise NativePortError("source_path_required")
        return self._extract_memories({"source_path": source_path}, context, **kwargs)

    def _accept_candidates(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_accept_candidates", payload, context, **kwargs)

    def _list_pending_enrichments(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not self.layout.content_db.is_file():
            return {
                "pending_count": 0,
                "tasks": [],
                "next_step": "classify/translate then call memoryguard_apply_enrichments",
                "storage": "v2_content_plane",
                "status": "NO_SOURCE",
            }
        return self._extraction_operation("memoryguard_list_pending_enrichments", payload, context, **kwargs)

    def _apply_enrichments(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_apply_enrichments", payload, context, **kwargs)

    def _enrichment_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not self.layout.content_db.is_file():
            return {"pending": 0, "applied": 0, "other": 0, "total": 0, "mode": "v2_content_plane", "status": "NO_SOURCE"}
        return self._extraction_operation("memoryguard_enrichment_status", payload, context, **kwargs)

    def _build_and_enrich(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._extraction_operation("memoryguard_build_and_enrich", payload, context, **kwargs)

    def _resolve_group(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        agent, _group = self._scoped_read_context(context)
        if not agent:
            raise NativePortError("native_agent_scope_required")
        try:
            from ..agent_binding import AgentBindingStore, BindingStatus

            bindings = AgentBindingStore(self.workspace).find_by_agent(agent, include_inactive=False)
            if not bindings:
                return {"share_group_id": None, "binding_id": None, "native_memory_mode": None}
            binding = bindings[0]
            return {
                "share_group_id": binding.share_group_id,
                "binding_id": binding.binding_id,
                "native_memory_mode": binding.native_memory_mode.value,
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_binding_read_unavailable") from exc

    def _status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Status surfaces are neutral reads.  Coverage is exposed separately
        # through the machine-readable ``coverage()`` provider; returning it
        # here would turn a tenant-neutral operation into a global count and
        # activation-state oracle.
        del payload, context
        return {"available": True}

    @staticmethod
    def _sandbox_status(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Read sandbox flag without constructing any workspace service."""

        del payload, context
        try:
            from ..security import detect_sandbox_mode

            return {"sandbox": bool(detect_sandbox_mode())}
        except Exception as exc:
            raise NativePortError("v2_sandbox_status_unavailable") from exc

    def _host_enrichment_guide(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Return bounded host guidance using existing read-only enrichment status."""

        status = self._enrichment_status(payload, context)
        pending = status.get("pending", 0)
        applied = status.get("applied", 0)
        if type(pending) is not int or type(applied) is not int:
            raise NativePortError("v2_enrichment_read_unavailable")
        return {
            "ok": True,
            "mode": "host_skill_primary",
            "pending_count": pending,
            "applied_count": applied,
            "message": (
                f"host skill has {pending} pending enrichment items"
                if pending else "no pending enrichment items"
            ),
            "mcp_tools": [
                "memoryguard_build_and_enrich",
                "memoryguard_list_pending_enrichments",
                "memoryguard_apply_enrichments",
                "memoryguard_enrichment_status",
            ],
        }

    @staticmethod
    def _host_llm_agents(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Expose host-only control-plane option; never scan or spawn CLIs."""

        del payload, context
        return {
            "agents": [{"agent": "host", "cli": "", "label": "host skill"}],
            "primary": "host",
        }

    def _canonical_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        group = _text(context.get("share_group_id"))
        if not group:
            raise NativePortError("native_scope_required")
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "share_group_id": group, "canonical_state": "absent"}
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                row = conn.execute(
                    "SELECT activation_status,read_path,canonical_digest,source_digest,effective_digest,runtime_digest,assessment_digest,policy_version,updated_at "
                    "FROM rule_canonical_state WHERE share_group_id=? ORDER BY updated_at DESC,scope_id LIMIT 1",
                    (group,),
                ).fetchone()
            if row is None:
                return {"status": "NO_SOURCE", "share_group_id": group, "canonical_state": "absent"}
            return {
                "status": "READY",
                "share_group_id": group,
                "canonical_state": str(row[0] or ""),
                "read_path": str(row[1] or "legacy"),
                "canonical_digest": str(row[2] or ""),
                "source_digest": str(row[3] or ""),
                "effective_digest": str(row[4] or ""),
                "runtime_digest": str(row[5] or ""),
                "assessment_digest": str(row[6] or ""),
                "policy_version": str(row[7] or ""),
                "updated_at": str(row[8] or ""),
            }
        except Exception as exc:
            raise NativePortError("v2_canonical_status_unavailable") from exc

    def _projection_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        scope = self._scope(context)
        result: dict[str, Any] = {"status": "READY", "scenario_heads": 0, "profile_heads": 0}
        configured = False
        try:
            for kind, path in (("scenario", self.layout.scenario_db), ("profile", self.layout.profile_db)):
                if not path.is_file():
                    continue
                configured = True
                clauses = ["a.workspace_id=?", "a.share_group_id=?"]
                params: list[Any] = [self.workspace, scope.get("share_group_id", "")]
                for column, value in (
                    ("a.agent_instance_id", scope.get("agent_instance_id", "")),
                    ("a.project_ref", scope.get("project_ref", "")),
                    ("a.provider", scope.get("provider", "")),
                ):
                    if value:
                        clauses.append(column + "=?")
                        params.append(value)
                with open_database(path, readonly=True) as conn:
                    count = int(conn.execute(
                        "SELECT COUNT(DISTINCT h.head_id) FROM projection_heads h "
                        "JOIN projection_acl a ON a.projection_id=h.current_projection_id "
                        "WHERE h.current_projection_id<>'' AND " + " AND ".join(clauses),
                        params,
                    ).fetchone()[0])
                result[f"{kind}_heads"] = count
            if not configured:
                result["status"] = "NO_SOURCE"
            result["total_heads"] = int(result["scenario_heads"]) + int(result["profile_heads"])
            return result
        except Exception as exc:
            raise NativePortError("v2_projection_status_unavailable") from exc

    def _diagnostics_snapshot(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        return {
            "status": "READY",
            "memory": self._memory_status(payload, context),
            "canonical": self._canonical_status(payload, context),
            "projection": self._projection_status(payload, context),
            "enrichment": self._enrichment_status(payload, context),
        }

    def _scope_echo(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        """Return the trusted GUI scope without exposing an absolute workspace."""
        del payload
        return {
            "available": True,
            "agent_instance_id": _text(context.get("agent_instance_id")),
            "share_group_id": _text(context.get("share_group_id")),
            "project_ref": _text(context.get("project_ref")),
            "provider": _text(context.get("provider")),
            "runtime_role": _text(context.get("runtime_role")),
            "admin": bool(context.get("admin", context.get("is_admin", False))),
        }

    def _hook_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return {"available": True}

    def _domain_status(self, domain: str, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if domain == "projection":
            return self._projection_status(payload, context)
        # Non-projection compatibility statuses remain bounded availability
        # envelopes until their public surfaces are promoted separately.
        del domain, payload, context
        return {"available": True}

    # ---- Phase 9 native service adapters ------------------------------------
    def _native_service(self, kind: str) -> Any:
        """Lazily construct one production native service.

        Construction errors are recorded as bounded codes and converted to a
        stable native blocker by the operation wrapper.  In particular, a
        missing history DB/config must not be repaired merely by constructing
        the port.
        """

        attr = f"_{kind}_service"
        current = getattr(self, attr)
        if current is not None:
            return current
        if kind in self._native_service_init_errors:
            return None
        try:
            if kind == "history":
                from .history_native import NativeHistoryService

                current = NativeHistoryService(self.workspace)
            elif kind == "source_read":
                from .safe_services import PureSourceReadService

                current = PureSourceReadService(self.workspace)
            elif kind == "import_preview":
                from .safe_services import ImportPreviewService

                source = self._native_service("source_read")
                if source is None:
                    self._native_service_init_errors[kind] = "v2_source_read_service_unavailable"
                    return None
                current = ImportPreviewService(self.workspace, source_reader=source)
            elif kind == "runtime_diagnostics":
                from .safe_services import RuntimeDiagnosticsService

                current = RuntimeDiagnosticsService(self.workspace)
            elif kind == "external_mcp":
                from .external_mcp_native import NativeExternalMCPService

                current = NativeExternalMCPService(self.workspace)
            elif kind == "rule_merge":
                from .rule_merge_native import NativeRuleMergeService

                injected = self._stores.get("rule_merge")
                capability = (
                    bind_native_test_capability(rule_merge_store=injected)
                    if injected is not None else None
                )
                current = NativeRuleMergeService(
                    self.workspace,
                    rule_store=capability,
                    state_provider=self.state_provider,
                )
            elif kind == "rule_lifecycle":
                from .rule_lifecycle_native import NativeRuleLifecycleService

                current = NativeRuleLifecycleService(self.workspace)
            elif kind == "extraction":
                from .extraction_native import NativeExtractionEnrichmentService

                current = NativeExtractionEnrichmentService(
                    self.workspace,
                    content_store=self._domain_store("content", write=True),
                    memory_store=self._domain_store("memory", write=True),
                    governance=self._governance_boundary(),
                )
            elif kind == "knowledge":
                from ..knowledge_v2.service import KnowledgeV2ReadonlyService

                current = KnowledgeV2ReadonlyService(self.workspace)
            else:  # pragma: no cover - registry controls this value
                self._native_service_init_errors[kind] = "v2_native_service_unavailable"
                return None
        except Exception:
            # Do not expose constructor exception details (which can include
            # absolute paths).  Service operation wrappers emit the stable
            # kind-specific code below.
            self._native_service_init_errors[kind] = f"v2_{kind}_service_unavailable"
            return None
        setattr(self, attr, current)
        return current

    @staticmethod
    def _service_result(service: Any, operation: str, payload: Mapping[str, Any], *, context: Mapping[str, Any], **kwargs: Any) -> Any:
        """Invoke a native service and unwrap its transport envelope.

        ``NativeV2RuntimePort`` owns the outer V2 envelope.  Builtin services
        retain their stable ``status/service/code`` fields inside the returned
        data while service failures become the port's bounded ``NativePortError``.
        """

        dispatch = getattr(service, "dispatch", None) or getattr(service, "call", None)
        if not callable(dispatch):
            raise NativePortError("v2_native_service_unavailable")
        try:
            result = dispatch(operation, payload, context=context, **kwargs)
        except NativePortError:
            raise
        except Exception as exc:
            # Service implementations already expose stable error envelopes;
            # an unexpected exception is intentionally collapsed at this seam.
            raise NativePortError("v2_native_service_failed") from exc
        if not isinstance(result, Mapping):
            raise NativePortError("v2_native_service_failed")
        if result.get("ok") is False or _text(result.get("status")).casefold() in {"error", "blocked", "failed"}:
            code = _text(result.get("code") or result.get("error"))
            raise NativePortError(code or "v2_native_service_failed")
        # NativeHistoryService returns {ok,status,operation,data}; preserve
        # only the business data so the native port adds one canonical envelope.
        if "data" in result and isinstance(result.get("data"), Mapping):
            return result["data"]
        return result

    def _history_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        mutation: bool = False,
        **_: Any,
    ) -> Any:
        service = self._native_service("history")
        if service is None:
            raise NativePortError("v2_history_service_unavailable")
        return self._service_result(
            service,
            operation,
            payload,
            context=context,
            generation=generation,
            state=state,
            mutation=mutation,
        )

    @staticmethod
    def _history_neutral(handler: str) -> dict[str, Any]:
        operation = handler.removeprefix("history_")
        if operation == "search":
            return {"query": "", "results": [], "limit": 0, "offset": 0, "neutral": True}
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
        return {"neutral": True}

    @staticmethod
    def _source_neutral(handler: str) -> dict[str, Any]:
        if handler == "list_sources":
            return {"ok": True, "status": "NO_SOURCE", "service": "source_read", "sources": [], "total": 0}
        return {"ok": True, "status": "NO_SOURCE", "service": "source_read", "coverage": {"candidate_count": 0}, "roots": []}

    def _list_sources(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "list_sources", payload, context=context)

    def _scan_summary(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("source_read")
        if service is None:
            raise NativePortError("v2_source_read_service_unavailable")
        return self._service_result(service, "scan_summary", payload, context=context)

    def _import_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        # Import preview resolves configured roots and therefore is scoped even
        # though the operation is read-only.  Require the process-issued
        # authority before allowing the service to inspect a requested path;
        # missing/forged bindings fail closed without a path oracle.
        try:
            resolve_native_transport_context(context)
        except NativeContextError as exc:
            raise NativePortError("trusted_context_capability_required") from exc
        service = self._native_service("import_preview")
        if service is None:
            raise NativePortError("v2_import_preview_service_unavailable")
        return self._service_result(service, "memoryguard_import_preview", payload, context=context)

    def _runtime_processes(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        service = self._native_service("runtime_diagnostics")
        if service is None:
            raise NativePortError("v2_runtime_diagnostics_service_unavailable")
        # Diagnostics summary is available without a binding, but admin detail
        # is only enabled by the immutable authority.  Strip forged mapping
        # admin flags while preserving the canonical authority projection.
        try:
            authority = resolve_native_transport_context(context)
            runtime_context: Mapping[str, Any] = {
                "admin": bool(authority.admin),
                "is_admin": bool(authority.is_admin),
            }
        except NativeContextError:
            runtime_context = {}
        return self._service_result(service, "memoryguard_runtime_processes", payload, context=runtime_context)

    def _external_mcp_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("external_mcp")
        if service is None:
            raise NativePortError("v2_external_mcp_service_unavailable")
        return self._service_result(service, operation, payload, context=context, **kwargs)

    def _external_mcp_list(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "memoryguard_external_mcp_list", payload, context, **kwargs,
        )

    def _external_mcp_detect(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "detect_external_mcp", payload, context, **kwargs,
        )

    def _external_mcp_preview(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "preview_external_mcp_import", payload, context, **kwargs,
        )

    def _external_mcp_import(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._external_mcp_operation(
            "memoryguard_external_mcp_import", payload, context, **kwargs,
        )

    def _rule_merge_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        state: Any = None,
        mutation: bool = True,
        **_: Any,
    ) -> Any:
        service = self._native_service("rule_merge")
        if service is None:
            raise NativePortError("v2_rule_merge_service_unavailable")
        return self._service_result(
            service,
            operation,
            payload,
            context=context,
            generation=generation,
            state=state,
            mutation=mutation,
        )

    @staticmethod
    def _binding_matches_context(binding: Any, context: Mapping[str, Any]) -> bool:
        target_type = _text(getattr(binding, "target_type", "")).casefold()
        target_id = _text(getattr(binding, "target_id", ""))
        project_ref = _text(getattr(binding, "project_ref", ""))
        provider = _text(getattr(binding, "provider", "")).casefold()
        runtime_role = _text(getattr(binding, "runtime_role", "")).casefold()
        if target_type == "system":
            return True
        if target_type == "group":
            return target_id in {"", _text(context.get("share_group_id"))}
        if target_type == "agent":
            return target_id == _text(context.get("agent_instance_id"))
        if target_type == "project":
            return (project_ref or target_id) == _text(context.get("project_ref"))
        if target_type == "agent_project":
            return target_id == _text(context.get("agent_instance_id")) and project_ref == _text(context.get("project_ref"))
        if target_type == "provider":
            return target_id.casefold() == _text(context.get("provider")).casefold()
        if target_type == "runtime_role":
            return target_id.casefold() == _text(context.get("runtime_role")).casefold()
        return False

    def _gui_rule_scope_options(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        admin = self._trusted_admin(context)
        target_types = ["agent", "agent_project"]
        if admin:
            target_types.extend(["group", "project", "provider", "runtime_role", "system"])
        return {
            "target_types": target_types,
            "agents": ([{"id": _text(context.get("agent_instance_id"))}] if context.get("agent_instance_id") else []),
            "groups": ([{"id": _text(context.get("share_group_id"))}] if context.get("share_group_id") else []),
            "projects": ([{"id": _text(context.get("project_ref"))}] if context.get("project_ref") else []),
            "providers": ([{"id": _text(context.get("provider"))}] if context.get("provider") else []),
            "runtime_roles": ([{"id": _text(context.get("runtime_role"))}] if context.get("runtime_role") else []),
            "automatic_scope_policy": ["agent", "agent_project"],
        }

    def _gui_rule_snapshot(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "rules": [], "effective": [], "excluded": [], "total": 0}
        try:
            store = self._domain_store("rules")
            group = _text(context.get("share_group_id"))
            bindings = [item for item in store.list_bindings(share_group_id=group, status="active")]
            definitions = {item.definition_id: item for item in store.list_definitions(status="active")}
            by_definition: dict[str, list[Any]] = {}
            for binding in bindings:
                by_definition.setdefault(binding.definition_id, []).append(binding)
            rules: list[dict[str, Any]] = []
            effective: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = []
            for definition_id, scoped in sorted(by_definition.items()):
                definition = definitions.get(definition_id)
                if definition is None:
                    continue
                visible_bindings = [item for item in scoped if item.owner_agent_id in {"", _text(context.get("agent_instance_id"))} or self._trusted_admin(context)]
                if not visible_bindings:
                    continue
                includes = [item for item in visible_bindings if item.effect != "exclude" and self._binding_matches_context(item, context)]
                excludes = [item for item in visible_bindings if item.effect == "exclude" and self._binding_matches_context(item, context)]
                row = {
                    "definition_id": definition.definition_id,
                    "canonical_text": definition.canonical_text,
                    "rule_kind": definition.rule_kind,
                    "polarity": definition.polarity,
                    "rule_strength": definition.rule_strength,
                    "maturity_state": definition.maturity_state,
                    "revision": definition.revision,
                    "bindings": [item.to_dict() for item in visible_bindings],
                    "effective": bool(includes and not excludes),
                    "excluded": bool(excludes),
                }
                rules.append(row)
                if excludes:
                    excluded.append(row)
                elif includes:
                    effective.append(row)
            return {
                "status": "READY",
                "rules": rules,
                "effective": effective,
                "excluded": excluded,
                "total": len(rules),
                "scope_options": self._gui_rule_scope_options({}, context),
            }
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_rule_snapshot_unavailable") from exc

    def _gui_rule_effective(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        snapshot = self._gui_rule_snapshot(payload, context, **kwargs)
        return {
            "status": snapshot.get("status", "READY"),
            "context": {
                "agent_instance_id": _text(context.get("agent_instance_id")),
                "share_group_id": _text(context.get("share_group_id")),
                "project_ref": _text(context.get("project_ref")),
                "provider": _text(context.get("provider")),
                "runtime_role": _text(context.get("runtime_role")),
            },
            "effective": list(snapshot.get("effective") or []),
            "excluded": list(snapshot.get("excluded") or []),
            "unavailable": [item for item in snapshot.get("rules", []) if not item.get("effective") and not item.get("excluded")],
        }

    def _rule_allowed_ids(self, conn: sqlite3.Connection, context: Mapping[str, Any]) -> set[str]:
        group = _text(context.get("share_group_id"))
        rows = conn.execute(
            "SELECT definition_id FROM rule_bindings WHERE share_group_id=?",
            (group,),
        ).fetchall()
        allowed = {str(row[0]) for row in rows if str(row[0] or "")}
        links = conn.execute(
            "SELECT memory_id,canonical_definition_id FROM rule_source_links WHERE share_group_id=?",
            (group,),
        ).fetchall()
        for row in links:
            if str(row[0] or ""):
                allowed.add(str(row[0]))
            if str(row[1] or ""):
                allowed.add(str(row[1]))
        return allowed

    def _gui_rule_decisions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        raw_limit = payload.get("limit", 50)
        try:
            limit = max(1, min(int(raw_limit or 50), 200))
        except (TypeError, ValueError) as exc:
            raise NativePortError("invalid_limit") from exc
        if not self.layout.rules_db.is_file():
            return {"decisions": [], "total": 0, "status": "NO_SOURCE"}
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                allowed = self._rule_allowed_ids(conn, context)
                rows = conn.execute("SELECT * FROM rule_decisions ORDER BY created_at DESC,decision_id LIMIT ?", (limit * 4,)).fetchall()
            values: list[dict[str, Any]] = []
            agent = _text(context.get("agent_instance_id"))
            admin = self._trusted_admin(context)
            for row in rows:
                item = {str(key): row[key] for key in row.keys()}
                owner = _text(item.get("owner_agent_id") or item.get("actor"))
                rule_id = _text(item.get("rule_id"))
                if not admin and owner not in {"", agent}:
                    continue
                if not admin and not owner and rule_id not in allowed:
                    continue
                # Decision bodies are hash/JSON state, not source text; still
                # keep the public view compact and bounded.
                values.append({
                    key: item.get(key) for key in (
                        "decision_id", "actor", "owner_agent_id", "rule_id", "action",
                        "before_hash", "after_hash", "reason", "confidence", "undo_id",
                        "target_ids_json", "created_at",
                    )
                })
                if len(values) >= limit:
                    break
            return {"decisions": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_decisions_unavailable") from exc

    def _gui_rule_receipts(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self.layout.rules_db.is_file():
            return {"receipts": [], "total": 0, "status": "NO_SOURCE"}
        try:
            limit = max(1, min(int(payload.get("limit", 50) or 50), 200))
            clauses = ["share_group_id=?"]
            params: list[Any] = [_text(context.get("share_group_id"))]
            memory_id = _text(payload.get("memory_id"))
            if memory_id:
                clauses.append("source_rule_id=?")
                params.append(memory_id)
            if not self._trusted_admin(context):
                clauses.append("agent_instance_id=?")
                params.append(_text(context.get("agent_instance_id")))
            params.append(limit)
            with open_database(self.layout.rules_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT receipt_id,definition_id,source_rule_id,share_group_id,agent_instance_id,project_ref,session_id,task_hash,selection_digest,created_at "
                    "FROM rule_receipt_refs WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC,receipt_id LIMIT ?",
                    params,
                ).fetchall()
            values = [
                {
                    "receipt_id": str(row[0]), "definition_id": str(row[1] or ""),
                    "source_rule_id": str(row[2] or ""), "share_group_id": str(row[3] or ""),
                    "agent_instance_id": str(row[4] or ""), "project_ref": str(row[5] or ""),
                    "session_id": str(row[6] or ""), "task_hash": str(row[7] or ""),
                    "selection_digest": str(row[8] or ""), "created_at": str(row[9] or ""),
                }
                for row in rows
            ]
            return {"receipts": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_receipts_unavailable") from exc

    def _gui_rule_exceptions(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self.layout.rules_db.is_file():
            return {"exceptions": [], "total": 0, "status": "NO_SOURCE"}
        parent = _text(payload.get("parent_rule"))
        try:
            with open_database(self.layout.rules_db, readonly=True) as conn:
                allowed = self._rule_allowed_ids(conn, context)
                rows = conn.execute("SELECT * FROM rule_exceptions WHERE active=1 ORDER BY created_at,exception_id").fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                item = {str(key): row[key] for key in row.keys()}
                parent_id = _text(item.get("parent_rule_id") or item.get("parent_rule"))
                if parent_id not in allowed:
                    continue
                if parent and parent not in {parent_id, _text(item.get("parent_rule"))}:
                    continue
                values.append({
                    key: item.get(key) for key in (
                        "exception_id", "parent_rule_id", "child_exception_id", "priority",
                        "reason", "active", "created_at", "updated_at",
                    )
                })
            return {"exceptions": values, "total": len(values), "status": "READY"}
        except Exception as exc:
            raise NativePortError("v2_rule_exceptions_unavailable") from exc

    def _gui_rule_audience_update(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        rule_id = _text(payload.get("memory_id") or payload.get("definition_id") or payload.get("rule_id"))
        assignments = payload.get("assignments")
        if not rule_id or not isinstance(assignments, list):
            raise NativePortError("rule_audience_payload_required")
        policy = _text(payload.get("injection_policy") or "always")
        if policy not in {"always", "relevant"}:
            raise NativePortError("invalid_injection_policy")
        if policy == "relevant" and assignments:
            raise NativePortError("relevant_rule_cannot_have_assignments")
        if policy == "always" and not assignments:
            raise NativePortError("always_rule_requires_include_audience")
        try:
            store = self._domain_store("rules", write=True)
            definition_id = rule_id if store.get_definition(rule_id) is not None else ""
            if not definition_id:
                with open_database(self.layout.rules_db, readonly=True) as conn:
                    row = conn.execute(
                        "SELECT canonical_definition_id FROM rule_source_links WHERE share_group_id=? AND memory_id=? ORDER BY updated_at DESC LIMIT 1",
                        (_text(context.get("share_group_id")), rule_id),
                    ).fetchone()
                definition_id = str(row[0] or "") if row is not None else ""
            if not definition_id or store.get_definition(definition_id) is None:
                raise NativePortError("rule_definition_not_found")
            from ..rule_binding import RuleBinding
            group = _text(context.get("share_group_id"))
            existing = store.list_bindings(definition_id=definition_id, share_group_id=group)
            before = [item.to_dict() for item in existing]
            # Preserve history by deactivating prior bindings rather than
            # deleting permission evidence.
            for binding in existing:
                if binding.status == "active":
                    store.upsert_binding(RuleBinding.from_dict({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1}))
            created: list[dict[str, Any]] = []
            if policy == "always":
                options = self._gui_rule_scope_options({}, context)
                allowed_types = set(options["target_types"])
                for raw in assignments:
                    if not isinstance(raw, Mapping):
                        raise NativePortError("invalid_rule_assignment")
                    target_type = _text(raw.get("target_type")).casefold()
                    target_id = _text(raw.get("target_id"))
                    project_ref = _text(raw.get("project_ref"))
                    effect = _text(raw.get("effect") or "include").casefold()
                    if target_type not in allowed_types or effect not in {"include", "exclude"}:
                        raise NativePortError("invalid_rule_assignment")
                    trusted_values = {
                        "agent": _text(context.get("agent_instance_id")),
                        "group": group,
                        "project": _text(context.get("project_ref")),
                        "provider": _text(context.get("provider")),
                        "runtime_role": _text(context.get("runtime_role")),
                    }
                    if target_type == "agent" and target_id != trusted_values["agent"]:
                        raise NativePortError("unknown_agent_target")
                    if target_type == "group" and target_id not in {"", trusted_values["group"]}:
                        raise NativePortError("unknown_group_target")
                    if target_type == "project" and (project_ref or target_id) != trusted_values["project"]:
                        raise NativePortError("unknown_project_target")
                    if target_type == "agent_project" and (target_id != trusted_values["agent"] or project_ref != trusted_values["project"]):
                        raise NativePortError("unknown_agent_project_target")
                    if target_type == "provider" and target_id.casefold() != trusted_values["provider"].casefold():
                        raise NativePortError("unknown_provider_target")
                    if target_type == "runtime_role" and target_id.casefold() != trusted_values["runtime_role"].casefold():
                        raise NativePortError("unknown_runtime_role_target")
                    if target_type == "system" and (target_id or project_ref):
                        raise NativePortError("system_target_must_be_empty")
                    priority = int(raw.get("priority", payload.get("priority", 0)) or 0)
                    binding_id = hashlib.sha256(_canonical_json({
                        "definition_id": definition_id, "group": group, "target_type": target_type,
                        "target_id": target_id, "project_ref": project_ref, "provider": target_id if target_type == "provider" else "",
                        "runtime_role": target_id if target_type == "runtime_role" else "", "effect": effect, "priority": priority,
                    }).encode("utf-8")).hexdigest()[:32]
                    binding = RuleBinding(
                        binding_id=binding_id,
                        definition_id=definition_id,
                        share_group_id=group,
                        target_type=target_type,
                        target_id=target_id,
                        project_ref=project_ref,
                        provider=target_id if target_type == "provider" else "",
                        runtime_role=target_id if target_type == "runtime_role" else "",
                        effect=effect,
                        priority=priority,
                        owner_agent_id=_text(context.get("agent_instance_id")),
                        created_by="admin",
                        authorization=f"native-gui:{_text(context.get('session_id'))}",
                        status="active",
                    )
                    persisted = store.upsert_binding(binding)
                    created.append(persisted.to_dict())
            after = created
            decision_id = hashlib.sha256(_canonical_json({"operation": "gui_rule_audience_update", "definition_id": definition_id, "before": before, "after": after}).encode("utf-8")).hexdigest()
            store.record_decision({
                "decision_id": decision_id,
                "actor": _text(context.get("agent_instance_id")),
                "owner_agent_id": _text(context.get("agent_instance_id")),
                "rule_id": definition_id,
                "action": "rule_audience_update",
                "before_hash": hashlib.sha256(_canonical_json(before).encode("utf-8")).hexdigest(),
                "after_hash": hashlib.sha256(_canonical_json(after).encode("utf-8")).hexdigest(),
                "before_json": _canonical_json(before),
                "after_json": _canonical_json(after),
                "reason": "native GUI rule audience update",
                "confidence": 1.0,
                "undo_id": "",
                "target_ids_json": _canonical_json([item.get("binding_id") for item in after]),
                "metadata_json": _canonical_json({"injection_policy": policy}),
                "source_ref": "native-v2:gui:rule_audience_update",
            })
            return {"ok": True, "definition_id": definition_id, "injection_policy": policy, "bindings": after, "decision_id": decision_id}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_rule_audience_update_failed") from exc

    def _gui_rule_create(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        text = _text(payload.get("text"))
        if not text:
            raise NativePortError("rule_text_required")
        clean = {
            "text": text,
            "idempotency_key": _text(payload.get("idempotency_key")) or "gui-rule-create:" + hashlib.sha256(
                _canonical_json({"text": text, "agent": context.get("agent_instance_id", ""), "project": context.get("project_ref", "")}).encode("utf-8")
            ).hexdigest(),
        }
        return self._rule_lifecycle_operation("rule_create_auto", clean, context, **kwargs)

    def _gui_rule_feedback(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        clean = {
            "receipt_id": _text(payload.get("receipt_id")),
            "outcome": _text(payload.get("outcome")),
            "evidence": str(payload.get("evidence") or ""),
            "confidence": payload.get("confidence", 1.0),
        }
        clean["idempotency_key"] = _text(payload.get("idempotency_key")) or "gui-rule-feedback:" + hashlib.sha256(
            _canonical_json({"receipt_id": clean["receipt_id"], "outcome": clean["outcome"], "evidence": clean["evidence"], "agent": context.get("agent_instance_id", "")}).encode("utf-8")
        ).hexdigest()
        return self._rule_lifecycle_operation("rule_feedback", clean, context, **kwargs)

    def _gui_rule_undo(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        decision_id = _text(payload.get("decision_id"))
        if not decision_id:
            raise NativePortError("decision_id_required")
        return self._rule_lifecycle_operation("rule_undo", {"decision_id": decision_id}, context, **kwargs)

    def _rule_lifecycle_operation(
        self,
        operation: str,
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        service = self._native_service("rule_lifecycle")
        if service is None:
            raise NativePortError("v2_rule_lifecycle_service_unavailable")
        return self._service_result(
            service, operation, payload, context=context, **kwargs,
        )

    @staticmethod
    def _bridge_transport(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return {"transport": "SafeBridgeApi", "available": True}

    @staticmethod
    def _gui_pick_path(payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del context
        return {"host_action": "pick_path", "for_files": bool(payload.get("for_files", False)), "gated_by": "v2_native_manifest"}

    def _gui_source_add(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        path = _text(payload.get("path"))
        if not path:
            raise NativePortError("source_path_required")
        source_type = _text(payload.get("source_type") or "selected_directory").casefold()
        try:
            from ..source_registry import SourceRegistry
            from ..schema_v3 import SourceRootType
            aliases = {
                "directory": SourceRootType.SELECTED_DIRECTORY,
                "selected_directory": SourceRootType.SELECTED_DIRECTORY,
                "file": SourceRootType.SELECTED_FILE,
                "selected_file": SourceRootType.SELECTED_FILE,
                "obsidian": SourceRootType.OBSIDIAN_VAULT,
                "obsidian_vault": SourceRootType.OBSIDIAN_VAULT,
            }
            enum_type = aliases.get(source_type)
            if enum_type is None:
                raise NativePortError("invalid_source_type")
            if source_type == "directory" and (Path(path).expanduser() / ".obsidian").is_dir():
                enum_type = SourceRootType.OBSIDIAN_VAULT
            reg = SourceRegistry(self.workspace)
            root = reg.add(path, enum_type, display_name=_text(payload.get("display_name")))
            return {"ok": True, "root_id": root.root_id, "type": root.type.value, "display_name": root.display_name}
        except NativePortError:
            raise
        except FileNotFoundError as exc:
            raise NativePortError("source_path_not_found") from exc
        except Exception as exc:
            raise NativePortError("v2_source_add_failed") from exc

    def _gui_source_remove(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        source_id = _text(payload.get("source_id") or payload.get("root_id"))
        if not source_id:
            raise NativePortError("source_id_required")
        try:
            from ..source_registry import SourceRegistry
            ok = SourceRegistry(self.workspace).remove(source_id)
            if not ok:
                raise NativePortError("source_not_removable")
            return {"ok": True, "root_id": source_id}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_source_remove_failed") from exc

    def _gui_memory_source_map(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload
        scope = self._scope(context)
        if not self.layout.memory_db.is_file():
            return {"status": "NO_SOURCE", "mappings": [], "total": 0}
        clauses = ["a.workspace_id=?", "a.share_group_id=?"]
        params: list[Any] = [self.workspace, scope.get("share_group_id", "")]
        for column, value in (
            ("a.agent_instance_id", scope.get("agent_instance_id", "")),
            ("a.project_ref", scope.get("project_ref", "")),
            ("a.provider", scope.get("provider", "")),
            ("a.runtime_role", scope.get("runtime_role", "")),
        ):
            if value:
                clauses.append(column + "=?")
                params.append(value)
        try:
            with open_database(self.layout.memory_db, readonly=True) as conn:
                rows = conn.execute(
                    "SELECT a.memory_id,m.source_domain,m.source_ref,m.source_record_id,m.source_revision,m.digest,m.provenance_json "
                    "FROM source_mappings m JOIN atoms a ON a.atom_id=m.atom_id WHERE " + " AND ".join(clauses) + " ORDER BY a.memory_id,m.mapping_id",
                    params,
                ).fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                source_ref = str(row[2] or "")
                if source_ref and (Path(source_ref).is_absolute() or source_ref.startswith("\\\\")):
                    source_ref = "source:" + hashlib.sha256(source_ref.encode("utf-8", "replace")).hexdigest()[:24]
                values.append({
                    "memory_id": str(row[0] or ""),
                    "source_domain": str(row[1] or ""),
                    "source_ref": source_ref,
                    "source_record_id": str(row[3] or ""),
                    "source_revision": str(row[4] or ""),
                    "digest": str(row[5] or ""),
                    "provenance": _plain(json.loads(str(row[6] or "{}"))),
                })
            return {"status": "READY", "mappings": values, "total": len(values)}
        except Exception as exc:
            raise NativePortError("v2_memory_source_map_unavailable") from exc

    def _gui_groups(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        del payload, kwargs
        return self._cli_groups({"action": "list"}, context)

    def _cli_audit(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._reference_audit(payload, context, **kwargs)

    def _cli_explain(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._explain(payload, context, **kwargs)

    def _cli_scan(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        return self._scan_summary(payload, context, **kwargs)

    @staticmethod
    def _cli_host_action(name: str) -> dict[str, Any]:
        return {"host_action": name, "gated_by": "v2_native_manifest"}

    def _cli_source(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action not in {"list", "add", "remove", "preview"}:
            raise NativePortError("invalid_source_action")
        if action in {"add", "remove"} and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("source")

    def _cli_provider(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        if _text(payload.get("action")).casefold() != "repair":
            raise NativePortError("invalid_provider_action")
        if not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("provider")

    def _cli_hooks(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action not in {"status", "install", "ensure", "uninstall", "mode"}:
            raise NativePortError("invalid_hooks_action")
        if action != "status" and not self._trusted_admin(context):
            raise NativePortError("admin_capability_required")
        return self._cli_host_action("hooks")

    def _cli_groups(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        action = _text(payload.get("action")).casefold()
        if action == "migrate":
            raise NativePortError("v2_groups_migrate_retired")
        if action != "list":
            raise NativePortError("invalid_groups_action")
        if not self.layout.rules_db.is_file():
            return {"status": "NO_SOURCE", "groups": [], "total": 0}
        try:
            store = self._domain_store("rules")
            bindings = list(store.list_bindings(status="active"))
            if not self._trusted_admin(context):
                group = _text(context.get("share_group_id"))
                bindings = [item for item in bindings if _text(getattr(item, "share_group_id", "")) == group]
            grouped: dict[str, dict[str, Any]] = {}
            for binding in bindings:
                gid = _text(getattr(binding, "share_group_id", ""))
                if not gid:
                    continue
                item = grouped.setdefault(gid, {"share_group_id": gid, "binding_count": 0, "definition_ids": set()})
                item["binding_count"] += 1
                item["definition_ids"].add(_text(getattr(binding, "definition_id", "")))
            values = [
                {
                    "share_group_id": gid,
                    "binding_count": int(item["binding_count"]),
                    "definition_count": len(item["definition_ids"]),
                }
                for gid, item in sorted(grouped.items())
            ]
            return {"status": "READY", "groups": values, "total": len(values)}
        except NativePortError:
            raise
        except Exception as exc:
            raise NativePortError("v2_groups_status_unavailable") from exc

    def _cli_workspace_health(self, *, state: Any = None, generation: Any = None) -> dict[str, Any]:
        """Return non-tenant CLI health when no Agent binding is available.

        ``doctor`` and ``mcp-status`` are installation/control-plane diagnostics.
        A terminal process may legitimately have no host-issued Agent binding,
        especially immediately after activation.  In that case expose only
        workspace-level availability and manifest/coverage state: never record
        counts, memory bodies, group IDs, or any cross-tenant existence oracle.
        """
        coverage = self.coverage()
        return {
            "status": "READY",
            "scope_status": "UNBOUND",
            "manifest": {
                "state": _text(getattr(state, "value", state)),
                "generation": generation if type(generation) is int else None,
            },
            "domains": {
                "memory": self.layout.memory_db.is_file(),
                "rules": self.layout.rules_db.is_file(),
                "evidence": self.layout.evidence_db.is_file(),
                "content": self.layout.content_db.is_file(),
                "runtime": self.layout.runtime_db.is_file(),
            },
            "native_coverage": {
                "counts": dict(coverage.get("counts") or {}),
                "production_complete": bool(coverage.get("production_complete")),
            },
        }

    def _cli_mcp_status(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not context.get("share_group_id"):
            health = self._cli_workspace_health(
                state=kwargs.get("state"), generation=kwargs.get("generation")
            )
            return {
                **health,
                "available": bool(health["domains"].get("memory")),
                "memory_status": "READY" if health["domains"].get("memory") else "NO_SOURCE",
            }
        return self._memory_status(payload, context, **kwargs)

    def _cli_doctor(self, payload: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any) -> Any:
        if not context.get("share_group_id"):
            return self._cli_workspace_health(
                state=kwargs.get("state"), generation=kwargs.get("generation")
            )
        return {
            "status": "READY",
            "scope_status": "BOUND",
            "diagnostics": self._diagnostics_snapshot(payload, context, **kwargs),
            "native_coverage": {
                "counts": dict(self.coverage().get("counts") or {}),
                "production_complete": bool(self.coverage().get("production_complete")),
            },
        }

    def _cli_gui(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("gui")

    def _cli_open(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("open")

    def _cli_desktop(self, payload: Mapping[str, Any], context: Mapping[str, Any], **_: Any) -> Any:
        del payload, context
        return self._cli_host_action("desktop")

    def _maintenance(self, payload: Mapping[str, Any], context: Mapping[str, Any], *, generation: int, mutation: bool, **_: Any) -> Any:
        if self.maintenance_port is None:
            raise NativePortError("v2_maintenance_service_unavailable")
        fn = getattr(self.maintenance_port, "dispatch", None) or getattr(self.maintenance_port, "call", None)
        if not callable(fn):
            raise NativePortError("v2_maintenance_service_unavailable")
        return fn("cli", "storage", dict(payload), context=context, generation=generation, mutation=mutation)

    def _store(self) -> Any:
        """Compatibility seam for maintenance recovery/replay tests.

        The business implementation remains in ``MaintenanceRuntimePort``;
        this method only exposes its already-lazy store for callers that need
        to inspect a job receipt after an injected fault.
        """
        if self.maintenance_port is None:
            raise NativePortError("v2_maintenance_service_unavailable")
        fn = getattr(self.maintenance_port, "_store", None)
        if not callable(fn):
            raise NativePortError("v2_maintenance_service_unavailable")
        return fn()

    def _builtin(self, handler: str) -> Callable[..., Any] | None:
        methods: dict[str, Callable[..., Any]] = {
            "list_sources": self._list_sources,
            "scan_summary": self._scan_summary,
            "import_preview": self._import_preview,
            "runtime_processes": self._runtime_processes,
            "external_mcp_list": self._external_mcp_list,
            "external_mcp_detect": self._external_mcp_detect,
            "external_mcp_preview": self._external_mcp_preview,
            "external_mcp_import": self._external_mcp_import,
            "history_search": lambda p, context, **k: self._history_operation("search", p, context, **k),
            "history_timeline": lambda p, context, **k: self._history_operation("timeline", p, context, **k),
            "history_read": lambda p, context, **k: self._history_operation("read", p, context, **k),
            "history_extract_preview": lambda p, context, **k: self._history_operation("extract_preview", p, context, **k),
            "history_list_sessions": lambda p, context, **k: self._history_operation("list_sessions", p, context, **k),
            "history_export": lambda p, context, **k: self._history_operation("export", p, context, **k),
            "history_delete": lambda p, context, **k: self._history_operation("delete", p, context, **k),
            "memory_read": self._memory_read,
            "memory_list": self._memory_list,
            "memory_search": self._memory_search,
            "memory_status": self._memory_status,
            "memory_versions": self._memory_versions,
            "memory_supersede_chain": self._memory_supersede_chain,
            "coverage": lambda p, context, **k: self.coverage(),
            "memory_write": self._memory_write,
            "memory_update": self._memory_update,
            "memory_delete": self._memory_delete,
            "gui_memory_edit": self._gui_memory_edit,
            "gui_memory_lock": self._gui_memory_lock,
            "gui_memory_unlock": self._gui_memory_unlock,
            "gui_memory_policy": self._gui_memory_policy,
            "gui_memory_restore": self._gui_memory_restore,
            "gui_memory_delete": self._gui_memory_delete,
            "gui_memory_rollback": self._gui_memory_rollback,
            "context_bootstrap": self._context_bootstrap,
            "hook_bootstrap": self._context_bootstrap,
            "rule_create_auto": lambda p, context, **k: self._rule_lifecycle_operation("rule_create_auto", p, context, **k),
            "rule_feedback": lambda p, context, **k: self._rule_lifecycle_operation("rule_feedback", p, context, **k),
            "rule_undo": lambda p, context, **k: self._rule_lifecycle_operation("rule_undo", p, context, **k),
            "rule_decision_read": lambda p, context, **k: self._rule_lifecycle_operation("rule_decision_read", p, context, **k),
            "rule_scope_stats": lambda p, context, **k: self._rule_lifecycle_operation("rule_scope_stats", p, context, **k),
            "gui_rule_create": self._gui_rule_create,
            "gui_rule_feedback": self._gui_rule_feedback,
            "gui_rule_undo": self._gui_rule_undo,
            "gui_rule_snapshot": self._gui_rule_snapshot,
            "gui_rule_effective": self._gui_rule_effective,
            "gui_rule_scope_options": self._gui_rule_scope_options,
            "gui_rule_decisions": self._gui_rule_decisions,
            "gui_rule_receipts": self._gui_rule_receipts,
            "gui_rule_exceptions": self._gui_rule_exceptions,
            "gui_rule_audience_update": self._gui_rule_audience_update,
            "binding_list": self._binding_list,
            "binding_create": self._binding_create,
            "rule_merge_capability_issue": lambda p, context, **k: self._rule_merge_operation("capability_issue", p, context, **k),
            "rule_merge_approve": lambda p, context, **k: self._rule_merge_operation("approve", p, context, **k),
            "rule_merge_acknowledge": lambda p, context, **k: self._rule_merge_operation("acknowledge", p, context, **k),
            "rule_merge_cooldown_clear": lambda p, context, **k: self._rule_merge_operation("cooldown_clear", p, context, **k),
            "extract_memories": self._extract_memories,
            "gui_extract_preview": self._gui_extract_preview,
            "gui_extract_by_path": self._gui_extract_by_path,
            "accept_candidates": self._accept_candidates,
            "list_pending_enrichments": self._list_pending_enrichments,
            "apply_enrichments": self._apply_enrichments,
            "enrichment_status": self._enrichment_status,
            "build_and_enrich": self._build_and_enrich,
            "resolve_group": self._resolve_group,
            "knowledge_read": self._knowledge_read,
            "knowledge_book": self._knowledge_book,
            "knowledge_candidates": self._knowledge_candidates,
            "reference_audit": self._reference_audit,
            "explain": self._explain,
            "codegraph_graph": self._codegraph_graph,
            "semantic_check": self._semantic_check,
            "provider_install": self._provider_install,
            "status": self._status,
            "sandbox_status": self._sandbox_status,
            "host_enrichment_guide": self._host_enrichment_guide,
            "host_llm_agents": self._host_llm_agents,
            "diagnostics_snapshot": self._diagnostics_snapshot,
            "scope_echo": self._scope_echo,
            "hook_status": self._hook_status,
            "bridge_transport": self._bridge_transport,
            "gui_pick_path": self._gui_pick_path,
            "gui_source_add": self._gui_source_add,
            "gui_source_remove": self._gui_source_remove,
            "gui_memory_source_map": self._gui_memory_source_map,
            "gui_groups": self._gui_groups,
            "cli_audit": self._cli_audit,
            "cli_explain": self._cli_explain,
            "cli_scan": self._cli_scan,
            "cli_source": self._cli_source,
            "cli_provider": self._cli_provider,
            "cli_hooks": self._cli_hooks,
            "cli_groups": self._cli_groups,
            "cli_mcp_status": self._cli_mcp_status,
            "cli_doctor": self._cli_doctor,
            "cli_gui": self._cli_gui,
            "cli_open": self._cli_open,
            "cli_desktop": self._cli_desktop,
            "maintenance": self._maintenance,
            "projection_status": self._projection_status,
            "canonical_status": self._canonical_status,
            "diagnostics_status": lambda p, context, **k: self._status(p, context, **k),
            "codegraph_status": lambda p, context, **k: self._domain_status("codegraph", p, context, **k),
            "asset_status": lambda p, context, **k: self._domain_status("assets", p, context, **k),
            "skill_status": lambda p, context, **k: self._domain_status("skills", p, context, **k),
        }
        return methods.get(handler)

    def _call(
        self,
        fn: Callable[..., Any],
        payload: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        generation: int,
        mutation: bool,
        state: Any = None,
    ) -> Any:
        try:
            params = inspect.signature(fn).parameters
            accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values())
        except (TypeError, ValueError):
            params, accepts_kwargs = {}, False
        if (mutation or context) and not accepts_kwargs and "context" not in params:
            raise NativePortError("v2_context_capability_required")
        kwargs = {
            key: value for key, value in {
                "context": context,
                "generation": generation,
                "mutation": mutation,
                "state": state,
            }.items() if accepts_kwargs or key in params
        }
        return fn(payload, **kwargs)

    @staticmethod
    def _provider_snapshot(provider: Any) -> tuple[str, int | None]:
        """Read one trusted manifest snapshot from an injected provider."""
        value = provider
        current = getattr(provider, "current", None)
        if callable(current):
            value = current()
        elif callable(provider):
            value = provider()
        if isinstance(value, Mapping):
            state = value.get("state")
            generation = value.get("generation")
        else:
            state = getattr(value, "state", None)
            generation = getattr(value, "generation", None)
        state_text = _text(getattr(state, "value", state)).upper()
        return state_text, generation if type(generation) is int else None

    def _validated_dispatch_state(
        self,
        surface: str,
        name: str,
        generation: Any,
        mutation: bool,
        state: Any,
    ) -> tuple[dict[str, Any] | None, Any]:
        """Enforce state/generation at the native boundary.

        V2RuntimeFacade supplies a trusted immutable state snapshot.  Direct
        native callers must either provide that snapshot or inject a
        ``state_provider``; mutations without either capability are refused.
        """
        if type(generation) is not int or generation < 0:
            return self._error(surface, name, "invalid_manifest_generation"), state
        effective_state = state
        provider_generation: int | None = None
        # Mutations must always compare against a trusted provider snapshot.
        # A state value supplied in an RPC payload is only a CAS hint and may
        # not bootstrap a direct native write by itself.
        if mutation and self.state_provider is None:
            return self._error(surface, name, "v2_state_provider_required", generation=generation), state
        if (mutation or effective_state is None) and self.state_provider is not None:
            try:
                provider_state, provider_generation = self._provider_snapshot(self.state_provider)
                if effective_state is not None:
                    supplied_state = _text(getattr(effective_state, "value", effective_state)).upper()
                    if supplied_state and supplied_state != provider_state:
                        return self._error(surface, name, "manifest_state_mismatch", generation=generation), state
                effective_state = provider_state
            except Exception:
                return self._error(surface, name, "v2_manifest_state_unavailable", generation=generation), state
        if mutation and provider_generation is None:
            return self._error(surface, name, "v2_manifest_generation_unavailable", generation=generation), effective_state
        if effective_state is None:
            if mutation:
                return self._error(surface, name, "v2_state_provider_required", generation=generation), effective_state
            return None, effective_state
        state_text = _text(getattr(effective_state, "value", effective_state)).upper()
        if state_text not in {"V2_READY", "V2_ACTIVE"}:
            return self._error(surface, name, "v2_manifest_state_unavailable", generation=generation), effective_state
        if provider_generation is not None and provider_generation != generation:
            return self._error(surface, name, "manifest_generation_mismatch", generation=generation), effective_state
        if mutation and state_text != "V2_ACTIVE":
            return self._error(surface, name, "v2_not_active", generation=generation), effective_state
        return None, effective_state

    def _validate_dispatch_state(
        self,
        surface: str,
        name: str,
        generation: Any,
        mutation: bool,
        state: Any,
    ) -> dict[str, Any] | None:
        """Compatibility wrapper returning only the state-gate error."""

        error, _effective_state = self._validated_dispatch_state(
            surface, name, generation, mutation, state,
        )
        return error

    @staticmethod
    def _classify_mutation(
        surface: str,
        name: str,
        args: Any,
        explicit: bool,
        spec: SurfaceSpec,
    ) -> bool:
        """Keep direct ports from downgrading a known CLI storage write."""
        if spec.mutation or explicit:
            return True
        if surface == "cli" and spec.handler == "maintenance":
            try:
                action = _text(_payload(args).get("action")).casefold()
            except NativePortError:
                return True
            # Maintenance audit/report are read-only.  Every other action is
            # conservatively treated as a write, independent of caller flags.
            return action not in {"audit", "report"}
        return False

    def _dispatch_checked(
        self,
        surface: str,
        name: str,
        args: Any,
        *,
        context: Any,
        generation: Any,
        mutation: bool,
        state: Any = None,
    ) -> dict[str, Any]:
        if type(generation) is not int or generation < 0:
            return self._error(surface, name, "invalid_manifest_generation")
        spec = self._registry.get(surface, {}).get(name)
        if spec is None:
            return self._error(surface, name, "unknown_surface_operation")
        if spec.status == "blocker":
            return self._error(surface, name, "v2_operation_not_implemented", status="blocked", generation=generation, reason=spec.reason)
        if spec.status == "retired":
            return self._error(surface, name, "v2_operation_retired", status="retired", generation=generation, reason=spec.reason)
        effective_mutation = self._classify_mutation(
            surface,
            name,
            args,
            bool(mutation),
            spec,
        )
        try:
            raw_payload = _phase9_gui_payload(surface, name, _payload(args))
            # ``provider`` is normally a protected identity selector.  For the
            # provider-install control-plane surface it is instead the target
            # integration name. Rename it before identity validation so the
            # trusted caller provider remains immutable and unambiguous.
            if surface == "mcp" and name == "memoryguard_provider_install" and "provider" in raw_payload:
                raw_payload = dict(raw_payload)
                raw_payload["target_provider"] = raw_payload.pop("provider")
            trusted = self._context(
                context,
                raw_payload,
                required=effective_mutation or surface in {"mcp", "gui", "hook"},
                allow_partial=(
                    surface == "cli" and spec.handler == "maintenance"
                ) or (
                    surface == "gui" and spec.handler in _PHASE9_GUI_READ_HANDLERS
                ),
            )
            # MaintenanceRuntimePort owns a separate private CLI capability
            # and validates it at its own boundary.  Do not require the MCP/
            # GUI native sentinel here for that one delegated handler; all
            # other mutations must carry the native transport capability.
            maintenance_route = surface == "cli" and spec.handler == "maintenance"
            if effective_mutation and not maintenance_route:
                resolve_native_transport_context(trusted)
            # GUI Phase 9 reads are scoped services too.  SafeBridge supplies
            # the process-issued bound context; plain identity mappings must
            # not become an authorization substitute (even for reads).
            if surface == "gui" and spec.handler in _PHASE9_GUI_HANDLERS:
                try:
                    authority = resolve_native_transport_context(trusted)
                except NativeContextError as exc:
                    raise NativePortError("trusted_context_capability_required") from exc
                if (
                    spec.handler in _PHASE9_GUI_READ_HANDLERS
                    and not authority.share_group_id
                ):
                    # Missing/ambiguous GUI binding is intentionally neutral;
                    # no source/history existence oracle is exposed.
                    if spec.handler.startswith("history_"):
                        return self._result(
                            surface,
                            name,
                            self._history_neutral(spec.handler),
                            generation=generation,
                        )
                    if spec.handler in {"list_sources", "scan_summary"}:
                        return self._result(
                            surface,
                            name,
                            self._source_neutral(spec.handler),
                            generation=generation,
                        )
                    if spec.handler in {"memory_versions", "memory_supersede_chain"}:
                        raise NativePortError("trusted_context_capability_required")
                    raise NativePortError("no_source")
            # Do not forward identity claims from the transport payload.  The
            # trusted context above is the only source of scope/identity.
            clean = {key: value for key, value in raw_payload.items() if key not in _IDENTITY_PAYLOAD_KEYS}
            fn = self._service(surface, name, spec.handler) or self._builtin(spec.handler)
            if fn is None:
                raise NativePortError("v2_operation_not_implemented")
            result = self._call(
                fn,
                clean,
                trusted,
                generation=generation,
                mutation=effective_mutation,
                state=state,
            )
            return self._result(surface, name, result, generation=generation)
        except NativePortError as exc:
            return self._error(surface, name, exc.code, generation=generation)
        except Exception:
            return self._error(surface, name, "v2_native_handler_failed", generation=generation)

    # ---- public dispatch -----------------------------------------------------
    def dispatch(self, surface: str, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        surface_key = _text(surface).casefold()
        operation = str(name or "")
        spec = self._registry.get(surface_key, {}).get(operation)
        if spec is None:
            return self._error(surface_key, operation, "unknown_surface_operation")
        # Classify command sub-actions before the state/CAS gate.  Otherwise a
        # caller could label a mutating maintenance action as read-only and
        # reach the handler without a trusted provider snapshot.
        effective_mutation = self._classify_mutation(
            surface_key,
            operation,
            args,
            bool(mutation),
            spec,
        )
        state_error, effective_state = self._validated_dispatch_state(
            surface_key, operation, generation, effective_mutation, state,
        )
        if state_error is not None:
            return state_error
        return self._dispatch_checked(
            surface_key,
            operation,
            args,
            context=context,
            generation=generation,
            mutation=effective_mutation,
            state=effective_state,
        )

    call = dispatch

    def bootstrap_hook(self, request: Any = None, payload: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        args = _payload(payload)
        if request is not None:
            args.setdefault("request", request)
        state_error, effective_state = self._validated_dispatch_state(
            "hook", "bootstrap_hook", generation, bool(mutation), state,
        )
        if state_error is not None:
            return state_error
        return self._dispatch_checked(
            "hook",
            "bootstrap_hook",
            args,
            context=context,
            generation=generation,
            mutation=mutation,
            state=effective_state,
        )

    bootstrap = bootstrap_hook

    # Surface-specific aliases make direct host integration explicit while all
    # calls still pass through the same registry/security path.
    def dispatch_mcp(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("mcp", name, args, context=context, generation=generation, mutation=mutation, state=state)

    def dispatch_gui(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("gui", name, args, context=context, generation=generation, mutation=mutation, state=state)

    def dispatch_cli(self, name: str, args: Any = None, *, context: Any = None, generation: int | None = None, mutation: bool = False, state: Any = None) -> dict[str, Any]:
        return self.dispatch("cli", name, args, context=context, generation=generation, mutation=mutation, state=state)


NativeRuntimePort = NativeV2RuntimePort

__all__ = [
    "NativeBoundContext", "NativeContextEnvelope", "NativeContextError", "NativePortError", "NativeRuntimePort", "NativeV2RuntimePort",
    "SurfaceSpec", "bind_native_transport_context", "resolve_native_transport_context", "bind_native_test_capability",
    "bind_native_test_services", "bind_native_test_store",
]
