"""Gated V2 runtime facade and single-route cutover dispatcher."""

from __future__ import annotations

from collections import Counter
import hashlib
import inspect
import re
import threading
from typing import Any, Mapping

from .ports import RuntimePorts
from .readiness import ReadinessGate
from .state import CutoverState, RuntimeSnapshot, snapshot_from_port
from .surfaces import (
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    GUI_MUTATION_NAMES,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
)


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
    digest = hashlib.sha256(
        f"{typename}\x00{str(exc)}".encode("utf-8", "replace"),
    ).hexdigest()[:16]
    return {"type": typename, "hash": digest, "code": safe_error_code(code)}


def sanitize_public_payload(value: Any, *, error_code: str = "operation_failed") -> Any:
    """Redact public error/path fields while preserving safe data."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _ERROR_KEYS:
                if lowered == "error":
                    # Successful protocol packets use an explicit empty error
                    # value.  It is absence of an error, not malformed error
                    # text that should be replaced by the failure fallback.
                    output[key] = raw_value if not raw_value else safe_error_code(raw_value, error_code)
                continue
            if lowered == "code":
                output[key] = raw_value if not raw_value else safe_error_code(raw_value, error_code)
                continue
            if lowered in _PATH_KEYS:
                output[key] = "<redacted>"
                continue
            if lowered == "path" and isinstance(raw_value, str):
                if raw_value.startswith(("/", "\\")) or (len(raw_value) > 2 and raw_value[1] == ":"):
                    output[key] = "<redacted>"
                    continue
            output[key] = sanitize_public_payload(raw_value, error_code=error_code)
        return output
    if isinstance(value, (list, tuple)):
        return [sanitize_public_payload(item, error_code=error_code) for item in value]
    return value


def _args_mapping(args: Any) -> dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, Mapping):
        return dict(args)
    if isinstance(args, (list, tuple)):
        return {"args": list(args)}
    namespace = vars(args) if hasattr(args, "__dict__") else None
    if isinstance(namespace, dict):
        payload = dict(namespace)
        payload.pop("func", None)
        return payload
    raise ValueError("invalid_cli_arguments")


def _flag(value: Any) -> bool:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    return False


def _cli_is_mutation(command: str, args: Any) -> bool:
    """Classify CLI writes from command and parser sub-action."""

    command = str(command or "").casefold()
    try:
        payload = _args_mapping(args)
    except Exception:
        return True
    boolean_keys = {
        "apply", "dry_run", "archive_source", "auto_confirm", "watch",
        "register_uri", "force", "yes", "confirmed",
    }
    for key in boolean_keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None or type(value) is bool or (type(value) is int and value in (0, 1)):
            continue
        return True
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
        if not any(key in payload for key in ("action", "subcommand", "operation")):
            return True
        if _flag(payload.get("apply")):
            return True
        if "migrate" in values or "list" in values:
            return False
        return True
    if command == "source":
        return not values or not any(value in {"list", "preview"} for value in values)
    if command == "import":
        return not values or "preview" not in values
    if command == "provider":
        return True
    if command == "hooks":
        return not values or "status" not in values
    if command == "gc":
        if "apply" in payload:
            value = payload.get("apply")
            if _flag(value):
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
            return _flag(value) if value is not None else "apply" in values
        return True
    if command == "desktop":
        return True
    return False


def _safe_dict(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        try:
            return method()
        except Exception:
            return value
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class _GenerationPort:
    """Attach one request's expected generation without retrying calls."""

    def __init__(self, port: Any, *, generation: int, state: Any = None, facade: "V2RuntimeFacade") -> None:
        self.port = port
        self.generation = generation
        self.state = state
        self.facade = facade
        self.supports_rule_mutation_context = getattr(port, "supports_rule_mutation_context", None)

    def dispatch(self, surface: str, name: str, args: Mapping[str, Any], **kwargs: Any) -> Any:
        payload = dict(kwargs)
        payload.setdefault("generation", self.generation)
        if self.state is not None:
            payload.setdefault("state", self.state)
        return self.facade._invoke_port(self.port, surface, name, args, **payload)

    def call(self, surface: str, name: str, args: Mapping[str, Any], **kwargs: Any) -> Any:
        return self.dispatch(surface, name, args, **kwargs)


