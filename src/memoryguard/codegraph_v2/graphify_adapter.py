"""Safe Graphify metadata export adapter for the V2 CodeGraph plane.

The adapter deliberately does not accept Graphify's raw ``graph.json`` because
that format may contain absolute ``source_file`` paths and installation-specific
node IDs.  Instead Graphify exports a bounded metadata envelope whose paths are
workspace-relative and whose file hashes bind every structural row.  External
Graphify IDs are used only while resolving one import and are replaced with
MemoryGuard revision-scoped stable IDs before persistence.

No source body, code snippet, transcript, command, credential, authority, or
untrusted ACL field is accepted by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re
import sqlite3
from typing import Any, Mapping, Sequence

from ..graphify_core import (
    CORE_VERSION as _GRAPHIFY_CORE_VERSION,
    EXPORT_FORMAT as _CORE_EXPORT_FORMAT,
    export_repository as _graphify_export_repository,
)

from .models import (
    CodeGraphError,
    CodeGraphScope,
    Edge,
    SourceFile,
    Symbol,
    normalize_provenance,
    stable_digest,
    stable_id,
    validate_metadata,
)
from .store import CodeGraphStore, normalize_relative_path


EXPORT_FORMAT = _CORE_EXPORT_FORMAT
MAX_FILES = 20_000
MAX_NODES = 500_000
MAX_EDGES = 1_000_000
MAX_LABEL_BYTES = 2_048

_FORBIDDEN_KEYS = frozenset(
    {
        "body", "text", "raw", "raw_content", "content", "source_text",
        "source_body", "code", "snippet", "transcript", "full_transcript",
        "command", "stdout", "stderr", "secret", "token", "password",
        "api_key", "credential", "authority", "owner", "admin", "acl",
        "capability",
    }
)
_ALLOWED_TOP = frozenset(
    {"format", "complete", "graphify_version", "built_at_commit", "source_digest", "files", "source_files", "nodes", "edges", "links", "diagnostics"}
)
_LINE_RE = re.compile(r"^L(?P<start>\d+)(?:[-:](?:L)?(?P<end>\d+))?$")


class GraphifyExportError(CodeGraphError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "graphify_export_invalid")
        super().__init__(self.code)


class GraphifyCapabilityError(GraphifyExportError):
    """Graphify is missing or its export does not satisfy this contract."""


@dataclass(frozen=True)
class GraphifyCapability:
    available: bool
    version: str = ""
    executable: str = ""
    metadata_export: bool = False
    code: str = ""

    @classmethod
    def detect(cls) -> "GraphifyCapability":
        # Graphify Core ships inside MemoryGuard.  Capability no longer depends
        # on PATH, a separately installed graphifyy distribution, or a CLI
        # subprocess.  Keep this legacy-shaped DTO so existing CodeGraph status
        # and callers do not need a second capability contract.
        return cls(
            available=callable(_graphify_export_repository),
            version=_GRAPHIFY_CORE_VERSION,
            executable="",
            metadata_export=callable(_graphify_export_repository),
            code="" if callable(_graphify_export_repository) else "graphify_core_unavailable",
        )

    def to_dict(self) -> dict[str, Any]:
        return {"available": self.available, "version": self.version, "metadata_export": self.metadata_export, "code": self.code}


def _bounded_text(value: Any, *, field: str, required: bool = False, limit: int = MAX_LABEL_BYTES) -> str:
    if value is None:
        value = ""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise GraphifyExportError(f"graphify_{field}_invalid")
    text = str(value).strip()
    if required and not text:
        raise GraphifyExportError(f"graphify_{field}_required")
    if "\x00" in text or "\r" in text or "\n" in text or len(text.encode("utf-8")) > limit:
        raise GraphifyExportError(f"graphify_{field}_invalid")
    return text


def _reject_sensitive(value: Any, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).strip().casefold().replace("-", "_")
            if name in _FORBIDDEN_KEYS:
                raise GraphifyCapabilityError("graphify_source_body_forbidden")
            _reject_sensitive(child, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_sensitive(child, path=f"{path}[{index}]")
    elif isinstance(value, bytes):
        raise GraphifyCapabilityError("graphify_source_body_forbidden")


def _list(value: Any, *, field: str, maximum: int) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise GraphifyExportError(f"graphify_{field}_invalid")
    if any(not isinstance(item, Mapping) for item in value):
        raise GraphifyExportError(f"graphify_{field}_invalid")
    return [dict(item) for item in value]


def _line_range(value: Any, source_map: Mapping[str, Any]) -> tuple[int, int]:
    start = source_map.get("line_start", source_map.get("start_line", 0))
    end = source_map.get("line_end", source_map.get("end_line", 0))
    try:
        start_i, end_i = int(start or 0), int(end or 0)
    except (TypeError, ValueError) as exc:
        raise GraphifyExportError("graphify_source_map_invalid") from exc
    if not start_i and isinstance(value, str):
        match = _LINE_RE.match(value.strip())
        if match:
            start_i = int(match.group("start"))
            end_i = int(match.group("end") or start_i)
    if start_i < 0 or end_i < 0 or (start_i and end_i and end_i < start_i):
        raise GraphifyExportError("graphify_source_map_invalid")
    return start_i, end_i


def _merge_metadata(raw: Any, *, semantic_kind: str = "", external_id: str = "") -> dict[str, Any]:
    if raw is None:
        result: dict[str, Any] = {}
    elif isinstance(raw, Mapping):
        result = dict(raw)
    else:
        raise GraphifyExportError("graphify_metadata_invalid")
    _reject_sensitive(result)
    if semantic_kind:
        result.setdefault("semantic_kind", semantic_kind)
    if external_id:
        # Never persist Graphify's path-derived external identifier.  Keep only
        # a digest for deterministic troubleshooting/import replay.
        result.setdefault("external_id_digest", hashlib.sha256(external_id.encode("utf-8")).hexdigest())
    validate_metadata(result)
    return result


@dataclass(frozen=True)
class GraphifyImportResult:
    source_digest: str
    files: tuple[SourceFile, ...]
    symbols: tuple[Symbol, ...]
    edges: tuple[Edge, ...]
    tombstoned_files: int
    graphify_version: str
    projection_digest: str

    @property
    def counts(self) -> dict[str, int]:
        return {"source_files": len(self.files), "symbols": len(self.symbols), "edges": len(self.edges)}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_digest": self.source_digest,
            "counts": self.counts,
            "tombstoned_files": self.tombstoned_files,
            "graphify_version": self.graphify_version,
            "projection_digest": self.projection_digest,
        }


class GraphifyExportAdapter:
    """Project one body-free Graphify metadata export into ``CodeGraphStore``."""

    def __init__(self, store: CodeGraphStore) -> None:
        if not isinstance(store, CodeGraphStore):
            raise TypeError("GraphifyExportAdapter requires CodeGraphStore")
        self.store = store

    @staticmethod
    def _file_key(item: Mapping[str, Any]) -> tuple[str, str]:
        path = normalize_relative_path(_bounded_text(item.get("path") or item.get("relative_path"), field="file_path", required=True, limit=4096))
        external = _bounded_text(item.get("id") or item.get("file_id") or path, field="file_id", required=True, limit=8192)
        return external, path

    @staticmethod
    def _source_ref(node: Mapping[str, Any]) -> str:
        value = node.get("file") or node.get("file_id") or node.get("source_file") or node.get("path") or node.get("relative_path")
        return _bounded_text(value, field="node_file", required=True, limit=8192)

    def _project_atomic(
        self,
        files: Sequence[Mapping[str, Any]],
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
        *,
        scope: CodeGraphScope,
        full_snapshot: bool,
        graphify_version: str,
        source_digest: str,
    ) -> GraphifyImportResult:
        """Apply Graphify export as one DB transaction.

        Graphify IDs are request-local.  The store returns the canonical row
        selected by its DB identity, then every external endpoint is rebound
        to that ID before edge insertion.
        """

        file_by_external: dict[str, SourceFile] = {}
        file_by_path: dict[str, SourceFile] = {}
        received_paths: set[str] = set()
        symbol_values_by_file: dict[str, list[dict[str, Any]]] = {}
        symbol_external_ids_by_file: dict[str, list[str]] = {}
        external_to_input_symbol: dict[str, str] = {}
        external_to_symbol: dict[str, str] = {}
        persisted_symbols: dict[str, Symbol] = {}
        symbol_revision: dict[str, str] = {}
        persisted_edges: tuple[Edge, ...] = ()
        tombstoned = 0

        try:
            with self.store._write_transaction(scope) as (conn, checked_scope, scope_id, now):
                for raw in files:
                    external, path = self._file_key(raw)
                    if external in file_by_external or path in file_by_path:
                        raise GraphifyExportError("graphify_duplicate_file")
                    try:
                        content_hash = _bounded_text(raw.get("content_hash") or raw.get("hash"), field="content_hash", required=True, limit=256)
                        language = _bounded_text(raw.get("language"), field="language", limit=64)
                        raw_provenance = _bounded_text(raw.get("provenance"), field="provenance", required=True, limit=32)
                        raw_source_role = _bounded_text(raw.get("source_role") or raw_provenance, field="source_role", required=True, limit=32)
                        provenance = normalize_provenance(raw_provenance)
                        source_role = normalize_provenance(raw_source_role, field_name="source_role")
                    except (ValueError, GraphifyExportError) as exc:
                        raise GraphifyCapabilityError("graphify export requires file hash/provenance metadata") from exc
                    persisted = self.store._upsert_source_file_conn(
                        conn,
                        checked_scope,
                        path_value=path,
                        content_hash=content_hash,
                        source_revision=_bounded_text(raw.get("source_revision"), field="source_revision", limit=256),
                        language=language,
                        source_id="graphify:" + hashlib.sha256(external.encode("utf-8")).hexdigest(),
                        source_role=source_role,
                        provenance=provenance,
                        now=now,
                    )
                    file_by_external[external] = persisted
                    file_by_path[path] = persisted
                    received_paths.add(path)

                for raw in nodes:
                    external = _bounded_text(raw.get("id") or raw.get("node_id"), field="node_id", required=True, limit=8192)
                    if external in external_to_input_symbol:
                        raise GraphifyExportError("graphify_duplicate_node")
                    file_ref = self._source_ref(raw)
                    persisted = file_by_external.get(file_ref)
                    if persisted is None:
                        try:
                            persisted = file_by_path.get(normalize_relative_path(file_ref))
                        except Exception:
                            persisted = None
                    if persisted is None:
                        raise GraphifyExportError("graphify_node_file_unknown")
                    kind = _bounded_text(raw.get("kind") or raw.get("type") or raw.get("file_type") or "symbol", field="node_kind", required=True, limit=128)
                    is_file_node = kind in {"file", "code"} and (raw.get("file_type") == "code" or raw.get("kind") == "file" or raw.get("type") == "file")
                    if is_file_node:
                        symbol_hash = stable_digest((persisted.path, persisted.content_hash, "file"))
                        symbol_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, "file-anchor")
                        value = {
                            "symbol_id": symbol_id,
                            "name": PurePosixPath(persisted.path).name,
                            "kind": "file",
                            "signature": "",
                            "symbol_hash": symbol_hash,
                            "line_start": 1,
                            "line_end": 1,
                            "provenance": persisted.provenance,
                            "source_map": {"path": persisted.path, "line_start": 1, "line_end": 1},
                            "metadata": _merge_metadata(raw.get("metadata"), semantic_kind="file", external_id=external),
                        }
                    else:
                        provenance = normalize_provenance(raw.get("provenance") or persisted.provenance)
                        if provenance != persisted.provenance:
                            raise GraphifyCapabilityError("graphify node provenance must inherit its source file")
                        source_map = dict(raw.get("source_map") or {}) if isinstance(raw.get("source_map") or {}, Mapping) else None
                        if source_map is None:
                            raise GraphifyExportError("graphify_source_map_invalid")
                        source_map.setdefault("path", persisted.path)
                        source_location = _bounded_text(raw.get("source_location"), field="source_location", limit=128)
                        line_start, line_end = _line_range(source_location, source_map)
                        source_map["line_start"], source_map["line_end"] = line_start, line_end
                        source_map["path"] = normalize_relative_path(str(source_map.get("path") or persisted.path))
                        if source_map["path"] != persisted.path:
                            raise GraphifyExportError("graphify_source_map_file_mismatch")
                        name = _bounded_text(raw.get("name") or raw.get("label") or external, field="node_name", required=True)
                        signature = _bounded_text(raw.get("signature"), field="signature", limit=4096)
                        semantic_kind = _bounded_text(raw.get("semantic_kind") or raw.get("context") or kind, field="semantic_kind", limit=128)
                        symbol_hash = _bounded_text(raw.get("symbol_hash") or raw.get("hash") or stable_digest((name, kind, signature, source_map)), field="symbol_hash", required=True, limit=256)
                        symbol_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, name, kind, symbol_hash, line_start, line_end)
                        value = {
                            "symbol_id": symbol_id,
                            "name": name,
                            "kind": kind,
                            "signature": signature,
                            "symbol_hash": symbol_hash,
                            "line_start": line_start,
                            "line_end": line_end,
                            "provenance": provenance,
                            "source_map": source_map,
                            "metadata": _merge_metadata(raw.get("metadata"), semantic_kind=semantic_kind, external_id=external),
                        }
                    external_to_input_symbol[external] = symbol_id
                    symbol_values_by_file.setdefault(persisted.file_id, []).append(value)
                    symbol_external_ids_by_file.setdefault(persisted.file_id, []).append(external)

                # A file anchor is materialized only for an edge endpoint that
                # names a file but has no explicit Graphify node.
                referenced_file_ids: set[str] = set()
                for raw in edges:
                    for key in ("source", "target", "from", "to", "from_id", "to_id"):
                        if raw.get(key) is not None:
                            referenced_file_ids.add(_bounded_text(raw.get(key), field="edge_endpoint", limit=8192))
                for external in sorted(referenced_file_ids):
                    if external in external_to_input_symbol or external not in file_by_external:
                        continue
                    persisted = file_by_external[external]
                    symbol_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, "file-anchor")
                    external_to_input_symbol[external] = symbol_id
                    symbol_values_by_file.setdefault(persisted.file_id, []).append(
                        {
                            "symbol_id": symbol_id,
                            "name": PurePosixPath(persisted.path).name,
                            "kind": "file",
                            "signature": "",
                            "symbol_hash": stable_digest((persisted.path, persisted.content_hash, "file")),
                            "line_start": 1,
                            "line_end": 1,
                            "provenance": persisted.provenance,
                            "source_map": {"path": persisted.path, "line_start": 1, "line_end": 1},
                            "metadata": _merge_metadata({}, semantic_kind="file", external_id=external),
                        }
                    )
                    symbol_external_ids_by_file.setdefault(persisted.file_id, []).append(external)

                if full_snapshot:
                    # A complete extraction is authoritative even when source
                    # bytes (and therefore revision_id) did not change.  Engine
                    # upgrades may change the symbol set for the same immutable
                    # file revision; deactivate the prior head transactionally
                    # and let canonical symbols/edges below reactivate when the
                    # exact identity is still present.
                    for persisted in file_by_path.values():
                        conn.execute(
                            "UPDATE edges SET active=0 WHERE scope_id=? AND active=1 AND ("
                            "from_id IN (SELECT symbol_id FROM symbols WHERE file_id=? AND scope_id=? AND active=1) "
                            "OR to_id IN (SELECT symbol_id FROM symbols WHERE file_id=? AND scope_id=? AND active=1))",
                            (scope_id, persisted.file_id, scope_id, persisted.file_id, scope_id),
                        )
                        conn.execute(
                            "UPDATE symbols SET active=0 WHERE file_id=? AND scope_id=? AND active=1",
                            (persisted.file_id, scope_id),
                        )

                for persisted in sorted(file_by_path.values(), key=lambda item: item.path):
                    values = symbol_values_by_file.get(persisted.file_id, [])
                    if not values:
                        continue
                    paired = sorted(
                        zip(values, symbol_external_ids_by_file[persisted.file_id]),
                        key=lambda item: (
                            str(item[0].get("name") or ""),
                            str(item[0].get("kind") or ""),
                            str(item[0].get("symbol_hash") or ""),
                            int(item[0].get("line_start") or 0),
                            int(item[0].get("line_end") or 0),
                            str(item[1]),
                        ),
                    )
                    persisted_values = self.store._put_symbols_conn(
                        conn,
                        persisted.file_id,
                        [item[0] for item in paired],
                        checked_scope=checked_scope,
                        revision_id=persisted.revision_id,
                        external_ids=[item[1] for item in paired],
                        now=now,
                    )
                    for (_, external), symbol in zip(paired, persisted_values):
                        external_to_symbol[external] = symbol.symbol_id
                        persisted_symbols[symbol.symbol_id] = symbol
                        symbol_revision[symbol.symbol_id] = symbol.revision_id

                edge_values: list[dict[str, Any]] = []
                seen_edge_identity: set[tuple[str, ...]] = set()
                for raw in edges:
                    source_external = _bounded_text(raw.get("source") or raw.get("from") or raw.get("from_id"), field="edge_source", required=True, limit=8192)
                    target_external = _bounded_text(raw.get("target") or raw.get("to") or raw.get("to_id"), field="edge_target", required=True, limit=8192)
                    from_id = external_to_symbol.get(source_external)
                    to_id = external_to_symbol.get(target_external)
                    if not from_id or not to_id:
                        raise GraphifyExportError("graphify_edge_endpoint_unknown")
                    relation = _bounded_text(raw.get("relation") or raw.get("type") or "related", field="edge_relation", required=True, limit=128)
                    context = _bounded_text(raw.get("context"), field="edge_context", limit=256)
                    provenance = normalize_provenance(_bounded_text(raw.get("provenance"), field="edge_provenance", required=True, limit=32))
                    source_symbol = persisted_symbols.get(from_id)
                    if source_symbol is None or provenance != source_symbol.provenance:
                        raise GraphifyCapabilityError("graphify edge provenance must inherit its source node")
                    source_location = _bounded_text(raw.get("source_location"), field="source_location", limit=128)
                    identity = (from_id, to_id, relation, context, provenance, source_location)
                    if identity in seen_edge_identity:
                        continue
                    seen_edge_identity.add(identity)
                    metadata = _merge_metadata(
                        raw.get("metadata"),
                        semantic_kind=_bounded_text(raw.get("semantic_kind") or context, field="semantic_kind", limit=128),
                        external_id=_bounded_text(raw.get("id") or raw.get("edge_id"), field="edge_id", limit=8192),
                    )
                    confidence = _bounded_text(raw.get("confidence"), field="confidence", limit=64)
                    if confidence:
                        metadata.setdefault("confidence", confidence)
                    edge_values.append(
                        {
                            "from_id": from_id,
                            "to_id": to_id,
                            "relation": relation,
                            "context": context,
                            "provenance": provenance,
                            "source_location": source_location,
                            "metadata": metadata,
                            "weight": float(raw.get("weight", 1.0)),
                            "revision_id": symbol_revision.get(from_id, ""),
                        }
                    )
                if edge_values:
                    persisted_edges = self.store._put_edges_conn(conn, edge_values, checked_scope=checked_scope, now=now)

                if full_snapshot:
                    for existing in self.store._list_source_files_conn(conn, checked_scope):
                        if existing.path in received_paths:
                            continue
                        _tombstone_id, changed = self.store._tombstone_source_file_conn(
                            conn,
                            checked_scope,
                            existing.file_id,
                            reason="graphify_snapshot_removed",
                            now=now,
                        )
                        tombstoned += int(changed)

                if not source_digest:
                    source_digest = stable_digest(
                        {
                            "format": EXPORT_FORMAT,
                            "files": [(item.path, item.content_hash, item.revision_id, item.provenance) for item in sorted(file_by_path.values(), key=lambda value: value.path)],
                            "symbols": sorted(persisted_symbols),
                            "edges": sorted(item.edge_id for item in persisted_edges),
                        }
                    )
                projection_digest = stable_digest(
                    {
                        "source_digest": source_digest,
                        "files": sorted(received_paths),
                        "symbols": sorted(persisted_symbols),
                        "edges": sorted(item.edge_id for item in persisted_edges),
                        "tombstoned": tombstoned,
                    }
                )
        except (GraphifyExportError, GraphifyCapabilityError):
            raise
        except sqlite3.IntegrityError as exc:
            raise GraphifyExportError("graphify_database_integrity_error") from exc
        except CodeGraphError as exc:
            raise GraphifyExportError("graphify_projection_error") from exc

        return GraphifyImportResult(
            source_digest=source_digest,
            files=tuple(sorted(file_by_path.values(), key=lambda item: item.path)),
            symbols=tuple(sorted(persisted_symbols.values(), key=lambda item: item.symbol_id)),
            edges=tuple(sorted(persisted_edges, key=lambda item: item.edge_id)),
            tombstoned_files=tombstoned,
            graphify_version=graphify_version,
            projection_digest=projection_digest,
        )

    def project(
        self,
        payload: Mapping[str, Any],
        *,
        scope: CodeGraphScope,
        full_snapshot: bool | None = None,
    ) -> GraphifyImportResult:
        if not isinstance(payload, Mapping):
            raise GraphifyExportError("graphify_export_object_required")
        unknown = set(str(key) for key in payload) - _ALLOWED_TOP
        if unknown:
            raise GraphifyExportError("graphify_export_unknown_field")
        _reject_sensitive(payload)
        format_name = str(payload.get("format") or "").strip()
        if format_name != EXPORT_FORMAT:
            raise GraphifyCapabilityError("graphify_export_format_unsupported")
        if not isinstance(scope, CodeGraphScope) or not scope.trusted_context:
            raise GraphifyExportError("graphify_trusted_scope_required")
        files = _list(payload.get("files") if "files" in payload else payload.get("source_files"), field="files", maximum=MAX_FILES)
        nodes = _list(payload.get("nodes"), field="nodes", maximum=MAX_NODES)
        edges = _list(payload.get("edges") if "edges" in payload else payload.get("links"), field="edges", maximum=MAX_EDGES)
        if not files:
            raise GraphifyCapabilityError("graphify_export_requires_file_metadata")
        complete = payload.get("complete") is True
        if full_snapshot is None:
            full_snapshot = complete
        if full_snapshot and not complete:
            raise GraphifyExportError("graphify_complete_snapshot_required")
        if full_snapshot and not files:
            # A tool failure must never look like an authoritative empty repo.
            raise GraphifyExportError("graphify_empty_snapshot_rejected")

        graphify_version = _bounded_text(payload.get("graphify_version"), field="version", limit=64)
        source_digest = _bounded_text(payload.get("source_digest"), field="source_digest", limit=256)

        return self._project_atomic(
            files,
            nodes,
            edges,
            scope=scope,
            full_snapshot=bool(full_snapshot),
            graphify_version=graphify_version,
            source_digest=source_digest,
        )

        file_by_external: dict[str, SourceFile] = {}
        file_by_path: dict[str, SourceFile] = {}
        file_external_by_path: dict[str, str] = {}
        received_paths: set[str] = set()
        file_node_external_ids: set[str] = set()
        symbol_values_by_file: dict[str, list[dict[str, Any]]] = {}
        external_to_symbol: dict[str, str] = {}
        symbol_revision: dict[str, str] = {}

        # Phase 1: establish immutable file revisions without accepting source text.
        for raw in files:
            external, path = self._file_key(raw)
            if external in file_by_external or path in file_by_path:
                raise GraphifyExportError("graphify_duplicate_file")
            try:
                content_hash = _bounded_text(raw.get("content_hash") or raw.get("hash"), field="content_hash", required=True, limit=256)
                language = _bounded_text(raw.get("language"), field="language", limit=64)
                raw_provenance = _bounded_text(raw.get("provenance"), field="provenance", required=True, limit=32)
                raw_source_role = _bounded_text(raw.get("source_role") or raw_provenance, field="source_role", required=True, limit=32)
                provenance = normalize_provenance(raw_provenance)
                source_role = normalize_provenance(raw_source_role, field_name="source_role")
            except (ValueError, GraphifyExportError) as exc:
                raise GraphifyCapabilityError("graphify export requires file hash/provenance metadata") from exc
            persisted = self.store.upsert_source_file(
                path,
                content_hash,
                scope=scope,
                source_revision=_bounded_text(raw.get("source_revision"), field="source_revision", limit=256),
                language=language,
                source_id="graphify:" + hashlib.sha256(external.encode("utf-8")).hexdigest(),
                source_role=source_role,
                provenance=provenance,
            )
            file_by_external[external] = persisted
            file_by_path[path] = persisted
            file_external_by_path[path] = external
            received_paths.add(path)

        # Phase 2: map Graphify nodes to revision-scoped MemoryGuard IDs.
        for raw in nodes:
            external = _bounded_text(raw.get("id") or raw.get("node_id"), field="node_id", required=True, limit=8192)
            file_ref = self._source_ref(raw)
            persisted = file_by_external.get(file_ref)
            if persisted is None:
                try:
                    persisted = file_by_path.get(normalize_relative_path(file_ref))
                except Exception:
                    persisted = None
            if persisted is None:
                raise GraphifyExportError("graphify_node_file_unknown")
            kind = _bounded_text(raw.get("kind") or raw.get("type") or raw.get("file_type") or "symbol", field="node_kind", required=True, limit=128)
            is_file_node = kind in {"file", "code"} and (raw.get("file_type") == "code" or raw.get("kind") == "file" or raw.get("type") == "file")
            if is_file_node:
                anchor_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, "file-anchor")
                external_to_symbol[external] = anchor_id
                file_node_external_ids.add(external)
                symbol_revision[anchor_id] = persisted.revision_id
                symbol_values_by_file.setdefault(persisted.file_id, []).append(
                    {"symbol_id": anchor_id, "name": PurePosixPath(persisted.path).name, "kind": "file", "signature": "", "symbol_hash": stable_digest((persisted.path, persisted.content_hash, "file")), "line_start": 1, "line_end": 1, "provenance": persisted.provenance, "source_map": {"path": persisted.path, "line_start": 1, "line_end": 1}, "metadata": _merge_metadata(raw.get("metadata"), semantic_kind="file", external_id=external)}
                )
                continue
            symbol_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, hashlib.sha256(external.encode("utf-8")).hexdigest())
            if external in external_to_symbol and external_to_symbol[external] != symbol_id:
                raise GraphifyExportError("graphify_duplicate_node")
            provenance = normalize_provenance(raw.get("provenance") or persisted.provenance)
            if provenance != persisted.provenance:
                raise GraphifyCapabilityError("graphify node provenance must inherit its source file")
            source_map = dict(raw.get("source_map") or {}) if isinstance(raw.get("source_map") or {}, Mapping) else None
            if source_map is None:
                raise GraphifyExportError("graphify_source_map_invalid")
            source_map.setdefault("path", persisted.path)
            source_location = _bounded_text(raw.get("source_location"), field="source_location", limit=128)
            line_start, line_end = _line_range(source_location, source_map)
            source_map["line_start"], source_map["line_end"] = line_start, line_end
            source_map["path"] = normalize_relative_path(str(source_map.get("path") or persisted.path))
            if source_map["path"] != persisted.path:
                raise GraphifyExportError("graphify_source_map_file_mismatch")
            name = _bounded_text(raw.get("name") or raw.get("label") or external, field="node_name", required=True)
            signature = _bounded_text(raw.get("signature"), field="signature", limit=4096)
            semantic_kind = _bounded_text(raw.get("semantic_kind") or raw.get("context") or kind, field="semantic_kind", limit=128)
            metadata = _merge_metadata(raw.get("metadata"), semantic_kind=semantic_kind, external_id=external)
            symbol_values_by_file.setdefault(persisted.file_id, []).append(
                {
                    "symbol_id": symbol_id,
                    "name": name,
                    "kind": kind,
                    "signature": signature,
                    "symbol_hash": _bounded_text(raw.get("symbol_hash") or raw.get("hash") or stable_digest((name, kind, signature, source_map)), field="symbol_hash", required=True, limit=256),
                    "line_start": line_start,
                    "line_end": line_end,
                    "provenance": provenance,
                    "source_map": source_map,
                    "metadata": metadata,
                }
            )
            external_to_symbol[external] = symbol_id
            symbol_revision[symbol_id] = persisted.revision_id

        # Create a file anchor only if an edge explicitly references a file ID
        # that did not appear as a node. This preserves Graphify semantics
        # without inventing one extra node per source file.
        referenced_file_ids: set[str] = set()
        for raw in edges:
            for key in ("source", "target", "from", "to", "from_id", "to_id"):
                value = raw.get(key)
                if value is not None:
                    referenced_file_ids.add(_bounded_text(value, field="edge_endpoint", limit=8192))
        for external in sorted(referenced_file_ids):
            if external in external_to_symbol or external not in file_by_external:
                continue
            persisted = file_by_external[external]
            anchor_id = stable_id("graphify-symbol", persisted.file_id, persisted.revision_id, "file-anchor")
            external_to_symbol[external] = anchor_id
            symbol_revision[anchor_id] = persisted.revision_id
            symbol_values_by_file.setdefault(persisted.file_id, []).append(
                {"symbol_id": anchor_id, "name": PurePosixPath(persisted.path).name, "kind": "file", "signature": "", "symbol_hash": stable_digest((persisted.path, persisted.content_hash, "file")), "line_start": 1, "line_end": 1, "provenance": persisted.provenance, "source_map": {"path": persisted.path, "line_start": 1, "line_end": 1}, "metadata": _merge_metadata({}, semantic_kind="file", external_id=external)}
            )

        persisted_symbols: dict[str, Symbol] = {}
        for persisted in file_by_path.values():
            values = symbol_values_by_file.get(persisted.file_id, [])
            if not values:
                continue
            for symbol in self.store.put_symbols(persisted.file_id, values, scope=scope, revision_id=persisted.revision_id):
                persisted_symbols[symbol.symbol_id] = symbol
                symbol_revision[symbol.symbol_id] = symbol.revision_id

        # Phase 3: all endpoints must resolve. The export producer, not this
        # store, is responsible for excluding external/dangling dependency stubs.
        edge_values: list[dict[str, Any]] = []
        seen_edge_identity: set[tuple[str, ...]] = set()
        for raw in edges:
            source_external = _bounded_text(raw.get("source") or raw.get("from") or raw.get("from_id"), field="edge_source", required=True, limit=8192)
            target_external = _bounded_text(raw.get("target") or raw.get("to") or raw.get("to_id"), field="edge_target", required=True, limit=8192)
            from_id = external_to_symbol.get(source_external)
            to_id = external_to_symbol.get(target_external)
            if not from_id or not to_id:
                raise GraphifyExportError("graphify_edge_endpoint_unknown")
            relation = _bounded_text(raw.get("relation") or raw.get("type") or "related", field="edge_relation", required=True, limit=128)
            context = _bounded_text(raw.get("context"), field="edge_context", limit=256)
            provenance = normalize_provenance(_bounded_text(raw.get("provenance"), field="edge_provenance", required=True, limit=32))
            source_symbol = persisted_symbols.get(from_id)
            if source_symbol is None or provenance != source_symbol.provenance:
                raise GraphifyCapabilityError("graphify edge provenance must inherit its source node")
            source_location = _bounded_text(raw.get("source_location"), field="source_location", limit=128)
            identity = (from_id, to_id, relation, context, provenance, source_location)
            if identity in seen_edge_identity:
                continue
            seen_edge_identity.add(identity)
            metadata = _merge_metadata(raw.get("metadata"), semantic_kind=_bounded_text(raw.get("semantic_kind") or context, field="semantic_kind", limit=128), external_id=_bounded_text(raw.get("id") or raw.get("edge_id"), field="edge_id", limit=8192))
            confidence = _bounded_text(raw.get("confidence"), field="confidence", limit=64)
            if confidence:
                metadata.setdefault("confidence", confidence)
            edge_values.append(
                {
                    "from_id": from_id,
                    "to_id": to_id,
                    "relation": relation,
                    "context": context,
                    "provenance": provenance,
                    "source_location": source_location,
                    "metadata": metadata,
                    "weight": float(raw.get("weight", 1.0)),
                    "revision_id": symbol_revision.get(from_id, ""),
                }
            )
        persisted_edges = self.store.put_edges(edge_values, scope=scope) if edge_values else ()

        tombstoned = 0
        if full_snapshot:
            for existing in self.store.list_source_files(scope=scope):
                if existing.path in received_paths:
                    continue
                self.store.tombstone_source_file(existing.file_id, scope=scope, reason="graphify_snapshot_removed")
                tombstoned += 1

        if not source_digest:
            source_digest = stable_digest(
                {
                    "format": EXPORT_FORMAT,
                    "files": [(item.path, item.content_hash, item.revision_id, item.provenance) for item in sorted(file_by_path.values(), key=lambda value: value.path)],
                    "symbols": sorted(persisted_symbols),
                    "edges": sorted(item.edge_id for item in persisted_edges),
                }
            )
        projection_digest = stable_digest(
            {
                "source_digest": source_digest,
                "files": sorted(received_paths),
                "symbols": sorted(persisted_symbols),
                "edges": sorted(item.edge_id for item in persisted_edges),
                "tombstoned": tombstoned,
            }
        )
        return GraphifyImportResult(
            source_digest=source_digest,
            files=tuple(sorted(file_by_path.values(), key=lambda item: item.path)),
            symbols=tuple(sorted(persisted_symbols.values(), key=lambda item: item.symbol_id)),
            edges=tuple(sorted(persisted_edges, key=lambda item: item.edge_id)),
            tombstoned_files=tombstoned,
            graphify_version=graphify_version,
            projection_digest=projection_digest,
        )

    import_export = project
    update = project


__all__ = [
    "EXPORT_FORMAT",
    "GraphifyCapability",
    "GraphifyCapabilityError",
    "GraphifyExportAdapter",
    "GraphifyExportError",
    "GraphifyImportResult",
]
