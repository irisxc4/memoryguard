"""V2 Legacy API Adapter contract.

The adapter preserves the public MCP/GUI/CLI names while routing through
injected ports.  It intentionally does not import or construct any legacy
storage class, and it is not wired into the running server in this phase.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
import re
from typing import Any, Mapping, Protocol, runtime_checkable

from ..governance_v2.rules import (
    RuleAuthorizationError,
    RuleMutationContext,
    RuleMutationError,
)


# Public adapter/MCP/GUI envelopes must never carry exception text.  Validation
# errors historically used compact, machine-readable strings; keep that small
# compatibility subset while rejecting paths, SQL, secrets and arbitrary
# attacker-controlled text.
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]*(?::[a-z0-9_.-]+(?:,[a-z0-9_.-]+)*)?$")
_ERROR_KEYS = frozenset({"error", "detail", "exception", "traceback", "sql", "query"})
_PATH_KEYS = frozenset({"workspace", "source_path", "absolute_path", "canonical_store_path"})


def safe_error_code(value: Any, fallback: str = "operation_failed") -> str:
    """Return a stable public code without reflecting arbitrary error text."""
    candidate = str(value or "").strip().casefold()
    if len(candidate) <= 128 and _SAFE_CODE_RE.fullmatch(candidate):
        return candidate
    return str(fallback or "operation_failed")


def safe_exception_diagnostic(exc: BaseException, *, code: str) -> dict[str, str]:
    """Expose only exception type and a non-reversible diagnostic hash."""
    typename = type(exc).__name__ or "Exception"
    # Hashing the text is useful for correlating one failure without disclosing
    # its path/SQL/secret.  The text is never returned to a caller.
    digest = hashlib.sha256(
        f"{typename}\x00{str(exc)}".encode("utf-8", "replace"),
    ).hexdigest()[:16]
    return {"type": typename, "hash": digest, "code": safe_error_code(code)}


def sanitize_public_payload(value: Any, *, error_code: str = "operation_failed") -> Any:
    """Redact error/path fields in a public mapping while preserving data."""
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _ERROR_KEYS:
                if lowered == "error":
                    output[key] = safe_error_code(raw_value, error_code)
                # ``detail``/traceback/query/sql are intentionally omitted.
                continue
            if lowered == "code":
                output[key] = safe_error_code(raw_value, error_code)
                continue
            if lowered in _PATH_KEYS:
                output[key] = "<redacted>"
                continue
            if lowered == "path" and isinstance(raw_value, str):
                # Keep protocol route markers (v2/legacy/none), redact file
                # system paths that might arrive in an error payload.
                if raw_value.startswith(("/", "\\")) or (len(raw_value) > 2 and raw_value[1] == ":"):
                    output[key] = "<redacted>"
                    continue
            output[key] = sanitize_public_payload(raw_value, error_code=error_code)
        return output
    if isinstance(value, (list, tuple)):
        return [sanitize_public_payload(item, error_code=error_code) for item in value]
    return value


def invoke_once(fn: Any, candidates: tuple[tuple[Any, ...], ...], *, error_code: str) -> Any:
    """Select one callable shape from its signature, then invoke exactly once.

    A TypeError raised *inside* the implementation is an operation failure; it
    is never interpreted as a signature mismatch and never retried.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise RuleMutationError(error_code) from exc
    selected: tuple[Any, ...] | None = None
    for candidate in candidates:
        try:
            signature.bind(*candidate)
        except TypeError:
            continue
        selected = candidate
        break
    if selected is None:
        raise RuleMutationError(error_code)
    return fn(*selected)
from ..cutover_v2.surfaces import (
    CLI_COMMAND_NAMES as _SURFACE_CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES as _SURFACE_GUI_METHOD_NAMES,
    LEGACY_CLI_COMMAND_NAMES as _SURFACE_LEGACY_CLI_COMMAND_NAMES,
    LEGACY_GUI_METHOD_NAMES as _SURFACE_LEGACY_GUI_METHOD_NAMES,
    LEGACY_MCP_TOOL_NAMES as _SURFACE_LEGACY_MCP_TOOL_NAMES,
    MCP_MUTATION_NAMES as _SURFACE_MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES as _SURFACE_MCP_TOOL_NAMES,
    MUTATING_MCP_TOOL_NAMES as _SURFACE_MUTATING_MCP_TOOL_NAMES,
    RULE_MUTATION_GUI_NAMES as _SURFACE_RULE_MUTATION_GUI_NAMES,
    RULE_MUTATION_MCP_NAMES as _SURFACE_RULE_MUTATION_MCP_NAMES,
    SAFE_BRIDGE_METHOD_NAMES as _SURFACE_SAFE_BRIDGE_METHOD_NAMES,
    GUI_MUTATION_NAMES as _SURFACE_GUI_MUTATION_NAMES,
    MUTATING_GUI_METHOD_NAMES as _SURFACE_MUTATING_GUI_METHOD_NAMES,
    GUI_MUTATION_METHOD_NAMES as _SURFACE_GUI_MUTATION_METHOD_NAMES,
)


