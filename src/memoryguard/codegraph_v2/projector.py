"""Deterministic metadata projector for :mod:`memoryguard.codegraph_v2`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .models import CodeGraphError, CodeGraphScope, Edge, SourceFile, Symbol, stable_digest, validate_metadata
from .store import CodeGraphStore


_BODY_KEYS = frozenset(
    {
        "body",
        "text",
        "raw",
        "content",
        "source",
        "source_text",
        "full_transcript",
        "document_body",
    }
)


def _reject_body(value: Any) -> None:
    if isinstance(value, Mapping):
        # ``validate_metadata`` traverses mappings/lists recursively, bounds
        # depth/size, and rejects credentials/body/authority fields before the
        # first store transaction.  Keep the explicit body check for a stable
        # error class/message used by older callers.
        lowered = {str(key).strip().lower() for key in value}
        forbidden = sorted(lowered & _BODY_KEYS)
        if forbidden:
            raise CodeGraphError("codegraph projector refuses source-body fields: " + ",".join(forbidden))
        validate_metadata(value)


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, Mapping) or isinstance(value, (str, bytes, SourceFile, Symbol, Edge)):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class CodeGraphProjectionResult:
    scope: CodeGraphScope
    source_digest: str
    files: tuple[SourceFile, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    edges: tuple[Edge, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {"source_files": len(self.files), "symbols": len(self.symbols), "edges": len(self.edges)}

    @property
    def digest(self) -> str:
        return stable_digest({"source_digest": self.source_digest, "files": [item.to_dict() for item in self.files], "symbols": [item.to_dict() for item in self.symbols], "edges": [item.to_dict() for item in self.edges]})

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope.to_dict(), "source_digest": self.source_digest, "projection_digest": self.digest, "counts": self.counts, "files": [item.to_dict() for item in self.files], "symbols": [item.to_dict() for item in self.symbols], "edges": [item.to_dict() for item in self.edges]}


class CodeGraphProjector:
    """Build one graph revision without copying source bytes."""

    def __init__(self, store: CodeGraphStore) -> None:
        if not isinstance(store, CodeGraphStore):
            raise TypeError("CodeGraphProjector requires CodeGraphStore")
        self.store = store

    def project(
        self,
        files: Sequence[SourceFile | Mapping[str, Any]] | SourceFile | Mapping[str, Any],
        *,
        scope: CodeGraphScope | Mapping[str, Any] | None = None,
        symbols: Sequence[Symbol | Mapping[str, Any]] = (),
        edges: Sequence[Edge | Mapping[str, Any]] = (),
        source_digest: str = "",
        fail_at: str | None = None,
    ) -> CodeGraphProjectionResult:
        checked_scope = CodeGraphScope.from_value(scope) if not isinstance(scope, CodeGraphScope) else scope
        file_values = _as_sequence(files)
        symbol_values = _as_sequence(symbols)
        edge_values = _as_sequence(edges)
        for value in (*file_values, *symbol_values, *edge_values):
            _reject_body(value)
        if not file_values:
            raise CodeGraphError("codegraph projection requires at least one source file")
        # Fault injection is evaluated before the first write.  This keeps a
        # failed shadow projection all-or-nothing even though the public store
        # methods are independently transactional.
        if fail_at in {"after_file", "after_files", "after_edges"}:
            raise CodeGraphError(f"injected codegraph projection failure at {fail_at}")
        projected_files: list[SourceFile] = []
        projected_symbols: list[Symbol] = []
        # First establish all file heads and symbols.  Edges are inserted only
        # afterwards so cross-file references are valid regardless of order.
        for raw_file in file_values:
            file_data = raw_file if isinstance(raw_file, SourceFile) else dict(raw_file)
            file_symbol_values: list[Any] = []
            file_key = str(file_data.get("file_id") or file_data.get("path") or file_data.get("relative_path") or "")
            for raw_symbol in symbol_values:
                mapping = raw_symbol.to_dict() if isinstance(raw_symbol, Symbol) else raw_symbol
                if not isinstance(mapping, Mapping):
                    continue
                target = str(mapping.get("file_id") or mapping.get("path") or mapping.get("file") or "")
                if target and target in {file_key, str(file_data.get("path") or file_data.get("relative_path") or "")}:
                    file_symbol_values.append(raw_symbol)
            projected = self.store.upsert_source_file(file_data, scope=checked_scope, symbols=file_symbol_values, fail_at=fail_at)
            projected_files.append(projected)
            if file_symbol_values:
                projected_symbols.extend(self.store.get_symbols(projected.file_id, scope=checked_scope, revision_id=projected.revision_id))
        projected_edges = self.store.put_edges(edge_values, scope=checked_scope) if edge_values else ()
        if not source_digest:
            source_digest = stable_digest({"files": [item.to_dict() for item in projected_files], "symbols": [item.to_dict() for item in projected_symbols], "edges": [item.to_dict() for item in projected_edges]})
        return CodeGraphProjectionResult(checked_scope, str(source_digest), tuple(projected_files), tuple(projected_symbols), tuple(projected_edges))

    build = project
    project_files = project


PersistentCodeGraphProjector = CodeGraphProjector


__all__ = ["CodeGraphProjectionResult", "CodeGraphProjector", "PersistentCodeGraphProjector"]
