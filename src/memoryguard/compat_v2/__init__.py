"""Contract-only adapters for the V2 shadow build.

Phase 6 keeps the public GUI/CLI surfaces stable while the runtime facade is
introduced independently.  The small helpers below deliberately use
feature-detection: importing MemoryGuard on a V1 installation must not import
or construct any V2/legacy storage implementation.  When the V2 facade is
missing, the manifest is still inspected so an ``V2_ACTIVE`` workspace fails
closed instead of silently falling back to V1.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import inspect
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .legacy_adapter import (
    AdapterEnvelope,
    LegacyV2Adapter,
    V2LegacyAdapter,
    LegacyAPIAdapter,
    LegacyAdapter,
    LegacyAdapterPorts,
    LegacyAdapterPort,
    LegacyPort,
    V2Port,
    MCP_TOOL_NAMES,
    LEGACY_MCP_TOOL_NAMES,
    GUI_METHOD_NAMES,
    LEGACY_GUI_METHOD_NAMES,
    SAFE_BRIDGE_METHOD_NAMES,
    CLI_COMMAND_NAMES,
    LEGACY_CLI_COMMAND_NAMES,
    MCP_MUTATION_NAMES,
    MUTATING_MCP_TOOL_NAMES,
    invoke_once,
    safe_error_code,
    safe_exception_diagnostic,
    sanitize_public_payload,
)
from ..governance_v2.rules import RuleMutationContext, RuleMutationError


def _invoke_status_once(fn: Any, target: str, *, error_code: str) -> Any:
    """Choose status call shape before invocation; never retry TypeError."""
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise RuleMutationError(error_code) from exc
    try:
        signature.bind(target)
    except TypeError:
        try:
            signature.bind(workspace=target)
        except TypeError:
            try:
                signature.bind()
            except TypeError as exc:
                raise RuleMutationError(error_code) from exc
            return fn()
        return fn(workspace=target)
    return fn(target)


def _invoke_kwargs_once(
    fn: Any,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    error_code: str,
) -> Any:
    """Bind one complete call shape, then invoke once (including TypeError)."""
    try:
        signature = inspect.signature(fn)
        signature.bind(*args, **dict(kwargs))
    except (TypeError, ValueError) as exc:
        raise RuleMutationError(error_code) from exc
    return fn(*args, **dict(kwargs))


@runtime_checkable
class V2RuntimePort(Protocol):
    """Minimal Phase 6-A facade seam used by GUI and CLI.

    The facade may expose either ``state_snapshot`` or ``status``.  Dispatch
    methods are intentionally surface-specific so a Namespace's sub-action
    cannot be dropped while adapting the CLI.
    """

    def status(self, workspace: str) -> Mapping[str, Any] | bool: ...

    def state_snapshot(self, workspace: str) -> Mapping[str, Any] | bool: ...

    def dispatch_gui(
        self,
        name: str,
        args: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def dispatch_cli(
        self,
        command: str,
        args: Mapping[str, Any] | Any = None,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class _BridgeMutationContext:
    """Adapter-compatible immutable context for trusted GUI/CLI calls."""

    values: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.values)


class _ManifestStatePort:
    """Read the activation manifest without creating a V2 store."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())

    def status(self, workspace: str = "") -> str:
        try:
            target = Path(workspace or self.workspace).expanduser().resolve()
            # A purely synthetic/nonexistent workspace has no manifest and is
            # the legacy default.  Existing but malformed storage is handled
            # below as UNKNOWN and therefore fails closed.
            if not target.exists():
                return "V1_ACTIVE"
            from ..system.manifest import ManifestManager

            return ManifestManager(target).current().state.value
        except Exception:
            # Unknown/corrupt manifests are never treated as V1-ready.
            return "UNKNOWN"

    state_snapshot = status