class V2RuntimeFacade:
    """Runtime entrypoint enforcing one state snapshot and one route per call.

    ``manifest`` and ``v2`` are explicit ports. Unknown constructor aliases
    are ignored so an untrusted caller cannot inject another route.
    """

    def __init__(
        self,
        manifest: Any = None,
        v2: Any = None,
        *,
        ports: RuntimePorts | Mapping[str, Any] | Any | None = None,
        workspace: str = "",
        readiness: Any = None,
        context_engine: Any = None,
        recall_planner: Any = None,
        hook_v2: Any = None,
        readiness_gate: Any = None,
        manifest_store: Any = None,
        v2_port: Any = None,
        **aliases: Any,
    ) -> None:
        manifest = manifest if manifest is not None else manifest_store
        v2 = v2 if v2 is not None else v2_port
        if manifest is None:
            manifest = aliases.pop("system_manifest", None)
        if readiness_gate is None:
            readiness_gate = aliases.pop("readiness_port", None)
        overrides = {
            "manifest": manifest,
            "v2": v2,
            "readiness": readiness_gate or readiness,
            "context_engine": context_engine,
            "recall_planner": recall_planner,
            "hook_v2": hook_v2,
        }
        overrides = {key: value for key, value in overrides.items() if value is not None}
        if ports is not None:
            self.ports = RuntimePorts.from_value(ports, **overrides)
        else:
            self.ports = RuntimePorts.from_value(overrides)
        # A native port used behind this facade gets the facade's trusted
        # manifest provider.  This binding is intentionally performed only at
        # the cutover boundary; a direct NativeV2RuntimePort caller still has
        # to inject its own provider and cannot self-assert V2_ACTIVE.
        native = self.ports.v2
        if (
            native is not None
            and getattr(native, "requires_state_provider", False)
            and getattr(native, "state_provider", None) is None
        ):
            try:
                native.state_provider = self.ports.manifest
            except Exception:
                pass
        self.workspace = str(workspace or "")
        self.gate = self.ports.readiness if isinstance(self.ports.readiness, ReadinessGate) else (self.ports.readiness or ReadinessGate(manifest=self.ports.manifest))
        self.context_engine = self.ports.context_engine
        self.recall_planner = self.ports.recall_planner
        self._lock = threading.RLock()
        self._metrics: Counter[str] = Counter()

    # ---- state and diagnostics -------------------------------------------------
    def _snapshot(self) -> RuntimeSnapshot:
        return snapshot_from_port(self.ports.manifest)

    def state_snapshot(self, workspace: Any = None) -> RuntimeSnapshot:
        """Capture one immutable manifest/CAS generation snapshot.

        ``workspace`` is accepted for compatibility with transport ports but
        never changes the injected manifest identity.
        """

        del workspace
        return self._snapshot()

    @staticmethod
    def _base(snapshot: RuntimeSnapshot, *, path: str = "none", status: str = "error", ok: bool = False, code: str = "") -> dict[str, Any]:
        result = {
            "ok": bool(ok),
            "status": status,
            "path": path,
            "state": snapshot.state.value,
            "generation": snapshot.generation,
        }
        if code:
            result["code"] = code
            result["error"] = code
        return result

    def status(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        result = snapshot.to_dict()
        result["ok"] = snapshot.available
        result["status"] = "ok" if snapshot.available else "error"
        return result

    def readiness(self, evidence: Any = None) -> dict[str, Any]:
        snapshot = self._snapshot()
        try:
            result = self.gate.evaluate(evidence, manifest=snapshot.raw)
            payload = result.to_dict() if hasattr(result, "to_dict") else _safe_dict(result)
        except Exception:
            payload = {"ready": False, "ok": False, "status": "BLOCKED", "failures": ["readiness_unavailable"]}
        payload = dict(payload or {})
        payload["manifest_state"] = snapshot.state.value
        payload["generation"] = snapshot.generation
        if not snapshot.available:
            payload["ready"] = payload["ok"] = False
            payload.setdefault("failures", []).append("manifest_unavailable")
        return payload

    def health(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        with self._lock:
            metrics = dict(self._metrics)
        payload = {
            "ok": snapshot.available and snapshot.state is not CutoverState.UNKNOWN,
            "state": snapshot.state.value,
            "generation": snapshot.generation,
            "available": snapshot.available,
            "metrics": metrics,
        }
        if not snapshot.available:
            payload["error"] = snapshot.error or "v2_manifest_state_unavailable"
        return payload

    # ---- routing ---------------------------------------------------------------
    @staticmethod
    def _mutation(surface: str, name: str, args: Any, explicit: bool | None) -> bool:
        """Classify writes without allowing a caller to downgrade a known one.

        The transport boundary is security-sensitive: explicit ``mutation``
        flags are only hints.  Registered write names always remain writes and
        unknown names are conservatively gated as writes.
        """
        if surface == "mcp":
            return name not in MCP_TOOL_NAMES or name in MCP_MUTATION_NAMES or bool(explicit)
        if surface == "gui":
            return name not in GUI_METHOD_NAMES or name in GUI_MUTATION_NAMES or bool(explicit)
        if surface == "cli":
            if name not in CLI_COMMAND_NAMES:
                return True
            return bool(explicit) or _cli_is_mutation(name, args)
        return True if explicit is None else bool(explicit)

    @staticmethod
    def _invoke_port(port: Any, surface: str, name: str, args: Any, **kwargs: Any) -> Any:
        if port is None:
            raise RuntimeError("runtime_port_unavailable")
        fn = getattr(port, "dispatch", None) or getattr(port, "call", None)
        generic = callable(fn)
        if not generic:
            aliases = {
                "mcp": ("dispatch_mcp", "mcp"),
                "gui": ("dispatch_gui", "gui_call"),
                "cli": ("dispatch_cli", "cli"),
                "hook": ("bootstrap_hook", "bootstrap"),
            }
            for candidate in aliases.get(surface, ()):
                fn = getattr(port, candidate, None)
                if callable(fn):
                    break
        if not callable(fn):
            raise RuntimeError("runtime_port_missing_dispatch")
        try:
            parameters = inspect.signature(fn).parameters
            accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
            accepted = {key: value for key, value in kwargs.items() if key in parameters or accepts_kwargs}
        except (TypeError, ValueError):
            if "generation" in kwargs or "context" in kwargs:
                raise RuntimeError("runtime_port_signature_unavailable")
            accepted = {}
        # Generic dispatch ports receive surface/name/args; surface-specific
        # ports receive only name/args.  Shape is selected before invocation,
        # so an invocation TypeError is never retried with dropped context.
        if generic:
            return fn(surface, name, args, **accepted)
        return fn(name, args, **accepted)

    @staticmethod
    def _supports_context(port: Any) -> bool:
        advertised = getattr(port, "supports_rule_mutation_context", None)
        if advertised is False:
            return False
        candidates = (
            "dispatch", "call", "dispatch_mcp", "mcp", "dispatch_gui", "gui_call",
            "dispatch_cli", "cli", "bootstrap_hook", "bootstrap",
        )
        for name in candidates:
            fn = getattr(port, name, None)
            if not callable(fn):
                continue
            try:
                parameters = inspect.signature(fn).parameters.values()
            except (TypeError, ValueError):
                continue
            if any(item.name == "context" or item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters):
                return True
        # An affirmative marker without a context-bearing callable is not a
        # capability; treating it as one would silently drop trusted context.
        return False

    def _envelope(self, result: Any, snapshot: RuntimeSnapshot, *, path: str, status: str, code: str = "") -> dict[str, Any]:
        raw_result = dict(result) if isinstance(result, Mapping) else {"data": _safe_dict(result)}
        # TaskCoordinator includes an empty error mapping in successful task
        # status payloads.  It is absence of an error, not a diagnostic to
        # sanitize into the fallback ``runtime_dispatch_failed`` code.
        if not raw_result.get("error"):
            raw_result.pop("error", None)
        payload = sanitize_public_payload(
            raw_result,
            error_code=code or "runtime_dispatch_failed",
        )
        payload.setdefault("ok", status == "ok")
        payload.setdefault("status", status)
        payload.setdefault("path", path)
        payload.setdefault("state", snapshot.state.value)
        payload.setdefault("generation", snapshot.generation)
        if code:
            payload.setdefault("code", code)
            payload.setdefault("error", code)
        return payload

    def _dispatch(self, surface: str, name: str, args: Any = None, *, mutation: bool | None = None, context: Any = None, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
        if snapshot is None:
            snapshot = self._snapshot()  # exactly one state read for this request
        elif not isinstance(snapshot, RuntimeSnapshot) or not snapshot.trusted:
            invalid = RuntimeSnapshot.unavailable("invalid_runtime_snapshot")
            return self._base(invalid, code="invalid_runtime_snapshot")
        with self._lock:
            self._metrics["requests"] += 1
        if not snapshot.available or snapshot.state is CutoverState.UNKNOWN:
            with self._lock:
                self._metrics["unknown"] += 1
            return self._base(snapshot, code="v2_manifest_state_unavailable")
        if snapshot.state in {CutoverState.V1_ACTIVE, CutoverState.V2_BUILDING}:
            return self._base(snapshot, code="v2_upgrade_required")
        is_mutation = self._mutation(surface, name, args, mutation)
        if snapshot.state is CutoverState.V2_READY and is_mutation:
            # READY is a V2 read-only state.  Do not invoke the native port.
            return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_not_active")
        # READY and ACTIVE are the only executable states.  Both use the
        # injected native V2 port and never fall back to another route.
        if snapshot.state in {CutoverState.V2_READY, CutoverState.V2_ACTIVE}:
            if context is not None and not self._supports_context(self.ports.v2):
                return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_context_capability_required")
            try:
                result = self._invoke_port(
                    _GenerationPort(self.ports.v2, generation=snapshot.generation, state=snapshot.state, facade=self),
                    surface, name, args if args is not None else {},
                    context=context,
                    mutation=is_mutation,
                )
                with self._lock:
                    self._metrics["v2_calls"] += 1
                result_status = str(result.get("status", "") or "").strip().casefold() if isinstance(result, Mapping) else ""
                failed = (
                    isinstance(result, Mapping)
                    and (result.get("ok") is False or result_status in {"error", "blocked", "failed"} or result.get("error"))
                )
                return self._envelope(
                    result,
                    snapshot,
                    path="v2",
                    status="error" if failed else "ok",
                    code=(str(result.get("code") or "v2_error") if failed and isinstance(result, Mapping) else ""),
                )
            except Exception:
                return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_error")
        return self._base(snapshot, code="v2_manifest_state_unavailable")

    def dispatch_mcp(self, name: str, args: Any = None, *, context: Any = None, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
        return self._dispatch("mcp", str(name), args, context=context, snapshot=snapshot)

    def dispatch_gui(self, method: str, args: Any = None, *, mutation: bool | None = None, context: Any = None, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
        return self._dispatch("gui", str(method), args, mutation=mutation, context=context, snapshot=snapshot)

    def dispatch_cli(self, command: str, args: Any = None, *, mutation: bool | None = None, context: Any = None, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
        return self._dispatch("cli", str(command), args, mutation=mutation, context=context, snapshot=snapshot)

    # Compatibility aliases used by host adapters.
    dispatch = _dispatch
    call_mcp = dispatch_mcp
    call_gui = dispatch_gui
    call_cli = dispatch_cli

    def bootstrap_hook(self, request: Any = None, payload: Any = None, *, context: Any = None, snapshot: RuntimeSnapshot | None = None) -> dict[str, Any]:
        if snapshot is None:
            snapshot = self._snapshot()
        elif not isinstance(snapshot, RuntimeSnapshot) or not snapshot.trusted:
            invalid = RuntimeSnapshot.unavailable("invalid_runtime_snapshot")
            return self._base(invalid, code="invalid_runtime_snapshot")
        with self._lock:
            self._metrics["requests"] += 1
        if not snapshot.available or snapshot.state is CutoverState.UNKNOWN:
            with self._lock:
                self._metrics["unknown"] += 1
            return self._base(snapshot, code="v2_manifest_state_unavailable")
        if snapshot.state in {CutoverState.V1_ACTIVE, CutoverState.V2_BUILDING}:
            return self._base(snapshot, code="v2_upgrade_required")
        if context is not None:
            context_port = self.ports.hook_v2 or self.ports.v2 or self.context_engine
            if context_port is None or not self._supports_context(context_port):
                return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_context_capability_required")
        port = self.ports.hook_v2 or self.ports.v2
        path = "v2"
        if port is None and self.context_engine is not None:
            port = self.context_engine
        try:
            if port is None:
                raise RuntimeError("runtime_port_unavailable")
            if self.context_engine is port:
                fn = getattr(port, "bootstrap", None) or getattr(port, "build_context", None)
                if not callable(fn):
                    raise RuntimeError("context_engine_missing_bootstrap")
                result = fn(request, payload)
            elif self.ports.hook_v2 is not None and port is self.ports.hook_v2:
                fn = getattr(port, "bootstrap_hook", None)
                result = fn(request, payload, generation=snapshot.generation, state=snapshot.state, context=context) if callable(fn) else self._invoke_port(port, "hook", "bootstrap_hook", {"request": request, "payload": payload}, generation=snapshot.generation, state=snapshot.state, context=context)
            else:
                result = self._invoke_port(port, "hook", "bootstrap_hook", {"request": request, "payload": payload}, generation=snapshot.generation, context=context)
        except Exception:
            return self._envelope({"ok": False}, snapshot, path=path, status="error", code="bootstrap_failed")
        with self._lock:
            self._metrics[f"{path}_calls"] += 1
        result_status = str(result.get("status", "") or "").strip().casefold() if isinstance(result, Mapping) else ""
        failed = (
            isinstance(result, Mapping)
            and (result.get("ok") is False or result_status in {"error", "blocked", "failed"} or result.get("error"))
        )
        return self._envelope(
            result,
            snapshot,
            path=path,
            status="error" if failed else "ok",
            code=(str(result.get("code") or "bootstrap_failed") if failed and isinstance(result, Mapping) else ""),
        )

    bootstrap = bootstrap_hook

    def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Stop workers owned by the selected V2 runtime before host exit."""
        port = self.ports.v2
        fn = getattr(port, "shutdown", None) or getattr(port, "close", None)
        if not callable(fn):
            return {"ok": True, "owned_workers_stopped": True}
        try:
            result = fn(timeout=float(timeout))
        except TypeError:
            result = fn()
        except Exception:
            return {"ok": False, "owned_workers_stopped": False}
        if isinstance(result, Mapping):
            return dict(result)
        return {"ok": True, "owned_workers_stopped": True}

    close = shutdown

    # ---- activation ------------------------------------------------------------
    def activate(self, evidence: Any = None) -> dict[str, Any]:
        snapshot = self._snapshot()
        if not snapshot.available or snapshot.state is not CutoverState.V2_READY:
            return self._base(snapshot, code="activation_requires_v2_ready")
        try:
            result = self.gate.evaluate(evidence, manifest=snapshot.raw)
            activated = self.gate.activate(self.ports.manifest, result, expected_generation=snapshot.generation, snapshot=snapshot)
            return self._envelope(_safe_dict(activated), snapshot, path="v2", status="ok")
        except Exception as exc:
            payload = self._base(snapshot, code="activation_failed")
            payload["diagnostic"] = safe_exception_diagnostic(exc, code="activation_failed")
            return payload

    def rollback(self, *, reason: str = "v2_cutover_rollback") -> dict[str, Any]:
        snapshot = self._snapshot()
        try:
            result = self.gate.rollback(self.ports.manifest, reason=reason, expected_generation=snapshot.generation, snapshot=snapshot)
            return self._envelope(_safe_dict(result), snapshot, path="none", status="ok")
        except Exception as exc:
            payload = self._base(snapshot, code="rollback_failed")
            payload["diagnostic"] = safe_exception_diagnostic(exc, code="rollback_failed")
            return payload


def get_v2_runtime_facade(
    workspace: str,
    *,
    v2_port: Any = None,
    native_port: Any = None,
    sweep_safety_port: Any = None,
    quiescence_verifier: Any = None,
    outbox_verifier: Any = None,
) -> V2RuntimeFacade:
    """Construct the production facade without importing any legacy Store."""

    from ..maintenance_v2.runtime_port import MaintenanceRuntimePort
    from ..runtime_v2.native_ports import NativeV2RuntimePort
    from ..system.manifest import ManifestManager

    manifest = ManifestManager(workspace)
    if v2_port is not None or native_port is not None:
        port = v2_port if v2_port is not None else native_port
    else:
        maintenance = MaintenanceRuntimePort(
            workspace,
            sweep_safety_port=sweep_safety_port,
            quiescence_verifier=quiescence_verifier,
            outbox_verifier=outbox_verifier,
        )
        # The native port is lazy: construction does not create a V2 database
        # and the facade remains the sole manifest/state gate.
        port = NativeV2RuntimePort(
            workspace,
            maintenance_port=maintenance,
            state_provider=manifest,
        )
    # NativeV2RuntimePort constructs the production ContextEngine when no
    # explicit engine was supplied.  Surface that same instance through the
    # facade so bootstrap callers cannot observe a missing context capability.
    context_engine = getattr(port, "context_engine", None)
    return V2RuntimeFacade(
        workspace=workspace,
        manifest=manifest,
        v2=port,
        context_engine=context_engine,
    )


__all__ = ["V2RuntimeFacade", "get_v2_runtime_facade"]
