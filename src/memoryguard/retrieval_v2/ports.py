"""Layer port contracts used by :mod:`memoryguard.retrieval_v2`.

Ports are dependency-injected.  Retrieval code never opens a V1 store or a
SQLite database; an application can adapt any governed read path to this
small protocol.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .models import RecallRequest


@runtime_checkable
class LayerPort(Protocol):
    """Minimal read-only layer adapter.

    ``trusted`` defaults to ``True`` for the built-in ``rules`` layer only;
    adapters exposing another layer must explicitly opt in if they need
    authoritative metadata, and the planner still clamps trust by layer.
    """

    layer: str

    def read(self, request: RecallRequest) -> Iterable[Any]:
        ...


@dataclass(frozen=True)
class StaticLayerPort:
    """Tiny immutable port useful for tests and embedding callers."""

    layer: str
    items: tuple[Any, ...] = ()
    configured: bool = True
    trusted: bool | None = None
    status: str = "READY"

    def __init__(
        self,
        layer: str,
        items: Iterable[Any] = (),
        *,
        configured: bool = True,
        trusted: bool | None = None,
        status: str = "READY",
    ) -> None:
        object.__setattr__(self, "layer", str(layer).strip().lower())
        object.__setattr__(self, "items", tuple(items))
        object.__setattr__(self, "configured", bool(configured))
        object.__setattr__(self, "trusted", trusted)
        object.__setattr__(self, "status", str(status))

    def read(self, request: RecallRequest) -> Iterator[Any]:
        del request
        yield from self.items


@dataclass(frozen=True)
class CallableLayerPort:
    """Adapter for a read function without store coupling."""

    layer: str
    reader: Any
    configured: bool = True
    trusted: bool | None = None
    status: str = "READY"

    def read(self, request: RecallRequest) -> Iterable[Any]:
        return self.reader(request)


__all__ = ["LayerPort", "StaticLayerPort", "CallableLayerPort"]
