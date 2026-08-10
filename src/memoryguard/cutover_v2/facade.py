"""Gated V2 runtime facade and single-route cutover dispatcher."""

from __future__ import annotations

from collections import Counter
import inspect
import threading
from typing import Any, Mapping

from ..compat_v2 import LegacyV2Adapter
from ..compat_v2.legacy_adapter import (
    LegacyV2Adapter as _LegacyV2AdapterClassifier,
    safe_error_code,
    safe_exception_diagnostic,
    sanitize_public_payload,
)
from .ports import RuntimePorts
from .readiness import ReadinessGate
from .state import CutoverState, RuntimeSnapshot, snapshot_from_port


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

    def status(self, workspace: str = "") -> Any:
        # LegacyV2Adapter never probes status when v2_ready is forced.  This
        # method exists only for compatibility with an injected adapter.
        method = getattr(self.port, "status", None)
        if callable(method):
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                return None
            # Choose the call shape from the signature before invocation.  A
            # TypeError raised by the implementation is never retried with a
            # different argument set (which could drop workspace binding).
            accepts_varargs = any(
                item.kind is inspect.Parameter.VAR_POSITIONAL
                for item in parameters.values()
            )
            positional = any(
                item.kind in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                }
                for item in parameters.values()
            )
            if positional or accepts_varargs:
                return method(workspace)
            if "workspace" in parameters:
                return method(workspace=workspace)
            return method()
        return None

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

    ``manifest``, ``v2`` and ``legacy`` are explicit ports.  They may be
    supplied as keyword arguments or through a :class:`RuntimePorts` bundle;
    no implementation module is discovered by import or global lookup.
    """

    def __init__(
        self,
        manifest: Any = None,
        v2: Any = None,
        legacy: Any = None,
        *,
        ports: RuntimePorts | Mapping[str, Any] | Any | None = None,
        workspace: str = "",
        readiness: Any = None,
        context_engine: Any = None,
        recall_planner: Any = None,
        hook_v2: Any = None,
        hook_legacy: Any = None,
        readiness_gate: Any = None,
        legacy_adapter: Any = None,
        manifest_store: Any = None,
        v2_port: Any = None,
        legacy_port: Any = None,
        **aliases: Any,
    ) -> None:
        manifest = manifest if manifest is not None else manifest_store
        v2 = v2 if v2 is not None else v2_port
        legacy = legacy if legacy is not None else legacy_port
        if manifest is None:
            manifest = aliases.pop("system_manifest", None)
        if readiness_gate is None:
            readiness_gate = aliases.pop("readiness_port", None)
        overrides = {
            "manifest": manifest,
            "v2": v2,
            "legacy": legacy,
            "readiness": readiness_gate or readiness,
            "context_engine": context_engine,
            "recall_planner": recall_planner,
            "hook_v2": hook_v2,
            "hook_legacy": hook_legacy,
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
        self._legacy_adapter = legacy_adapter
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
            from ..compat_v2 import MCP_MUTATION_NAMES, MCP_TOOL_NAMES
            known_mutation = name in MCP_MUTATION_NAMES
            known_name = name in MCP_TOOL_NAMES
            return True if known_mutation or not known_name else bool(explicit)
        if surface == "gui":
            from ..compat_v2.legacy_adapter import GUI_METHOD_NAMES, RULE_MUTATION_GUI_NAMES
            # GUI's public mutation registry is broader than the rule-only
            # context set mirrored by LegacyV2Adapter.  Keep the canonical
            # security classification authoritative at this boundary.
            from ..security import MUTATION_API_METHODS
            known_mutation = (
                name in RULE_MUTATION_GUI_NAMES
                or name in MUTATION_API_METHODS
                or name in {"request_mutation", "submit_request"}
            )
            known_name = name in GUI_METHOD_NAMES
            return True if known_mutation or not known_name else bool(explicit)
        if surface == "cli":
            from ..compat_v2.legacy_adapter import CLI_COMMAND_NAMES
            known_name = name in CLI_COMMAND_NAMES
            return True if not known_name else bool(explicit) or _LegacyV2AdapterClassifier._cli_is_mutation(name, args)
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
        payload = sanitize_public_payload(
            dict(result) if isinstance(result, Mapping) else {"data": _safe_dict(result)},
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
        is_mutation = self._mutation(surface, name, args, mutation)
        if snapshot.state is CutoverState.V2_READY and is_mutation:
            # READY is a V2 read-only state.  Do not invoke either route.
            return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_not_active")
        # READY and ACTIVE are V2-only paths.  Never instantiate or invoke the
        # compatibility adapter here, even for ordinary reads.
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
        if context is not None and not self._supports_context(self.ports.legacy):
            return self._envelope(
                {"ok": False}, snapshot, path="legacy", status="error",
                code="legacy_context_capability_required",
            )
        if context is not None:
            try:
                result = self._invoke_port(
                    _GenerationPort(self.ports.legacy, generation=snapshot.generation, state=snapshot.state, facade=self),
                    surface, name, args if args is not None else {},
                    context=context,
                    mutation=is_mutation,
                )
                with self._lock:
                    self._metrics["legacy_calls"] += 1
                return self._envelope(result, snapshot, path="legacy", status="ok")
            except Exception:
                return self._envelope({"ok": False}, snapshot, path="legacy", status="error", code="legacy_error")
        adapter = self._legacy_adapter
        if adapter is None:
            adapter = LegacyV2Adapter(
                v2_port=_GenerationPort(self.ports.v2, generation=snapshot.generation, state=snapshot.state, facade=self),
                legacy_port=_GenerationPort(self.ports.legacy, generation=snapshot.generation, state=snapshot.state, facade=self),
                workspace=self.workspace,
                v2_ready=snapshot.state.value,
            )
        try:
            if surface == "mcp":
                result = adapter.dispatch_mcp(name, args, context=context)
            elif surface == "gui":
                result = adapter.dispatch_gui(name, args, mutation=is_mutation, context=context)
            elif surface == "cli":
                result = adapter.dispatch_cli(name, args, mutation=is_mutation)
            else:
                return self._base(snapshot, code="unknown_surface")
        except Exception:
            return self._envelope({"ok": False}, snapshot, path="none", status="error", code="runtime_dispatch_failed")
        path = str(result.get("path", "none")) if isinstance(result, Mapping) else ("legacy" if snapshot.legacy_route else "v2")
        if path == "legacy":
            with self._lock:
                self._metrics["legacy_calls"] += 1
        elif path == "v2":
            with self._lock:
                self._metrics["v2_calls"] += 1
        return self._envelope(result, snapshot, path=path, status=str(result.get("status", "ok")) if isinstance(result, Mapping) else "ok")

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
        if context is not None and snapshot.state in {CutoverState.V2_READY, CutoverState.V2_ACTIVE}:
            context_port = self.ports.hook_v2 or self.ports.v2 or self.context_engine
            if context_port is None or not self._supports_context(context_port):
                return self._envelope({"ok": False}, snapshot, path="v2", status="error", code="v2_context_capability_required")
        port = self.ports.legacy if snapshot.legacy_route else (self.ports.hook_v2 or self.ports.v2)
        path = "legacy" if snapshot.legacy_route else "v2"
        if port is None and not snapshot.legacy_route and self.context_engine is not None:
            port = self.context_engine
        try:
            if port is None:
                raise RuntimeError("runtime_port_unavailable")
            if self.context_engine is port:
                fn = getattr(port, "bootstrap", None) or getattr(port, "build_context", None)
                if not callable(fn):
                    raise RuntimeError("context_engine_missing_bootstrap")
                result = fn(request, payload)
            elif snapshot.legacy_route and self.ports.hook_legacy is not None:
                fn = getattr(self.ports.hook_legacy, "bootstrap_hook", None) or getattr(self.ports.hook_legacy, "bootstrap", None)
                if not callable(fn):
                    raise RuntimeError("hook_port_missing_bootstrap")
                result = self._invoke_port(self.ports.hook_legacy, "hook", "bootstrap_hook", {"request": request, "payload": payload}, generation=snapshot.generation, context=context)
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
    return V2RuntimeFacade(workspace=workspace, manifest=manifest, v2=port)


__all__ = ["V2RuntimeFacade", "get_v2_runtime_facade"]