# Snapshot of the existing MCP public names.  Keep this list local so the
# contract cannot accidentally import the implementation module it mirrors.
MCP_TOOL_NAMES = frozenset({
    "memoryguard_audit", "memoryguard_explain", "memoryguard_list_sources",
    "memoryguard_scan_summary", "memoryguard_neuron_graph", "memoryguard_import_preview",
    "memoryguard_memory_read", "memoryguard_memory_search", "memoryguard_memory_write",
    "memoryguard_memory_update", "memoryguard_memory_delete", "memoryguard_memory_status",
    "memoryguard_context_bootstrap", "memoryguard_rule_feedback", "memoryguard_rule_create_auto",
    "memoryguard_rule_decision_read", "memoryguard_rule_undo", "memoryguard_rule_scope_stats",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_binding_create", "memoryguard_binding_list", "memoryguard_external_mcp_list",
    "memoryguard_external_mcp_import", "memoryguard_extract_memories", "memoryguard_accept_candidates",
    "memoryguard_semantic_check", "memoryguard_provider_install", "memoryguard_resolve_group",
    "memoryguard_list_pending_enrichments", "memoryguard_apply_enrichments", "memoryguard_enrichment_status",
    "memoryguard_build_and_enrich", "memoryguard_canonical_status", "memoryguard_diagnostics_snapshot",
    "memoryguard_projection_status", "memoryguard_runtime_processes",
    "memoryguard_history_search", "memoryguard_history_timeline", "memoryguard_history_read",
    "memoryguard_history_extract_preview", "memoryguard_history_list_sessions",
    "memoryguard_history_export", "memoryguard_history_delete",
    "memoryguard_knowledge_list", "memoryguard_knowledge_search", "memoryguard_knowledge_read",
    "memoryguard_knowledge_book", "memoryguard_knowledge_candidates",
})

# SafeBridge exposes only these methods to JavaScript.  GovernanceApi keeps a
# larger method surface; the union below is the stable compatibility registry
# used by the adapter and by clients that cannot import the GUI package.
SAFE_BRIDGE_METHOD_NAMES = frozenset({
    "call_readonly", "request_mutation", "get_api_method_registry",
    "get_sandbox_status", "pick_path",
})
GUI_METHOD_NAMES = frozenset({
    *SAFE_BRIDGE_METHOD_NAMES,
    "get_audit", "run_audit", "generate_plan", "discover_agents", "get_selection_tree", "get_agent_data",
    "get_neuron_graph", "get_projection_source_map", "get_governance_scope", "get_governance_scope_state",
    "list_native_memory_releases", "list_publish_targets", "choose_publish_target_path", "list_sources",
    "preview_source", "scan_sources", "get_raw_memory", "get_source_file_content", "list_agent_candidates",
    "list_archived_agents", "list_cleanup_history", "list_agents", "get_residual_cleanup", "open_agent_folder",
    "list_bindings", "check_binding_drift", "get_shared_group_preview", "get_host_hook_status",
    "list_external_mcp_servers", "preview_external_mcp_import", "detect_external_mcp", "list_memory",
    "get_memory", "search_memory", "list_memory_versions", "get_recent_events", "get_auto_actions",
    "get_supersede_chain", "get_conflicts", "get_quarantine", "get_governance_snapshot", "get_memory_status",
    "get_supersede_decisions", "list_share_groups", "get_global_memory_status", "get_memory_source_map",
    "extract_preview", "extract_preview_by_path", "preview_import", "get_memory_ir", "list_releases",
    "list_history", "plan_memoryguard_gc", "get_storage_overview", "get_request_status", "list_pending_requests",
    "list_pending_enrichments", "get_enrichment_status", "get_host_enrichment_guide", "get_build_progress",
    "list_host_llm_agents", "list_history_sessions", "search_history", "history_timeline", "history_read",
    "history_extract_preview", "export_history", "discover_local_history_sources", "list_rules_habits",
    "get_rule_scope_options", "preview_effective_rules", "list_rule_cockpit", "list_rule_decisions",
    "read_rule_decision", "get_rule_auto_scope_metrics", "list_rule_match_receipts", "list_rule_exceptions",
    "knowledge_list", "knowledge_deleted_list", "knowledge_search", "knowledge_read", "knowledge_book",
    "knowledge_job_status", "knowledge_candidates_list", "knowledge_candidate_targets", "get_sandbox_status",
    "submit_request", "knowledge_add", "knowledge_reingest", "knowledge_rebuild_smart", "knowledge_remove",
    "knowledge_restore", "knowledge_purge_deleted", "knowledge_update_settings", "knowledge_candidate_review",
    "set_governance_scope", "commit_selection", "neuron_decide", "set_projection_source_enabled",
    "build_projection", "start_build_projection", "cancel_build_projection", "delete_projection", "add_source",
    "remove_source", "mark_agent_uninstalled", "unmark_agent_uninstalled", "archive_agent_dir",
    "restore_archived_agent", "delete_archived_agent", "enter_multi_agent_mode", "exit_multi_agent_mode",
    "bind_agent", "bind_agents_to_shared_group", "unbind_agent", "ensure_personal_memory_group",
    "leave_shared_group_to_personal", "dissolve_shared_group", "export_memory_group", "clear_memory_group",
    "archive_memory_group", "install_shared_group_mcp_redirects", "set_host_hook_mode", "uninstall_host_hook",
    "import_native_memories_to_group", "commit_shared_memory_governance", "import_external_mcp_entries",
    "edit_memory", "lock_memory", "unlock_memory", "set_memory_injection_policy", "restore_memory",
    "delete_memory", "rollback_memory", "resolve_conflict", "release_quarantine", "delete_quarantine",
    "accept_candidates", "create_import", "apply_memoryguard_gc", "apply_plan", "undo_change",
    "apply_enrichments", "delete_history", "backfill_local_history", "update_rule_audience",
    "create_rule_from_text", "undo_rule_decision", "create_child_exception", "create_rule_exception",
    "submit_rule_feedback", "revoke_rule_exception",
    # Retained legacy names; the runtime security layer may reject these
    # native write-back operations, but clients must still receive a stable
    # method-not-available/error envelope rather than an unknown method.
    "publish_reconstructed_memory", "rollback_native_memory_release", "create_build_plan",
    "apply_build", "verify_release", "rollback_release",
})

