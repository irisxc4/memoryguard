"""Dependency-injected ports for the V2 runtime facade.

No implementation module for MCP, GUI, Hook, or a V1 store is imported here.
The host supplies narrow adapters, making the cutover boundary auditable and
easy to exercise with call-counting fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ManifestPort(Protocol):
    def current(self) -> Any: ...


@runtime_checkable
class DispatchPort(Protocol):
    def dispatch(self, surface: str, name: str, args: Mapping[str, Any], **kwargs: Any) -> Any: ...


@runtime_checkable
class LegacyPort(DispatchPort, Protocol):
    """Explicit legacy route; facade never discovers a legacy implementation."""


@runtime_checkable
class V2Port(DispatchPort, Protocol):
    """Explicit V2 route; implementations may consume generation/CAS metadata."""


@runtime_checkable
class HookPort(Protocol):
    def bootstrap_hook(self, request: Any, payload: Any = None, **kwargs: Any) -> Any: ...


@runtime_checkable
class ReadinessPort(Protocol):
    def evaluate(self, evidence: Any = None, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RuntimePorts:
    """Named bundle accepted by :class:`V2RuntimeFacade`.

    Ports are intentionally typed as ``Any`` at runtime: adapters may be
    callables, protocol objects, or test doubles.  The facade validates each
    operation before invoking it and fails closed when a method is missing.
    """

    manifest: Any
    v2: Any = None
    legacy: Any = None
    hook_v2: Any = None
    hook_legacy: Any = None
    readiness: Any = None
    context_engine: Any = None
    recall_planner: Any = None

    @classmethod
    def from_value(cls, value: Any, **overrides: Any) -> "RuntimePorts":
        if isinstance(value, cls):
            data = {name: getattr(value, name) for name in cls.__dataclass_fields__}
        elif isinstance(value, Mapping):
            data = dict(value)
        else:
            data = {name: getattr(value, name, None) for name in cls.__dataclass_fields__}
        aliases = {
            "manifest_store": "manifest",
            "system_manifest": "manifest",
            "v2_port": "v2",
            "v2_runtime": "v2",
            "legacy_port": "legacy",
            "legacy_adapter": "legacy",
            "hook": "hook_v2",
            "hook_port": "hook_v2",
            "readiness_gate": "readiness",
            "planner": "recall_planner",
        }
        # Accept the same aliases whether supplied in the constructor kwargs
        # or in a mapping/namespace bundle.
        for alias, canonical in aliases.items():
            if alias in data and canonical not in data:
                data[canonical] = data[alias]
        for key, value_item in list(overrides.items()):
            data[aliases.get(key, key)] = value_item
        normalized = {name: data.get(name) for name in cls.__dataclass_fields__}
        # ``None`` is an explicit "cutover not configured" port.  The state
        # factory maps it to trusted ``V1_ACTIVE`` generation 0 without
        # creating a database; callers can still fail closed on malformed
        # non-None ports.
        return cls(**normalized)


__all__ = [
    "ManifestPort", "DispatchPort", "LegacyPort", "V2Port", "HookPort", "ReadinessPort", "RuntimePorts",
]