def load_v2_runtime(workspace: str | Path, override: Any | None = None) -> Any | None:
    """Load the Phase 6-A facade if present, without making it mandatory.

    ``override`` is intentionally public for acceptance fixtures and host
    integration tests.  A factory/class can be supplied by the facade module;
    all import/constructor failures leave a manifest-only port in place, which
    makes active/unknown states fail closed while preserving V1 operation.
    """

    if override is not None:
        return override
    try:
        module = importlib.import_module("memoryguard.cutover_v2.facade")
    except (ImportError, ModuleNotFoundError):
        return None
    try:
        factory = getattr(module, "get_v2_runtime_facade", None)
        if callable(factory):
            path = str(Path(workspace).expanduser().resolve())
            # Select the constructor shape before calling.  A TypeError raised
            # inside a factory is an actual construction failure, not a cue to
            # retry without the workspace binding.
            try:
                signature = inspect.signature(factory)
            except (TypeError, ValueError) as exc:
                raise RuleMutationError("v2_runtime_factory_signature_unavailable") from exc
            try:
                signature.bind(path)
            except TypeError:
                try:
                    signature.bind(workspace=path)
                except TypeError as exc:
                    raise RuleMutationError("v2_runtime_factory_signature_unavailable") from exc
                return factory(workspace=path)
            return factory(path)
        cls = getattr(module, "V2RuntimeFacade", None)
        if callable(cls):
            path = str(Path(workspace).expanduser().resolve())
            # The concrete facade does not infer a workspace manifest on its
            # own.  Inject the read-only manager so corrupt manifests become
            # UNKNOWN instead of silently defaulting to V1_ACTIVE.  Bind once
            # from the signature; do not catch-and-retry on implementation
            # TypeError.
            from ..system.manifest import ManifestManager

            manifest = ManifestManager(path)
            try:
                signature = inspect.signature(cls)
            except (TypeError, ValueError) as exc:
                raise RuleMutationError("v2_runtime_constructor_signature_unavailable") from exc
            kwargs = {"workspace": path, "manifest": manifest}
            try:
                signature.bind(**kwargs)
            except TypeError:
                try:
                    signature.bind(path)
                except TypeError as exc:
                    raise RuleMutationError("v2_runtime_constructor_signature_unavailable") from exc
                return cls(path)
            return cls(**kwargs)
    except Exception:
        # Keep the manifest state visible; dispatch will fail closed in V2.
        return None
    return None


def _runtime_port(workspace: str | Path, override: Any | None = None) -> Any:
    """Return a facade or manifest-only state port for one API call."""

    if override is None and not Path(workspace).expanduser().resolve().exists():
        return _ManifestStatePort(workspace)
    runtime = load_v2_runtime(workspace, override)
    if runtime is None:
        return _ManifestStatePort(workspace)
    return _FacadePort(runtime, workspace)


class _FacadePort:
    """Normalize the Phase 6-A facade's optional state method names."""

    def __init__(self, facade: Any, workspace: str | Path) -> None:
        self.facade = facade
        self.workspace = str(Path(workspace).expanduser().resolve())

    def status(self, workspace: str = "") -> Any:
        target = workspace or self.workspace
        fn = getattr(self.facade, "status", None)
        if callable(fn):
            return _invoke_status_once(fn, target, error_code="v2_status_signature_unavailable")
        fn = getattr(self.facade, "state_snapshot", None)
        if callable(fn):
            return _invoke_status_once(fn, target, error_code="v2_snapshot_signature_unavailable")
        return "UNKNOWN"

    state_snapshot = status

    def __getattr__(self, name: str) -> Any:
        return getattr(self.facade, name)


def make_cutover_adapter(
    workspace: str | Path,
    *,
    legacy_port: Any,
    v2_port: Any | None = None,
    trusted_context: Mapping[str, Any] | None = None,
) -> "Phase6CutoverAdapter":
    """Build a per-call V1/V2 gate.

    A fresh adapter per entrypoint call guarantees exactly one manifest/status
    read for the decision.  ``Phase6CutoverAdapter`` never retries a V2 call
    without its trusted context.
    """

    # Keep every public entrypoint on the lazy adapter.  It does not construct
    # LegacyV2Adapter (or the legacy port factory) until V1_ACTIVE/
    # V2_BUILDING has been selected.
    runtime = None
    if v2_port is not None or Path(workspace).expanduser().resolve().exists():
        runtime = load_v2_runtime(workspace, v2_port)
    return _LazyCutoverAdapter(
        workspace,
        v2_port=runtime if runtime is not None else _ManifestStatePort(workspace),
        legacy_factory=legacy_port,
        trusted_context=trusted_context,
    )