# Snapshot of ``memoryguard.cli.build_parser()`` subcommands.  Keep adapter
# independent from the CLI implementation; tests compare this snapshot to
# the parser's live choices and fail when either side drifts.
CLI_COMMAND_NAMES = frozenset({
    "audit", "open", "explain", "plan", "apply", "verify", "undo", "source", "scan",
    "import", "provider", "gc", "storage", "gui", "desktop", "hooks", "mcp-status", "doctor", "groups",
})
CLI_ENVELOPE_KEYS = frozenset({"command", "status", "ok", "path", "data", "error", "exit_code", "stdout", "stderr"})

RULE_MUTATION_MCP_NAMES = frozenset({
    "memoryguard_rule_feedback", "memoryguard_rule_create_auto", "memoryguard_rule_undo",
    "memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve",
    "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear",
    "memoryguard_binding_create",
})
# Mirrors mcp_server._MUTATING_TOOLS.  Keep this explicit snapshot local so
# the compatibility layer never imports the running MCP server.  Read-only
# ``context_bootstrap`` is intentionally excluded: it may refresh receipts,
# but its public MCP contract is a read operation.
MCP_MUTATION_NAMES = frozenset({
    "memoryguard_memory_write", "memoryguard_memory_update", "memoryguard_memory_delete",
    "memoryguard_binding_create", "memoryguard_external_mcp_import", "memoryguard_accept_candidates",
    "memoryguard_provider_install", "memoryguard_apply_enrichments", "memoryguard_history_delete",
    *RULE_MUTATION_MCP_NAMES,
})
MUTATING_MCP_TOOL_NAMES = MCP_MUTATION_NAMES
RULE_MUTATION_GUI_NAMES = frozenset({
    "update_rule_audience", "create_rule_from_text", "undo_rule_decision", "create_child_exception",
    "create_rule_exception", "submit_rule_feedback", "revoke_rule_exception", "neuron_decide",
})


@runtime_checkable
class LegacyAdapterPorts(Protocol):
    """Port shape accepted by the adapter (V1 and V2 implementations)."""

    def dispatch(self, surface: str, name: str, args: Mapping[str, Any]) -> Any: ...


@runtime_checkable
class V2AdapterPort(Protocol):
    def status(self, workspace: str) -> Mapping[str, Any] | bool: ...

    def dispatch(self, surface: str, name: str, args: Mapping[str, Any], *, context: Mapping[str, Any] | None = None) -> Any: ...


