"""Fail-closed state and generation contracts for the V2 cutover core.

The cutover package deliberately treats the manifest as an injected port.  A
manifest reader may be :class:`ManifestManager`, a test double, or a remote
adapter; this module only normalises its immutable record into a small runtime
snapshot and never creates storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


class CutoverState(str, Enum):
    V1_ACTIVE = "V1_ACTIVE"
    V2_BUILDING = "V2_BUILDING"
    V2_READY = "V2_READY"
    V2_ACTIVE = "V2_ACTIVE"
    UNKNOWN = "UNKNOWN"


V1_ACTIVE = CutoverState.V1_ACTIVE.value
V2_BUILDING = CutoverState.V2_BUILDING.value
V2_READY = CutoverState.V2_READY.value
V2_ACTIVE = CutoverState.V2_ACTIVE.value
UNKNOWN = CutoverState.UNKNOWN.value
KNOWN_STATES = frozenset({V1_ACTIVE, V2_BUILDING, V2_READY, V2_ACTIVE})
# Compatibility aliases used by migration/runtime callers.
RuntimeState = CutoverState
ManifestState = CutoverState


class CutoverError(RuntimeError):
    """Base error raised by the cutover boundary."""


class ManifestUnavailable(CutoverError):
    """The state source could not be read safely."""


class GenerationConflict(CutoverError):
    """A request was evaluated against a stale manifest generation."""


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _state(value: Any) -> CutoverState:
    marker = getattr(value, "value", value)
    marker = _text(marker).upper()
    try:
        return CutoverState(marker)
    except ValueError:
        return CutoverState.UNKNOWN


def _detached(value: Any) -> Any:
    """Copy caller-owned containers before a snapshot retains them."""

    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if value is None or isinstance(value, (str, int, float, bool, bytes, complex)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _detached(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_detached(item) for item in value]
    if isinstance(value, (set, frozenset)):
        # Sort by representation so a set containing mutable mappings can be
        # detached without attempting to hash the copied values.
        return [_detached(item) for item in sorted(value, key=repr)]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _detached(to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__") and not isinstance(value, type):
        try:
            return {str(key): _detached(item) for key, item in vars(value).items()}
        except Exception:
            pass
    # Slot-only objects have no ``__dict__`` but may still carry mutable
    # evidence.  Detach every declared slot into an immutable mapping rather
    # than retaining the opaque object reference.
    slots: list[str] = []
    try:
        for cls in type(value).__mro__:
            declared = getattr(cls, "__slots__", ())
            if isinstance(declared, str):
                declared = (declared,)
            slots.extend(str(name) for name in declared if name not in {"__dict__", "__weakref__"})
    except Exception:
        slots = []
    if slots:
        detached_slots: dict[str, Any] = {}
        for name in dict.fromkeys(slots):
            try:
                detached_slots[name] = _detached(getattr(value, name))
            except Exception:
                continue
        if detached_slots:
            return detached_slots
    # Opaque values cannot be made immutable safely; retain only a stable
    # textual representation instead of a caller-owned mutable reference.
    try:
        return repr(value)
    except Exception:
        return type(value).__name__


@dataclass(frozen=True)
class RuntimeSnapshot:
    """One immutable state/generation read used by a single request."""

    state: CutoverState
    generation: int
    migration_id: str = ""
    source_digest: str = ""
    target_digest: str = ""
    manifest_digest: str = ""
    digests: Mapping[str, Any] = field(default_factory=dict)
    checkpoints: Mapping[str, Any] = field(default_factory=dict)
    errors: Mapping[str, Any] = field(default_factory=dict)
    last_error: str = ""
    available: bool = True
    error: str = ""
    raw: Any = None
    _trusted: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Normalize enum/string values even when callers construct the
        # dataclass directly.  Mapping proxies prevent a caller from mutating
        # evidence after the request's CAS snapshot was captured.
        normalized = _state(self.state)
        object.__setattr__(self, "state", normalized)
        generation = self.generation
        if isinstance(generation, bool) or type(generation) is not int or generation < 0:
            generation = -1
            object.__setattr__(self, "available", False)
            if not self.error:
                object.__setattr__(self, "error", "v2_manifest_state_corrupt")
        object.__setattr__(self, "generation", generation)
        if normalized is CutoverState.UNKNOWN and self.available:
            object.__setattr__(self, "available", False)
            if not self.error:
                object.__setattr__(self, "error", "v2_manifest_state_unavailable")
        for name in ("digests", "checkpoints", "errors"):
            value = getattr(self, name)
            object.__setattr__(self, name, _freeze(_detached(value)) if isinstance(value, Mapping) else MappingProxyType({}))
        object.__setattr__(self, "raw", _freeze(_detached(self.raw)) if self.raw is not None else None)

    @classmethod
    def unavailable(cls, reason: str = "v2_manifest_state_unavailable", *, raw: Any = None) -> "RuntimeSnapshot":
        snapshot = cls(
            state=CutoverState.UNKNOWN,
            generation=-1,
            available=False,
            error=_text(reason) or "v2_manifest_state_unavailable",
            raw=raw,
        )
        object.__setattr__(snapshot, "_trusted", True)
        return snapshot

    @classmethod
    def from_value(cls, value: Any) -> "RuntimeSnapshot":
        """Normalise a manager record or mapping without widening authority.

        Unknown/corrupt state and generation values become an unavailable
        snapshot.  Callers can therefore return a stable error envelope and
        must never infer ``V1_ACTIVE`` from malformed data.
        """

        if isinstance(value, cls):
            return value if value.trusted else cls.unavailable("invalid_runtime_snapshot", raw=value)
        if value is None:
            return cls.unavailable(raw=value)
        try:
            state_value = value.get("state") if isinstance(value, Mapping) else getattr(value, "state")
            state = _state(state_value)
            default_generation = 0 if state is CutoverState.V1_ACTIVE else -1
            raw_generation = value.get("generation", default_generation) if isinstance(value, Mapping) else getattr(value, "generation", default_generation)
            if isinstance(raw_generation, bool):
                raise ValueError("boolean generation")
            if isinstance(raw_generation, bool) or type(raw_generation) is not int or raw_generation < 0:
                raise ValueError("negative generation")
            generation = raw_generation
            get = (lambda name, default="": value.get(name, default)) if isinstance(value, Mapping) else (lambda name, default="": getattr(value, name, default))
            availability = get("available", True)
            if type(availability) is not bool or not availability:
                return cls.unavailable(_text(get("error")) or "v2_manifest_state_unavailable", raw=value)
            if _text(get("error")):
                return cls.unavailable(_text(get("error")), raw=value)
            if isinstance(value, Mapping) and value.get("ok") is False:
                return cls.unavailable(_text(get("error")) or "v2_manifest_state_unavailable", raw=value)
            digests = _mapping(get("digests", get("digest_metadata", {})))
            checkpoints = _mapping(get("checkpoints", digests.get("checkpoints", {})))
            errors = _mapping(get("errors", {}))
            if state is CutoverState.UNKNOWN:
                return cls.unavailable("v2_manifest_state_unavailable", raw=value)
            snapshot = cls(
                state=state,
                generation=generation,
                migration_id=_text(get("migration_id")),
                source_digest=_text(get("source_digest")),
                target_digest=_text(get("target_digest")),
                manifest_digest=_text(get("manifest_digest")),
                digests=digests,
                checkpoints=checkpoints,
                errors=errors,
                last_error=_text(get("last_error")),
                available=True,
                raw=value,
            )
            object.__setattr__(snapshot, "_trusted", True)
            return snapshot
        except Exception:
            return cls.unavailable("v2_manifest_state_corrupt", raw=value)

    @property
    def trusted(self) -> bool:
        """Whether this snapshot came from the guarded factory boundary."""

        return self._trusted

    @property
    def state_value(self) -> str:
        return self.state.value

    def __str__(self) -> str:
        """Preserve compatibility with adapters that consume a marker string."""

        return self.state.value

    @property
    def ready(self) -> bool:
        return self.state in {CutoverState.V2_READY, CutoverState.V2_ACTIVE}

    @property
    def active(self) -> bool:
        return self.state is CutoverState.V2_ACTIVE

    @property
    def read_only(self) -> bool:
        return self.state is CutoverState.V2_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "generation": self.generation,
            "migration_id": self.migration_id,
            "source_digest": self.source_digest,
            "target_digest": self.target_digest,
            "manifest_digest": self.manifest_digest,
            "digests": dict(self.digests),
            "checkpoints": dict(self.checkpoints),
            "errors": dict(self.errors),
            "last_error": self.last_error,
            "available": self.available,
            **({"error": self.error} if self.error else {}),
        }


def snapshot_from_port(port: Any) -> RuntimeSnapshot:
    """Read one manifest snapshot from an explicit port.

    Exactly one reader method is invoked.  ``current``/``read`` are preferred
    because they preserve the manager's immutable manifest record; ``status``
    and ``snapshot`` are compatibility aliases for injected ports.
    """

    if port is None:
        return RuntimeSnapshot.from_value({"state": "V1_ACTIVE", "generation": 0})


    try:
        value: Any = None
        for name in ("current", "read", "snapshot", "status"):
            method = getattr(port, name, None)
            if callable(method):
                value = method()
                break
        else:
            value = port
        return RuntimeSnapshot.from_value(value)
    except Exception:
        return RuntimeSnapshot.unavailable("v2_manifest_state_unavailable")


def state_snapshot(port: Any) -> RuntimeSnapshot:
    """Public name for one immutable manifest/CAS read."""

    return snapshot_from_port(port)


__all__ = [
    "CutoverState", "RuntimeState", "ManifestState", "V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE", "UNKNOWN",
    "KNOWN_STATES", "CutoverError", "ManifestUnavailable", "GenerationConflict",
    "RuntimeSnapshot", "StateSnapshot", "CutoverSnapshot", "snapshot_from_port", "read_snapshot", "state_snapshot",
]

StateSnapshot = RuntimeSnapshot
CutoverSnapshot = RuntimeSnapshot
read_snapshot = snapshot_from_port