class _LazyCutoverAdapter:
    """Phase 6 gate that keeps legacy construction behind the state check."""

    def __init__(self, workspace: str | Path, *, v2_port: Any = None,
                 legacy_factory: Any = None,
                 trusted_context: Mapping[str, Any] | None = None) -> None:
        self.workspace = str(workspace)
        self.v2_port = v2_port if v2_port is not None else _ManifestStatePort(workspace)
        self.legacy_factory = legacy_factory
        self.trusted_context = dict(trusted_context or {})
        self._state: str | None = None
        self._snapshot: Any = None

    def _read_state(self) -> str:
        if self._state is not None:
            return self._state
        try:
            # Native facade snapshots are reusable by dispatch, so its
            # internal manifest reader still runs exactly once per call.
            snapshot_fn = getattr(self.v2_port, "state_snapshot", None)
            status_fn = getattr(self.v2_port, "status", None)
            if callable(snapshot_fn) and self.v2_port.__class__.__name__ == "V2RuntimeFacade":
                self._snapshot = snapshot_fn()
                value = self._snapshot
            elif callable(status_fn):
                value = _invoke_status_once(
                    status_fn, self.workspace,
                    error_code="v2_status_signature_unavailable",
                )
            else:
                value = getattr(self.v2_port, "state", "UNKNOWN")
            self._state = LegacyV2Adapter._state_value(value)
        except Exception:
            self._state = "UNKNOWN"
        return self._state

    def v2_state(self) -> str:
        return self._read_state()

    def _begin_call(self) -> None:
        # One adapter may serve multiple surface calls in tests/hosts, but
        # each call must capture a fresh manifest snapshot exactly once.
        self._state = None
        self._snapshot = None

    @staticmethod
    def _cli_is_mutation(command: str, args: Any) -> bool:
        return LegacyV2Adapter._cli_is_mutation(command, args)

    @staticmethod
    def _error(surface: str, name: str, code: str, *, path: str = "none") -> dict[str, Any]:
        return {
            "ok": False,
            "status": "error",
            "surface": surface,
            "name": name,
            "path": path,
            "error": code,
            "code": code,
        }

    def _invoke_v2(self, surface: str, name: str, args: Any,
                   *, mutation: bool, context: Any = None) -> dict[str, Any]:
        port = self.v2_port
        fn = getattr(port, "dispatch", None) or getattr(port, "call", None)
        surface_specific = False
        if not callable(fn):
            fn = getattr(port, {"gui": "dispatch_gui", "cli": "dispatch_cli"}.get(surface, ""), None)
            surface_specific = True
        if not callable(fn):
            return self._error(surface, name, "v2_adapter_port_missing_dispatch", path="v2")
        kwargs: dict[str, Any] = {}
        try:
            parameters = inspect.signature(fn).parameters
            has_var_kw = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
        except (TypeError, ValueError):
            parameters, has_var_kw = {}, False
        # GUI readonly calls also carry the server-bound scope/context. The
        # context is provenance, not mutation authority; only native mutation
        # boundaries require the process-local sentinel/admin capability.
        if context is not None:
            if not has_var_kw and "context" not in parameters:
                return self._error(surface, name, "v2_context_capability_required", path="v2")
            kwargs["context"] = context.to_dict() if hasattr(context, "to_dict") else dict(context)
        if has_var_kw or "mutation" in parameters:
            kwargs["mutation"] = bool(mutation)
        if self._snapshot is not None and (has_var_kw or "snapshot" in parameters):
            kwargs["snapshot"] = self._snapshot
        try:
            if surface_specific:
                result = fn(name, args, **kwargs)
            else:
                result = fn(surface, name, args, **kwargs)
        except Exception as exc:
            code = safe_error_code(exc, "v2_error")
            return self._error(surface, name, code, path="v2") | {
                "diagnostic": safe_exception_diagnostic(exc, code=code),
            }
        payload = dict(result) if isinstance(result, Mapping) else {"data": result}
        payload.setdefault("ok", True)
        payload.setdefault("status", "ok")
        payload.setdefault("surface", surface)
        payload.setdefault("name", name)
        payload.setdefault("path", "v2")
        return payload

    def _legacy(self) -> LegacyV2Adapter:
        port = self.legacy_factory() if callable(self.legacy_factory) else self.legacy_factory
        return LegacyV2Adapter(
            workspace=self.workspace,
            v2_port=None,
            legacy_port=port,
            v2_ready=self._state,
        )

    def _dispatch(self, surface: str, name: str, args: Any = None,
                  *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        if context is None and self.trusted_context and surface in {"gui", "hook"}:
            context = dict(self.trusted_context)
        state = self._read_state()
        if state not in {"V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE"}:
            return self._error(surface, name, "v2_manifest_state_unavailable")
        if state == "V2_READY" and mutation:
            return self._error(surface, name, "v2_not_active", path="v2")
        if state in {"V2_READY", "V2_ACTIVE"}:
            return self._invoke_v2(surface, name, args, mutation=mutation, context=context)
        # Only now create the legacy adapter/factory.  V2 and fail-closed
        # paths never touch GovernanceApi, RequestQueue, or LegacyV2Adapter.
        adapter = self._legacy()
        if surface == "gui":
            return adapter.dispatch_gui(name, args, mutation=mutation, context=context)
        if surface == "cli":
            return adapter.dispatch_cli(name, args, mutation=mutation)
        return self._error(surface, name, "unknown_surface")

    def dispatch_gui(self, method: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        self._begin_call()
        try:
            from ..security import is_mutation_method
            mutation = bool(mutation or is_mutation_method(method))
        except Exception:
            pass
        if method not in GUI_METHOD_NAMES:
            return self._error("gui", method, "unknown_gui_method")
        return self._dispatch("gui", method, args, mutation=mutation, context=context)

    def dispatch_cli(self, command: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        self._begin_call()
        if command not in CLI_COMMAND_NAMES:
            return self._error("cli", command, "unknown_cli_command")
        effective = bool(mutation or LegacyV2Adapter._cli_is_mutation(command, args))
        result = self._dispatch("cli", command, args, mutation=effective, context=context)
        result.setdefault("command", command)
        return result


class _NativeFacadeAdapter:
    """Adapter shim for ``V2RuntimeFacade``'s already-gated dispatch methods."""

    def __init__(self, workspace: str | Path, facade: Any, *, trusted_context: Mapping[str, Any] | None = None) -> None:
        self.workspace = str(workspace)
        self.facade = facade
        self._trusted_context = dict(trusted_context or {})

    @staticmethod
    def _ctx(value: Any) -> Mapping[str, Any] | None:
        if value is None:
            return None
        # Keep the process-local NativeBoundContext object when adapting a
        # mapping.  ``to_dict()`` projections intentionally omit authority;
        # only the private object survives the transport dictification.
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "to_dict"):
            projected = value.to_dict()
            bound = getattr(value, "bound_context", None)
            if bound is not None:
                projected = dict(projected)
                projected["__native_bound_context"] = bound
            return projected
        return dict(value)

    def v2_state(self) -> str:
        """Expose the facade's manifest marker for transport-side gating.

        ``V2RuntimeFacade`` performs the authoritative snapshot gate during
        dispatch.  The bridge also needs a cheap preflight to decide whether
        a legacy sandbox request may enter ``RequestQueue``; this method keeps
        that decision on the same facade/manifest port without constructing a
        legacy implementation.
        """
        try:
            status = getattr(self.facade, "status", None)
            value = status() if callable(status) else getattr(self.facade, "state", "UNKNOWN")
            return LegacyV2Adapter._state_value(value)
        except Exception:
            return "UNKNOWN"

    @staticmethod
    def _mutation_context(context: Any, default: Mapping[str, Any]) -> Any:
        if context is not None:
            return context
        return _BridgeMutationContext(default) if default else None

    def dispatch_gui(self, method: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        fn = getattr(self.facade, "dispatch_gui", None)
        if not callable(fn):
            return {"ok": False, "error": "v2_adapter_port_missing_dispatch", "code": "v2_adapter_port_missing_dispatch", "path": "none"}
        # The bridge is defensive even when a caller lies about ``mutation``:
        # known GUI writes must never be downgraded to a read-only V2 call.
        try:
            from ..security import is_mutation_method
            effective_mutation = bool(mutation or is_mutation_method(method))
        except Exception:
            effective_mutation = bool(mutation)
        # Preserve bound context on readonly GUI calls as well. The native
        # port uses it to scope reads; sentinel/admin checks remain gated by
        # the mutation flag inside the port.  Signature binding happens before
        # invocation so an implementation TypeError is not retried.
        trusted = self._mutation_context(context, self._trusted_context) if (context is not None or self._trusted_context) else None
        try:
            return _invoke_kwargs_once(
                fn, (method, args),
                {"mutation": effective_mutation, "context": self._ctx(trusted)},
                error_code=("v2_context_capability_required" if mutation and trusted else "v2_adapter_signature_unavailable"),
            )
        except RuleMutationError as exc:
            code = safe_error_code(exc, "v2_adapter_signature_unavailable")
            return {"ok": False, "error": code, "code": code, "path": "v2", "diagnostic": safe_exception_diagnostic(exc, code=code)}

    def dispatch_cli(self, command: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        fn = getattr(self.facade, "dispatch_cli", None)
        if not callable(fn):
            return {"ok": False, "error": "v2_adapter_port_missing_dispatch", "code": "v2_adapter_port_missing_dispatch", "path": "none"}
        # CLI facade currently derives mutation classification from the
        # Namespace.  Keep the Namespace intact; no positional collapse.
        effective_mutation = bool(mutation or self._cli_is_mutation(command, args))
        trusted = self._mutation_context(context, self._trusted_context) if (context is not None or self._trusted_context) else None
        try:
            return _invoke_kwargs_once(
                fn, (command, args),
                {"mutation": effective_mutation, "context": self._ctx(trusted)},
                error_code=("v2_context_capability_required" if mutation and trusted else "v2_adapter_signature_unavailable"),
            )
        except RuleMutationError as exc:
            code = safe_error_code(exc, "v2_adapter_signature_unavailable")
            return {"ok": False, "error": code, "code": code, "path": "v2", "diagnostic": safe_exception_diagnostic(exc, code=code)}

    def _cli_is_mutation(self, command: str, args: Any) -> bool:
        return LegacyV2Adapter._cli_is_mutation(command, args)


class Phase6CutoverAdapter(LegacyV2Adapter):
    """Thin extension of the stable adapter for GUI/CLI Phase 6 wiring."""

    def __init__(self, workspace: str | Path, *, trusted_context: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        self._trusted_context = dict(trusted_context or {})
        self._active_context: Any | None = None
        super().__init__(workspace=str(workspace), **kwargs)

    def _dispatch(self, surface: str, name: str, args: Mapping[str, Any] | None = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:  # type: ignore[override]
        # Trusted bridge identity is supplied by the transport, never by an
        # actor/preview argument.  Keep it active only for this one dispatch.
        supplied = context
        if mutation and supplied is None and self._trusted_context:
            supplied = _BridgeMutationContext(self._trusted_context)
        self._active_context = supplied
        try:
            return super()._dispatch(surface, name, args, mutation=mutation, context=context)
        finally:
            self._active_context = None

    def _invoke_v2(self, surface: str, name: str, args: Mapping[str, Any], *, context: Any = None) -> Any:  # type: ignore[override]
        # LegacyV2Adapter only attaches contexts to its rule-mutation subset;
        # Phase 6 requires every V2 mutation to retain the trusted bridge
        # context.  Never retry after a signature/context mismatch.
        effective = context or self._active_context
        port = self.v2_port
        if port is None:
            raise RuleMutationError("v2_not_ready")
        fn = getattr(port, "dispatch", None) or getattr(port, "call", None)
        if callable(fn):
            if effective is not None and not self._supports_context(fn):
                raise RuleMutationError("v2_context_capability_required")
            kwargs = {"context": effective.to_dict() if hasattr(effective, "to_dict") else dict(effective)} if effective is not None else {}
            return fn(surface, name, dict(args), **kwargs)
        # Preferred Phase 6-A surface-specific methods.
        method_name = "dispatch_gui" if surface == "gui" else "dispatch_cli" if surface == "cli" else "dispatch_mcp"
        fn = getattr(port, method_name, None)
        if not callable(fn):
            # Rule facade ports may expose read/mutate only.  Context is
            # mandatory for mutation and must not be silently omitted.
            fn = getattr(port, "mutate" if effective is not None else "read", None)
        if not callable(fn):
            raise RuleMutationError("v2_adapter_port_missing_dispatch")
        if effective is not None and not self._supports_context(fn):
            raise RuleMutationError("v2_context_capability_required")
        kwargs = {"context": effective.to_dict() if hasattr(effective, "to_dict") else dict(effective)} if effective is not None else {}
        if method_name == "dispatch_cli":
            return fn(name, dict(args), **kwargs)
        if method_name == "dispatch_gui":
            return fn(name, dict(args), **kwargs)
        return fn(name, dict(args), **kwargs)

    def dispatch_gui(self, method: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        # Keep the public ``mutation`` flag as an explicit override, but do
        # not let ``False`` bypass the canonical GUI mutation registry.
        try:
            from ..security import is_mutation_method
            mutation = bool(mutation or is_mutation_method(method))
        except Exception:
            mutation = bool(mutation)
        return super().dispatch_gui(method, args, mutation=mutation, context=context)

    def dispatch_cli(self, command: str, args: Any = None, *, mutation: bool = False, context: Any = None) -> dict[str, Any]:
        return super().dispatch_cli(command, args, mutation=mutation, context=context)

__all__ = [
    "AdapterEnvelope", "LegacyV2Adapter", "V2LegacyAdapter", "LegacyAPIAdapter", "LegacyAdapter", "LegacyAdapterPorts", "LegacyAdapterPort", "LegacyPort", "V2Port",
    "MCP_TOOL_NAMES", "LEGACY_MCP_TOOL_NAMES", "GUI_METHOD_NAMES", "LEGACY_GUI_METHOD_NAMES", "SAFE_BRIDGE_METHOD_NAMES", "CLI_COMMAND_NAMES", "LEGACY_CLI_COMMAND_NAMES",
    "MCP_MUTATION_NAMES", "MUTATING_MCP_TOOL_NAMES",
    "RuleMutationContext",
    "V2RuntimePort", "Phase6CutoverAdapter", "make_cutover_adapter", "load_v2_runtime",
]