@runtime_checkable
class V2RuntimeFacadePort(Protocol):
    """Phase6 runtime seam used by MCP and host hooks.

    The concrete facade is intentionally imported by callers at runtime.  A
    Protocol here keeps the compatibility layer independent from the phase6
    package while still making the required context-bearing calls explicit.
    """

    def state_snapshot(self) -> Mapping[str, Any] | str: ...

    def dispatch_mcp(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def bootstrap_hook(
        self,
        event: str,
        payload: Mapping[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def health(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class AdapterEnvelope:
    status: str
    surface: str
    name: str
    path: str
    ok: bool
    data: Any = None
    error: str = ""
    legacy: Any = None
    code: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status, "surface": self.surface, "name": self.name,
            "path": self.path, "ok": self.ok,
        }
        if self.data is not None:
            payload["data"] = sanitize_public_payload(self.data, error_code=self.code or "operation_failed")
        if self.error:
            payload["error"] = self.error
        if self.code:
            payload["code"] = self.code
        if self.legacy is not None:
            payload["legacy"] = sanitize_public_payload(self.legacy, error_code=self.code or "operation_failed")
        return payload


class LegacyV2Adapter:
    """Not-wired adapter preserving old entrypoint names and envelopes."""

    def __init__(
        self,
        v2_port: V2AdapterPort | Any | None = None,
        legacy_port: LegacyAdapterPorts | Any | None = None,
        *,
        workspace: str = "",
        v2_ready: bool | None = None,
    ) -> None:
        self.workspace = str(workspace or "")
        self.v2_port = v2_port
        self.legacy_port = legacy_port
        self._forced_ready = v2_ready

    @staticmethod
    def _as_mapping(result: Any) -> dict[str, Any] | None:
        return dict(result) if isinstance(result, Mapping) else None

    @staticmethod
    def _args_mapping(args: Any) -> dict[str, Any]:
        try:
            if args is None:
                return {}
            if isinstance(args, Mapping):
                return dict(args)
            if isinstance(args, (list, tuple)):
                return {"args": list(args)}
            namespace = vars(args) if hasattr(args, "__dict__") else None
            if isinstance(namespace, dict):
                payload = dict(namespace)
                # argparse stores executable callback in Namespace; it is an
                # implementation detail, not an adapter argument or JSON value.
                payload.pop("func", None)
                return payload
        except Exception as exc:
            raise RuleMutationError("invalid_adapter_arguments") from exc
        raise RuleMutationError("invalid_adapter_arguments")

    @staticmethod
    def _ready_value(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"v2_ready", "v2_active"}
        if isinstance(value, Mapping):
            if value.get("v2_ready") is True or value.get("ready") is True:
                return True
            marker = str(value.get("state", value.get("status", value.get("marker", ""))) or "").strip().casefold()
            return marker in {"v2_ready", "v2_active"}
        return False

    @staticmethod
    def _state_value(value: Any) -> str:
        """Normalize a port readiness marker to the migration state machine."""
        enum_value = getattr(value, "value", None)
        if enum_value is not None and enum_value is not value:
            value = enum_value
        object_state = getattr(value, "state", None)
        if object_state is not None and object_state is not value:
            return LegacyV2Adapter._state_value(object_state)
        if isinstance(value, bool):
            return "V2_ACTIVE" if value else "V2_NOT_READY"
        if isinstance(value, Mapping):
            # Explicit state wins over convenience booleans.  A status object
            # from a coordinator may expose either ``state`` or ``status``.
            marker = value.get("state", value.get("status", value.get("marker", "")))
            if marker:
                return LegacyV2Adapter._state_value(marker)
            if value.get("v2_ready") is True or value.get("ready") is True:
                return "V2_READY"
            return "V2_NOT_READY"
        marker = str(value or "").strip().upper()
        if marker in {"V2_ACTIVE", "V2_READY", "V2_NOT_READY", "V1_ACTIVE", "V2_BUILDING"}:
            return marker
        if marker in {"READY", "ACTIVE"}:
            return "V2_READY" if marker == "READY" else "V2_ACTIVE"
        if marker in {"NOT_READY", "NOT-READY", "BLOCKED"}:
            return "V2_NOT_READY"
        return "UNKNOWN"

    def v2_state(self) -> str:
        """Return readiness state without collapsing READY and ACTIVE.

        ``V2_READY`` is readable in this adapter contract; rule mutations
        require ``V2_ACTIVE``.  Neither state ever falls back from an active
        V2 port to a legacy write/read path.
        """
        if self._forced_ready is not None:
            return self._state_value(self._forced_ready)
        port = self.v2_port
        if port is None:
            # Constructor with no V2 port is an explicit legacy-only adapter;
            # retain the old compatibility path without treating an unknown
            # manifest returned by a V2 port as legacy-ready.
            return "V1_ACTIVE"
        try:
            value = getattr(port, "state", None)
            if value is None:
                value = getattr(port, "ready", None)
            if value is not None:
                return self._state_value(value)
            status = getattr(port, "status", None)
            if not callable(status):
                return "UNKNOWN"
            return self._state_value(status(self.workspace))
        except Exception:
            return "UNKNOWN"

    def _v2_can_route(self, *, mutation: bool = False, state: str | None = None) -> bool:
        state = state or self.v2_state()
        return state == "V2_ACTIVE" or (state == "V2_READY" and not mutation)

    def is_v2_ready(self) -> bool:
        return self.v2_state() in {"V2_READY", "V2_ACTIVE"}

    def _invoke_legacy(self, surface: str, name: str, args: Mapping[str, Any]) -> Any:
        if self.legacy_port is None:
            return None
        fn = getattr(self.legacy_port, "dispatch", None) or getattr(self.legacy_port, "call", None)
        if not callable(fn):
            raise RuleMutationError("legacy_adapter_port_missing_dispatch")
        # Legacy ports existed in both ``dispatch(surface, name, args)`` and
        # ``call(name, args)`` forms.  Bind once from the signature; an
        # implementation TypeError must become one stable error, never a
        # second invocation with a different authority/scope shape.
        return invoke_once(
            fn,
            ((surface, name, dict(args)), (name, dict(args))),
            error_code="legacy_adapter_signature_unavailable",
        )

    def _invoke_v2(self, surface: str, name: str, args: Mapping[str, Any], *, context: Any = None) -> Any:
        if self.v2_port is None:
            raise RuleMutationError("v2_not_ready")
        fn = getattr(self.v2_port, "dispatch", None) or getattr(self.v2_port, "call", None)
        if not callable(fn):
            method_name = "mutate" if context is not None else "read"
            fn = getattr(self.v2_port, method_name, None)
            if not callable(fn):
                raise TypeError("v2_adapter_port_missing_dispatch")
            if context is not None and not self._supports_context(fn):
                raise RuleMutationError("v2_context_capability_required")
            kwargs = {"workspace": self.workspace}
            if context is not None:
                kwargs["context"] = context.to_dict()
            # Never retry without context after a TypeError.  A retry could
            # turn a governed mutation into an untrusted write.
            return fn(name, dict(args), **kwargs)
        if context is not None and not self._supports_context(fn):
            raise RuleMutationError("v2_context_capability_required")
        kwargs = {"context": context.to_dict()} if context is not None else {}
        # Explicit capability/signature negotiation above; no TypeError retry
        # that silently strips context.
        return fn(surface, name, dict(args), **kwargs)

    def _supports_context(self, fn: Any) -> bool:
        """Check V2 port capability before passing a trusted context.

        Ports may opt in explicitly with ``supports_rule_mutation_context``;
        otherwise inspect their callable signature.  Unknown capability is a
        hard failure, never a context-dropping retry.
        """
        advertised = getattr(self.v2_port, "supports_rule_mutation_context", None)
        if advertised is False:
            return False
        if advertised is True:
            return True
        try:
            parameters = inspect.signature(fn).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(parameter.name == "context" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)

    @staticmethod
    def _coerce_context(context: RuleMutationContext | Mapping[str, Any] | None) -> RuleMutationContext:
        """Normalize untrusted adapter input to one stable error domain."""
        try:
            return RuleMutationContext.from_mapping(context)  # type: ignore[arg-type]
        except RuleMutationError:
            raise
        except Exception as exc:
            raise RuleAuthorizationError("invalid_rule_mutation_context") from exc

    @staticmethod
    def _result(status: str, surface: str, name: str, path: str, result: Any, *, code: str = "") -> dict[str, Any]:
        mapped = dict(result) if isinstance(result, Mapping) else None
        if mapped is not None:
            err = safe_error_code(mapped.get("error", ""), code or "operation_failed") if mapped.get("error") else ""
            ok = bool(mapped.get("ok", not bool(err))) and not err
            payload = sanitize_public_payload(dict(mapped), error_code=code or "operation_failed")
            if err:
                payload["error"] = err
            payload.setdefault("status", status)
            payload.setdefault("surface", surface)
            payload.setdefault("name", name)
            payload.setdefault("path", path)
            payload.setdefault("ok", ok)
            if code:
                payload.setdefault("code", code)
            return payload
        return AdapterEnvelope(status=status, surface=surface, name=name, path=path, ok=status == "ok", data=result, code=code).to_dict()

    def _dispatch(self, surface: str, name: str, args: Mapping[str, Any] | None = None, *, mutation: bool = False, context: RuleMutationContext | Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            payload = self._args_mapping(args)
        except RuleMutationError as exc:
            code = safe_error_code(exc, "invalid_adapter_arguments")
            return self._result(
                "error", surface, name, "none",
                {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                code=code,
            )
        is_rule_mutation = mutation and (name in RULE_MUTATION_MCP_NAMES or name in RULE_MUTATION_GUI_NAMES)
        state = self.v2_state()
        if mutation and state == "V2_READY":
            return self._result(
                "error", surface, name, "v2", {"error": "v2_not_active", "code": "v2_not_active"}, code="v2_not_active",
            )
        ctx: RuleMutationContext | None = None
        if is_rule_mutation:
            if context is None:
                return self._result(
                    "error", surface, name, "v2" if state in {"V2_READY", "V2_ACTIVE"} else "legacy",
                    {"error": "rule_mutation_context_required", "code": "rule_mutation_context_required"},
                    code="rule_mutation_context_required",
                )
            try:
                ctx = self._coerce_context(context)
                ctx.validate_mutation(payload)
            except RuleMutationError as exc:
                code = safe_error_code(exc, "invalid_rule_mutation_context")
                return self._result(
                    "error", surface, name, "v2" if state in {"V2_READY", "V2_ACTIVE"} else "legacy",
                    {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                    code=code,
                )
            except Exception:
                return self._result("error", surface, name, "v2" if state in {"V2_READY", "V2_ACTIVE"} else "legacy", {"error": "invalid_rule_mutation_context", "code": "invalid_rule_mutation_context"}, code="invalid_rule_mutation_context")
        # Non-rule writes still carry the trusted transport context.  The
        # payload is never treated as an identity/authorization source.
        v2_context: Any = (ctx if is_rule_mutation else context) if mutation else None
        if self._v2_can_route(mutation=mutation, state=state):
            try:
                result = self._invoke_v2(surface, name, payload, context=v2_context)
                return self._result("ok", surface, name, "v2", result)
            except RuleMutationError as exc:
                code = safe_error_code(exc, "v2_error")
                return self._result(
                    "error", surface, name, "v2",
                    {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                    code=code,
                )
            except Exception as exc:
                return self._result(
                    "error", surface, name, "v2",
                    {"error": "v2_error", "diagnostic": safe_exception_diagnostic(exc, code="v2_error")},
                    code="v2_error",
                )

        # Explicit legacy states only: one and only one legacy dispatch. Rule writes
        # still require a trusted context and cannot infer broad/other-agent
        # scope; V2_READY mutations were rejected above.
        # Only explicit V1_ACTIVE/V2_BUILDING may invoke the legacy route.
        # V2_NOT_READY is an unavailable/ambiguous manifest, not permission
        # to queue a write (or return a misleading deferred success).
        if state not in {"V1_ACTIVE", "V2_BUILDING"}:
            return self._result(
                "error", surface, name, "none",
                {"error": "v2_manifest_state_unavailable", "code": "v2_manifest_state_unavailable", "state": state},
                code="v2_manifest_state_unavailable",
            )
        if state == "V2_NOT_READY":
            return self._result(
                "error", surface, name, "none",
                {"error": "v2_not_ready", "code": "v2_not_ready"}, code="v2_not_ready",
            )
        if is_rule_mutation:
            # Context was normalized and fully authorized above.
            assert ctx is not None
        try:
            legacy = self._invoke_legacy(surface, name, payload)
        except Exception as exc:
            code = safe_error_code(exc, "legacy_error")
            legacy = {
                "error": code,
                "code": code,
                "diagnostic": safe_exception_diagnostic(exc, code=code),
            }
        return AdapterEnvelope(status="not_ready", surface=surface, name=name, path="legacy", ok=False, legacy=legacy, code="v2_not_ready").to_dict()

    def dispatch_mcp(
        self,
        name: str,
        args: Any = None,
        *,
        context: RuleMutationContext | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if name not in MCP_TOOL_NAMES:
            return self._result("error", "mcp", name, "none", {"error": "unknown_tool", "code": "unknown_tool"}, code="unknown_tool")
        return self._dispatch("mcp", name, args, mutation=name in MCP_MUTATION_NAMES, context=context)

    call_mcp = dispatch_mcp
    mcp = dispatch_mcp
    handle_mcp = dispatch_mcp

    def dispatch(self, surface: str, name: str, args: Any = None, *, context: RuleMutationContext | Mapping[str, Any] | None = None, mutation: bool = False) -> dict[str, Any]:
        key = str(surface or "").casefold()
        if key == "mcp":
            return self.dispatch_mcp(name, args, context=context)
        if key == "gui":
            return self.dispatch_gui(name, args, mutation=mutation, context=context)
        if key == "cli":
            # Keep argparse.Namespace/custom argument objects intact until
            # dispatch_cli performs its safe normalization.  Converting
            # non-Mappings to {} silently dropped sub-actions and flags.
            return self.dispatch_cli(name, args, mutation=mutation, context=context)
        return self._result("error", key or "unknown", name, "none", {"error": "unknown_surface", "code": "unknown_surface"}, code="unknown_surface")

    def dispatch_gui(self, method: str, args: list[Any] | tuple[Any, ...] | Mapping[str, Any] | None = None, *, mutation: bool = False, context: RuleMutationContext | Mapping[str, Any] | None = None) -> dict[str, Any]:
        if method not in GUI_METHOD_NAMES:
            return self._result("error", "gui", method, "none", {"error": "unknown_gui_method", "code": "unknown_gui_method"}, code="unknown_gui_method")
        if isinstance(args, Mapping):
            payload = dict(args)
        else:
            positional = list(args or [])
            payload = {"args": positional}
            # GUI callers pass positional arguments.  Preserve that shape but
            # surface an explicit scope object for the fail-closed rule gate.
            for item in positional:
                if isinstance(item, Mapping) and ({"target_type", "type", "target_id", "id"} & set(item)):
                    payload.setdefault("scope", dict(item))
                    break
        try:
            from ..security import is_mutation_method
            effective_mutation = bool(
                mutation or is_mutation_method(method) or method in GUI_MUTATION_NAMES
            )
        except Exception:
            effective_mutation = bool(mutation or method in GUI_MUTATION_NAMES)
        return self._dispatch("gui", method, payload, mutation=effective_mutation, context=context)

    def call_readonly(self, method: str, args: list[Any] | None = None) -> dict[str, Any]:
        if method not in GUI_METHOD_NAMES - GUI_MUTATION_NAMES - {"call_readonly", "request_mutation"}:
            return {"error": f"not a readonly method: {method}", "code": "not_readonly"}
        return self.dispatch_gui(method, args, mutation=False)

    def request_mutation(self, method: str, args: list[Any] | None = None, *, context: RuleMutationContext | Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_gui(method, args, mutation=True, context=context)

    gui_call = dispatch_gui
    handle_gui = dispatch_gui

    @staticmethod
    def _cli_is_mutation(command: str, args: Mapping[str, Any] | Any) -> bool:
        """Classify CLI writes from command and parser sub-action."""
        command = str(command or "").casefold()
        try:
            payload = LegacyV2Adapter._args_mapping(args)
        except RuleMutationError:
            return False
        # ``argparse`` emits real booleans for these switches.  A JSON/RPC
        # caller must not smuggle the strings ``"true"``/``"false"`` (or any
        # other string) through the mutation classifier; treating one as a
        # falsey dry-run would silently downgrade a write to a read.
        boolean_keys = {
            "apply", "dry_run", "archive_source", "auto_confirm", "watch",
            "register_uri", "force", "yes", "confirmed",
        }
        for key in boolean_keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if value is None or type(value) is bool:
                continue
            if type(value) is int and value in (0, 1):
                continue
            raise RuleMutationError(f"invalid_cli_boolean:{key}")
        if command in {"apply", "undo"}:
            return True
        values: list[str] = []
        for key in ("action", "subcommand", "operation", "mode", "command"):
            value = payload.get(key)
            if isinstance(value, str):
                values.append(value.casefold())
        raw_args = payload.get("args")
        if isinstance(raw_args, (list, tuple)):
            values.extend(str(item).casefold() for item in raw_args if isinstance(item, str))
        if command == "groups":
            if "action" not in payload and "subcommand" not in payload and "operation" not in payload:
                return True
            if LegacyV2Adapter._flag(payload.get("apply")):
                return True
            # ``groups migrate`` is a dry-run by default; explicit
            # --dry-run is also read-only.  Other administrative sub-actions
            # mutate immediately.
            if "migrate" in values:
                return False
            if any(value in {"list"} for value in values):
                return False
            return True
        if command == "source":
            if not values:
                return True
            if any(value in {"list", "preview"} for value in values):
                return False
            return True
        if command == "import":
            if not values:
                return True
            if "preview" in values:
                return False
            return True
        if command == "provider":
            if not values:
                return True
            return True
        if command == "hooks":
            if not values:
                return True
            if "status" in values:
                return False
            return True
        if command == "gc":
            if "apply" in payload:
                value = payload.get("apply")
                if LegacyV2Adapter._flag(value):
                    return True
                if value not in (None, False, 0, "0"):
                    return True
            return "apply" in values
        if command == "storage":
            if not values:
                return True
            if any(value in {"audit", "report"} for value in values):
                return False
            if "compact" in values:
                value = payload.get("apply")
                return LegacyV2Adapter._flag(value) if value is not None else "apply" in values
            # Lease management and a sweep audit both persist maintenance
            # ledger state, even when the latter performs no physical delete.
            return True
        if command == "desktop":
            # Desktop executor can process requests, watch queues, or register
            # a URI handler.  No stable read-only desktop action exists in the
            # parser, so conservatively gate all desktop invocations.
            return True
        return False

    @staticmethod
    def _flag(value: Any) -> bool:
        if type(value) is bool:
            return value
        if type(value) is int and value in (0, 1):
            return value == 1
        return False

    def dispatch_cli(self, command: str, args: Mapping[str, Any] | Any = None, *, mutation: bool = False, context: RuleMutationContext | Mapping[str, Any] | None = None) -> dict[str, Any]:
        if command not in CLI_COMMAND_NAMES:
            return self._result("error", "cli", command, "none", {"error": "unknown_cli_command", "code": "unknown_cli_command"}, code="unknown_cli_command")
        try:
            payload = self._args_mapping(args)
        except RuleMutationError as exc:
            code = safe_error_code(exc, "invalid_adapter_arguments")
            return self._result(
                "error", "cli", command, "none",
                {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                code=code,
            )
        try:
            classified_mutation = self._cli_is_mutation(command, payload)
            effective_mutation = bool(mutation or classified_mutation)
        except RuleMutationError as exc:
            code = safe_error_code(exc, "invalid_cli_arguments")
            return self._result(
                "error", "cli", command, "none",
                {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)}, code=code,
            )
        result = self._dispatch(
            "cli", command, payload,
            mutation=effective_mutation,
            context=context,
        )
        result.setdefault("command", command)
        return result

    handle_cli = dispatch_cli

    def hooks(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_cli("hooks", args)

    def mcp_status(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_cli("mcp-status", args)

    def doctor(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_cli("doctor", args)

    def groups(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.dispatch_cli("groups", args)

    def mutate_rule(self, operation: str, payload: Mapping[str, Any], context: RuleMutationContext | Mapping[str, Any]) -> dict[str, Any]:
        """Explicit V2 rule write contract; audit fields are always required."""
        try:
            ctx = self._coerce_context(context)
            normalized = ctx.validate_mutation(payload)
        except RuleMutationError as exc:
            code = safe_error_code(exc, "invalid_rule_mutation_context")
            return self._result(
                "error", "rule", operation, "none",
                {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                code=code,
            )
        except Exception:
            # Adapter boundary must never leak TypeError/AttributeError from
            # malformed context or payload objects.
            return self._result(
                "error", "rule", operation, "none",
                {"error": "invalid_rule_mutation_context", "code": "invalid_rule_mutation_context"},
                code="invalid_rule_mutation_context",
            )
        state = self.v2_state()
        if state == "V2_READY":
            return self._result(
                "error", "rule", operation, "v2",
                {"error": "v2_not_active", "code": "v2_not_active"}, code="v2_not_active",
            )
        if state not in {"V1_ACTIVE", "V2_BUILDING", "V2_ACTIVE"}:
            return self._result(
                "error", "rule", operation, "none",
                {"error": "v2_manifest_state_unavailable", "code": "v2_manifest_state_unavailable", "state": state},
                code="v2_manifest_state_unavailable",
            )
        if state in {"V1_ACTIVE", "V2_BUILDING"}:
            legacy = self._invoke_legacy("rule", operation, normalized) if self.legacy_port is not None else None
            return AdapterEnvelope(status="not_ready", surface="rule", name=operation, path="legacy", ok=False, legacy=legacy, code="v2_not_ready").to_dict()
        try:
            result = self._invoke_v2("rule", operation, normalized, context=ctx)
            return self._result("ok", "rule", operation, "v2", result)
        except RuleMutationError as exc:
            code = safe_error_code(exc, "v2_error")
            return self._result(
                "error", "rule", operation, "v2",
                {"error": code, "code": code, "diagnostic": safe_exception_diagnostic(exc, code=code)},
                code=code,
            )
        except Exception as exc:
            return self._result(
                "error", "rule", operation, "v2",
                {"error": "v2_error", "diagnostic": safe_exception_diagnostic(exc, code="v2_error")},
                code="v2_error",
            )


LegacyAdapter = LegacyV2Adapter
V2LegacyAdapter = LegacyV2Adapter
LegacyAPIAdapter = LegacyV2Adapter
LegacyAdapterPort = LegacyAdapterPorts
LegacyPort = LegacyAdapterPorts
V2Port = V2AdapterPort
# Re-export the pure contract snapshots.  Keeping these assignments at the
# boundary lets older imports continue to work while native V2 code depends
# only on ``cutover_v2.surfaces`` and never imports this adapter module.
MCP_TOOL_NAMES = _SURFACE_MCP_TOOL_NAMES
LEGACY_MCP_TOOL_NAMES = _SURFACE_LEGACY_MCP_TOOL_NAMES
SAFE_BRIDGE_METHOD_NAMES = _SURFACE_SAFE_BRIDGE_METHOD_NAMES
GUI_METHOD_NAMES = _SURFACE_GUI_METHOD_NAMES
LEGACY_GUI_METHOD_NAMES = _SURFACE_LEGACY_GUI_METHOD_NAMES
CLI_COMMAND_NAMES = _SURFACE_CLI_COMMAND_NAMES
LEGACY_CLI_COMMAND_NAMES = _SURFACE_LEGACY_CLI_COMMAND_NAMES
RULE_MUTATION_MCP_NAMES = _SURFACE_RULE_MUTATION_MCP_NAMES
MCP_MUTATION_NAMES = _SURFACE_MCP_MUTATION_NAMES
MUTATING_MCP_TOOL_NAMES = _SURFACE_MUTATING_MCP_TOOL_NAMES
RULE_MUTATION_GUI_NAMES = _SURFACE_RULE_MUTATION_GUI_NAMES
GUI_MUTATION_NAMES = _SURFACE_GUI_MUTATION_NAMES
MUTATING_GUI_METHOD_NAMES = _SURFACE_MUTATING_GUI_METHOD_NAMES
GUI_MUTATION_METHOD_NAMES = _SURFACE_GUI_MUTATION_METHOD_NAMES
LEGACY_MCP_TOOL_NAMES = MCP_TOOL_NAMES
LEGACY_GUI_METHOD_NAMES = GUI_METHOD_NAMES
LEGACY_CLI_COMMAND_NAMES = CLI_COMMAND_NAMES

__all__ = [
    "AdapterEnvelope", "LegacyAdapterPorts", "LegacyAdapterPort", "LegacyPort", "V2AdapterPort", "V2Port", "V2RuntimeFacadePort", "LegacyV2Adapter", "V2LegacyAdapter", "LegacyAPIAdapter", "LegacyAdapter",
    "MCP_TOOL_NAMES", "LEGACY_MCP_TOOL_NAMES", "GUI_METHOD_NAMES", "LEGACY_GUI_METHOD_NAMES", "SAFE_BRIDGE_METHOD_NAMES", "CLI_COMMAND_NAMES", "LEGACY_CLI_COMMAND_NAMES",
    "CLI_ENVELOPE_KEYS", "RULE_MUTATION_MCP_NAMES", "RULE_MUTATION_GUI_NAMES", "MCP_MUTATION_NAMES", "MUTATING_MCP_TOOL_NAMES",
    "GUI_MUTATION_NAMES", "MUTATING_GUI_METHOD_NAMES", "GUI_MUTATION_METHOD_NAMES",
]
