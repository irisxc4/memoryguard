"""Fail-closed, read-only Reference Audit for all V2 authoritative domains."""

from __future__ import annotations

from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from .adapters import CursorError, ReadOnlyAdapterError, SQLiteReadOnlyAdapter, assert_lexical_safe
from .registry import AUTHORITATIVE_DOMAINS, DEFAULT_REGISTRY, DomainRegistry, DomainSpec, ReferenceRule, TableSpec


@dataclass(frozen=True, slots=True)
class Reference:
    source_domain: str
    source_table: str
    source_id: str
    target_domain: str
    target_id: str
    kind: str = "logical"
    source_column: str = ""
    target_table: str = ""
    target_column: str = ""

    @property
    def key(self) -> tuple[str, ...]:
        return (self.source_domain, self.source_table, self.source_id, self.source_column, self.target_domain, self.target_table, self.target_column, self.target_id, self.kind)

    def to_dict(self) -> dict[str, str]:
        return {"source_domain": self.source_domain, "source_table": self.source_table, "source_id": self.source_id, "target_domain": self.target_domain, "target_id": self.target_id, "kind": self.kind, "source_column": self.source_column, "target_table": self.target_table, "target_column": self.target_column}


@dataclass(frozen=True, slots=True)
class Blocker:
    code: str
    domain: str = ""
    message: str = ""
    table: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        return self.message or self.code

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "domain": self.domain, "message": self.message, "table": self.table, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class Page:
    domain: str
    table: str
    rows: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    done: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    domains: tuple[str, ...]
    references: tuple[Reference, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    candidates: tuple[str, ...] = ()
    candidate_intersection: tuple[str, ...] = ()
    epoch_candidates: tuple[tuple[str, ...], ...] = ()
    schema_fingerprints: Mapping[str, str] = field(default_factory=dict)
    registry_digest: str = ""
    manifest_generation: int | None = None
    pages: tuple[Page, ...] = ()
    sweep: Mapping[str, Any] = field(default_factory=lambda: {"capability": False, "reason": "hold_first_not_proven", "deleted": 0})

    @property
    def blocked(self) -> bool:
        return self.status == "BLOCKED" or bool(self.blockers)

    @property
    def physical_deletion(self) -> Mapping[str, Any]:
        return self.sweep

    @property
    def sweep_capability(self) -> bool:
        return bool(self.sweep.get("capability", False))

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "domains": list(self.domains), "references": [r.to_dict() for r in self.references], "blockers": [b.to_dict() for b in self.blockers], "candidates": list(self.candidates), "candidate_intersection": list(self.candidate_intersection), "epoch_candidates": [list(e) for e in self.epoch_candidates], "schema_fingerprints": dict(self.schema_fingerprints), "registry_digest": self.registry_digest, "manifest_generation": self.manifest_generation, "pages": [{"domain": p.domain, "table": p.table, "rows": [dict(r) for r in p.rows], "next_cursor": p.next_cursor, "done": p.done, "fingerprint": p.fingerprint} for p in self.pages], "sweep": dict(self.sweep)}

    def to_public_dict(self) -> dict[str, Any]:
        """Return a stable receipt without authoritative row/reference IDs."""

        candidate_digest = hashlib.sha256("\n".join(sorted(self.candidates)).encode("utf-8")).hexdigest()
        return {
            "status": self.status,
            "blocked": self.blocked,
            "domains": list(self.domains),
            "domain_count": len(self.domains),
            "reference_count": len(self.references),
            "candidate_count": len(self.candidates),
            "candidate_digest": candidate_digest,
            "blocker_codes": sorted({item.code for item in self.blockers}),
            "blockers": [
                {"code": item.code, "domain": item.domain, "table": item.table}
                for item in self.blockers
            ],
            "schema_fingerprints": dict(self.schema_fingerprints),
            "registry_digest": self.registry_digest,
            "manifest_generation": self.manifest_generation,
            "page_count": len(self.pages),
            "sweep": dict(self.sweep),
        }

    public_dict = to_public_dict


@dataclass(frozen=True, slots=True)
class AuditProtocol:
    """Protocol metadata, useful to callers that persist audit receipts."""

    registry_digest: str
    schema_fingerprints: Mapping[str, str]
    manifest_generation: int | None
    read_only: bool = True
    deletion_capability: bool = False


_REF_KEY_RE = re.compile(r"(?:^|_)(?:ref|reference|id|ids)$", re.IGNORECASE)
_KNOWN_JSON_KEYS = frozenset({
    "source_ref", "source_id", "source_revision", "source_object_id", "target_id", "target_ref", "reference_id", "ref_id", "blob_id", "original_definition_id", "source_row_id", "source_path", "asset_id", "version_id", "evidence_id", "skill_id", "run_id", "node_id", "definition_id", "binding_id", "document_id", "namespace_id", "occurrence_id", "projection_id", "aggregate_id", "subject_id", "subject_type", "task_id", "old_id", "new_id", "attempt_id", "migration_id", "id", "ref", "refs", "references", "ids", "source_ids", "target_ids", "evidence_refs", "asset_refs", "metadata", "status", "state", "generation", "digest", "error", "errors", "policy", "scope", "acl", "project_ref", "provider", "agent_instance_id", "share_group_id", "workspace_id", "runtime_role", "owner_agent_id", "owner_id", "actor_id", "rule_id", "memory_id", "event_id", "request_id", "receipt_id", "source_root_id", "source_object_id",
})
_UNCONSUMED = {"pending", "failed", "unconsumed", "queued", "retry", "processing"}
_MIGRATION_BLOCKED = {"blocked", "conflict", "failed", "pending", "unresolved"}
_BASE_MARKER = "memoryguard-v2-phase1"
_METADATA_MARKERS: dict[str, tuple[tuple[str, str, int], ...]] = {
    "runtime": (("schema_meta", "memoryguard-v2-phase1", 1), ("runtime_v2_schema_meta", "1", 1)),
    "memory": (("schema_meta", "memoryguard-v2-phase1", 1), ("memory_schema_meta", "memoryguard-v2-phase2-memory", 1)),
    "rules": (("schema_meta", "memoryguard-v2-phase1", 1), ("rules_schema_meta", "memoryguard-v2-phase2-rules", 2)),
    "evidence": (("schema_meta", "memoryguard-v2-phase1", 1), ("evidence_schema_meta", "memoryguard-v2-phase2-evidence", 1)),
    "content": (("schema_meta", "memoryguard-v2-phase1", 1), ("content_schema_meta", "4", 4)),
    "knowledge": (("schema_meta", "memoryguard-v2-phase1", 1),),
    "codegraph": (("schema_meta", "memoryguard-v2-phase1", 1), ("codegraph_schema_meta", "2", 2)),
    "assets": (("schema_meta", "memoryguard-v2-phase1", 1), ("asset_schema_meta", "1", 1)),
    "scenario": (("schema_meta", "memoryguard-v2-phase1", 1), ("projection_schema_meta", "1", 1)),
    "profile": (("schema_meta", "memoryguard-v2-phase1", 1), ("projection_schema_meta", "1", 1)),
    "system": (("schema_meta", "memoryguard-v2-phase1", 1), ("gui_control_schema_meta", "1", 1)),
    "skills": (("schema_meta", "memoryguard-v2-phase5-skills", 1),),
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _json_load(raw: Any) -> Any:
    if raw in (None, ""):
        return {}
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode("utf-8")
    return json.loads(str(raw))


def _public_row(row: Mapping[str, Any]) -> Mapping[str, str]:
    """Expose page cardinality without leaking authoritative row bodies/IDs."""

    digest = hashlib.sha256()
    for key in sorted(row):
        value = row[key]
        digest.update(str(key).encode("utf-8"))
        digest.update(b"\0")
        if isinstance(value, (bytes, bytearray, memoryview)):
            digest.update(hashlib.sha256(bytes(value)).digest())
        else:
            digest.update(str(value).encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return {"row_hash": digest.hexdigest()}


class ReferenceAudit:
    """Audit V2 databases without opening any Store or creating directories."""

    def __init__(self, workspace: str | Path, *, registry: DomainRegistry = DEFAULT_REGISTRY, mode: str = "ro", page_size: int = 256) -> None:
        if mode not in {"ro", "readonly", "read_only"}:
            raise ValueError("ReferenceAudit mode must be ro")
        if type(page_size) is not int or not 1 <= page_size <= 10_000:
            raise ValueError("page_size must be between 1 and 10000")
        raw = Path(workspace).expanduser()
        # Reject a symlink workspace lexically before resolve.  We still use an
        # absolute path for deterministic child paths, never a resolved path.
        self.workspace = assert_lexical_safe(raw)
        self.registry = registry
        self.mode = "ro"
        self.page_size = page_size

    def protocol(self, result: Result | None = None) -> AuditProtocol:
        return AuditProtocol(self.registry.digest, {} if result is None else result.schema_fingerprints, None if result is None else result.manifest_generation)

    def _block(self, blockers: list[Blocker], code: str, domain: str = "", message: str = "", table: str = "", **detail: Any) -> None:
        blockers.append(Blocker(code, domain, message or code, table, detail))

    def _validate_schema(self, spec: DomainSpec, adapter: SQLiteReadOnlyAdapter, blockers: list[Blocker]) -> tuple[str, int | None]:
        try:
            snapshot = adapter.schema()
        except (ReadOnlyAdapterError, sqlite3.Error, OSError, ValueError) as exc:
            self._block(blockers, "schema_unreadable", spec.name, str(exc))
            return "", None
        actual = set(snapshot.tables)
        expected = set(spec.tables)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            self._block(blockers, "partial_schema", spec.name, "authoritative tables are missing", detail=missing)
        if unknown:
            self._block(blockers, "unknown_authoritative_table", spec.name, "unknown authoritative table", detail=unknown)
        for table in sorted(actual & expected):
            table_spec = spec.tables[table]
            columns = set(snapshot.tables[table])
            missing_columns = sorted(table_spec.required_columns - columns)
            unknown_columns = sorted(columns - table_spec.columns)
            if missing_columns:
                self._block(blockers, "partial_schema", spec.name, "required columns are missing", table, missing=missing_columns)
            if unknown_columns:
                self._block(blockers, "unknown_authoritative_column", spec.name, "unknown authoritative column", table, unknown=unknown_columns)
        if not snapshot.integrity_check == "ok":
            self._block(blockers, "integrity_check", spec.name, snapshot.integrity_check)
        if snapshot.foreign_key_errors:
            self._block(blockers, "foreign_key_check", spec.name, "foreign key violations", detail={"errors": len(snapshot.foreign_key_errors)})
        self._validate_metadata_markers(spec, adapter, blockers)
        marker_ok = snapshot.marker in spec.marker
        version_values = (spec.version,) if isinstance(spec.version, int) else tuple(spec.version)
        user_versions = tuple(spec.supported_user_versions) or version_values
        if snapshot.marker == "" or not marker_ok or snapshot.user_version not in user_versions or snapshot.version not in version_values:
            code = "future_schema" if snapshot.user_version > max(user_versions) or snapshot.version > max(version_values) else "missing_or_unsupported_marker"
            self._block(blockers, code, spec.name, "schema marker/version is not an exact supported V2 target", detail={"marker": snapshot.marker, "version": snapshot.version, "user_version": snapshot.user_version, "expected_marker": spec.marker, "expected_version": version_values, "expected_user_version": user_versions})
        generation = self._manifest_generation(spec, adapter)
        if spec.name == "system" and generation is None:
            self._block(blockers, "manifest_generation_unavailable", spec.name, "manifest generation is missing or corrupt", "manifest")
        return snapshot.fingerprint, generation

    def _validate_metadata_markers(self, spec: DomainSpec, adapter: SQLiteReadOnlyAdapter, blockers: list[Blocker]) -> None:
        try:
            with adapter.connect() as conn:
                names = set(adapter._table_names(conn))
                for table, expected_marker, expected_version in _METADATA_MARKERS.get(spec.name, ()):
                    if table not in names:
                        self._block(blockers, "partial_schema", spec.name, "required schema metadata table is missing", table)
                        continue
                    columns = set(adapter._columns(conn, table))
                    marker = ""
                    version: int | None = None
                    if {"domain", "version", "marker"} <= columns:
                        row = conn.execute(f'SELECT marker,version FROM "{table}" ORDER BY rowid LIMIT 1').fetchone()
                        if row:
                            marker, version = str(row[0]), int(row[1])
                    elif {"schema_id", "version", "marker"} <= columns:
                        row = conn.execute(f'SELECT marker,version FROM "{table}" ORDER BY rowid LIMIT 1').fetchone()
                        if row:
                            marker, version = str(row[0]), int(row[1])
                    elif {"key", "value"} <= columns:
                        values = {str(row[0]): str(row[1]) for row in conn.execute(f'SELECT key,value FROM "{table}"').fetchall()}
                        marker = values.get("marker", values.get("version", ""))
                        try:
                            version = int(values.get("version", ""))
                        except ValueError:
                            version = None
                    if marker != expected_marker or version != expected_version:
                        self._block(blockers, "metadata_marker_drift", spec.name, "schema metadata marker/version mismatch", table, marker=marker, version=version, expected_marker=expected_marker, expected_version=expected_version)
        except sqlite3.Error as exc:
            self._block(blockers, "schema_metadata_unreadable", spec.name, str(exc))

    def _manifest_generation(self, spec: DomainSpec, adapter: SQLiteReadOnlyAdapter, connection: sqlite3.Connection | None = None) -> int | None:
        if spec.name != "system":
            return None
        try:
            manager = adapter.connect() if connection is None else None
            conn = connection or manager
            try:
                assert conn is not None
                row = conn.execute("SELECT generation FROM manifest ORDER BY generation DESC, rowid DESC LIMIT 1").fetchone()
                if row is None or type(row[0]) is not int or row[0] < 0:
                    return None
                return row[0]
            finally:
                if manager is not None:
                    manager.close()
        except (sqlite3.Error, ValueError, TypeError):
            return None

    def _scan_json_and_refs(self, spec: DomainSpec, adapter: SQLiteReadOnlyAdapter, blockers: list[Blocker], pages: list[Page], *, connection: sqlite3.Connection, snapshot: Any) -> list[Reference]:
        refs: list[Reference] = []
        for table, table_spec in spec.tables.items():
            if table not in snapshot.tables:
                continue
            cursor: str | None = None
            while True:
                try:
                    page = adapter.page(table, limit=self.page_size, cursor=cursor, key_columns=table_spec.key_columns, connection=connection, snapshot=snapshot)
                except (ReadOnlyAdapterError, CursorError) as exc:
                    self._block(blockers, "pagination_failed", spec.name, str(exc), table)
                    break
                for row in page.rows:
                    row_id = _text(row.get(table_spec.primary_key or table_spec.key_columns[-1], row.get("rowid", "")))
                    # Check every allow-listed JSON-bearing column, including
                    # columns discovered by suffix for custom fixtures.
                    for column, value in row.items():
                        # Event/outbox payloads are transport envelopes, not
                        # authoritative reference graphs.  They retain event
                        # identities and projection metadata which may look
                        # like *_id keys but are not logical foreign keys.
                        opaque_event_payload = (
                            table in {"domain_outbox", "rule_domain_outbox", "rule_evidence_outbox", "group_outbox"}
                            and column in {"payload_json", "metadata_json"}
                        ) or (
                            table in {
                                "group_operation_receipts", "control_preferences",
                                "selection_manifests", "agent_cleanup_history",
                            }
                            and column in {"result_json", "value_json", "source_ids_json", "detail_json"}
                        )
                        if not (column.endswith("_json") or column in spec.json_columns):
                            continue
                        try:
                            parsed = _json_load(value)
                        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                            self._block(blockers, "malformed_authoritative_json", spec.name, str(exc), table, column=column, row_id=row_id)
                            continue
                        if not opaque_event_payload:
                            refs.extend(self._extract_json_refs(spec, table, row_id, column, parsed, blockers))
                    refs.extend(self._extract_rule_refs(spec, table, row, row_id))
                pages.append(Page(spec.name, table, tuple(_public_row(row) for row in page.rows), page.next_cursor, page.done, page.fingerprint))
                cursor = page.next_cursor
                if page.done:
                    break
        return refs

    def _extract_json_refs(self, spec: DomainSpec, table: str, row_id: str, column: str, value: Any, blockers: list[Blocker], *, path: str = "") -> list[Reference]:
        refs: list[Reference] = []
        if not path and (column.endswith("_json") or column in {"before_json", "after_json"}):
            path = "metadata"
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                normalized = key_text.casefold()
                child_path = f"{path}.{key_text}" if path else key_text
                # Nested metadata is an opaque, schema-owned payload.  It may
                # contain implementation identifiers (for example
                # metadata.memory_id, metadata.request_id) that are not
                # authoritative references.  Only evaluate reference-shaped
                # keys outside metadata namespaces.
                in_metadata = path.casefold().startswith("metadata")
                if ("ref" in normalized or normalized.endswith("_id") or normalized.endswith("_ids")) and normalized not in _KNOWN_JSON_KEYS and not in_metadata and "." not in child_path:
                    self._block(blockers, "unknown_reference_key", spec.name, "unknown reference key in authoritative JSON", table, column=column, key=child_path)
                if normalized in _KNOWN_JSON_KEYS and ("ref" in normalized or normalized.endswith("_id") or normalized.endswith("_ids")) and normalized not in {"source_ref", "source_revision", "attempt_id", "migration_id"}:
                    values = child if isinstance(child, (list, tuple, set)) else [child]
                    for item in values:
                        if isinstance(item, (str, int)) and str(item):
                            target_domain, target_table, target_column = self._json_target(normalized)
                            refs.append(Reference(spec.name, table, row_id, target_domain, str(item), "json", column, target_table, target_column))
                refs.extend(self._extract_json_refs(spec, table, row_id, column, child, blockers, path=child_path))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                refs.extend(self._extract_json_refs(spec, table, row_id, column, child, blockers, path=f"{path}[{index}]"))
        return refs

    @staticmethod
    def _json_target(key: str) -> tuple[str, str, str]:
        key = key.casefold()
        exact = {
            "blob_id": ("content", "content_blobs", "blob_id"),
            "occurrence_id": ("content", "content_occurrences", "occurrence_id"),
            "asset_id": ("assets", "assets", "asset_id"),
            "evidence_id": ("evidence", "evidence", "evidence_id"),
            "skill_id": ("skills", "skill_definitions", "skill_id"),
            "run_id": ("runtime", "task_runs", "run_id"),
            "node_id": ("runtime", "task_nodes", "node_id"),
            "definition_id": ("rules", "rule_definitions", "definition_id"),
            "binding_id": ("rules", "rule_bindings", "binding_id"),
        }
        return exact.get(key, ("system", "", ""))

    def _extract_rule_refs(self, spec: DomainSpec, table: str, row: Mapping[str, Any], row_id: str) -> list[Reference]:
        refs: list[Reference] = []
        for rule in spec.reference_rules:
            if rule.source_table != table or rule.source_column not in row:
                continue
            if rule.discriminator_column and rule.discriminator_values:
                if _text(row.get(rule.discriminator_column)).casefold() not in {item.casefold() for item in rule.discriminator_values}:
                    continue
            value = row.get(rule.source_column)
            if value not in (None, ""):
                refs.append(Reference(spec.name, table, row_id, rule.target_domain, str(value), rule.kind, rule.source_column, rule.target_table, rule.target_column))
        return refs

    def _ledger_and_outbox(self, spec: DomainSpec, adapter: SQLiteReadOnlyAdapter, blockers: list[Blocker], *, connection: sqlite3.Connection) -> None:
        try:
            conn = connection
            names = set(adapter._table_names(conn))
            for table in spec.unknown_ledger_tables:
                if table not in names:
                    continue
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                for row in rows:
                    status = str(row["status"]).casefold() if "status" in row.keys() else "blocked"
                    # Preserved migration anomalies are evidence, not active
                    # unresolved ledger failures.  They are intentionally kept
                    # for audit/readiness proof and must not block a V2 shadow
                    # that has already preserved the rationale.
                    if status not in {"resolved", "reviewed", "preserved"}:
                        self._block(blockers, "unknown_ledger", spec.name, "unknown ledger entry", table, count=len(rows))
                        break
            for table in spec.migration_tables:
                if table not in names:
                    continue
                cols = set(adapter._columns(conn, table))
                if "status" not in cols:
                    continue
                if table in {"migration_map"}:
                    # A migration map may intentionally preserve unmigrated
                    # legacy rows (for example FTS/index rows).  These are
                    # evidence records, not active migration failures.
                    if "target_type" in cols:
                        conn.execute("SELECT 1")
                        count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE lower(status) IN (?,?) AND target_type != ?', ("blocked", "failed", "legacy_row")).fetchone()[0])
                        if count:
                            self._block(blockers, "blocked_migration", spec.name, "blocked or unresolved migration rows", table, count=count)
                        continue
                    blocked_values = tuple(_MIGRATION_BLOCKED - {"pending"})
                else:
                    # Ignore historical failed attempts in audit; only current
                    # active migration state should block promotion.
                    blocked_values = tuple(_MIGRATION_BLOCKED - {"failed"})
                count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE lower(status) IN ({",".join("?" for _ in blocked_values)})', blocked_values).fetchone()[0])
                if count:
                    self._block(blockers, "blocked_migration", spec.name, "blocked or unresolved migration rows", table, count=count)
            for table in spec.outbox_tables:
                if table not in names:
                    continue
                cols = set(adapter._columns(conn, table))
                if "status" in cols:
                    placeholders = ",".join("?" for _ in _UNCONSUMED)
                    count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE lower(status) IN ({placeholders})', tuple(_UNCONSUMED)).fetchone()[0])
                    if count:
                        self._block(blockers, "unconsumed_outbox", spec.name, "pending/failed/unconsumed outbox rows", table, count=count)
                elif "consumed_at" in cols:
                    count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE consumed_at IS NULL OR trim(consumed_at)=\'\'').fetchone()[0])
                    if count:
                        self._block(blockers, "unconsumed_outbox", spec.name, "unconsumed outbox rows", table, count=count)
                if "sequence" in cols:
                    checkpoint = None
                    cp_table = "outbox_checkpoints" if "outbox_checkpoints" in names else None
                    if cp_table:
                        cp = conn.execute(f'SELECT MAX(last_sequence) FROM "{cp_table}"').fetchone()
                        checkpoint = 0 if cp is None or cp[0] is None else int(cp[0])
                    maximum = conn.execute(f'SELECT MAX(sequence) FROM "{table}"').fetchone()[0]
                    if maximum is not None and checkpoint is not None and int(maximum) > checkpoint:
                        self._block(blockers, "unconsumed_outbox", spec.name, "outbox checkpoint lags event sequence", table, maximum=int(maximum), checkpoint=checkpoint)
        except sqlite3.Error as exc:
            self._block(blockers, "ledger_unreadable", spec.name, str(exc))

    def _dangling_refs(self, refs: Sequence[Reference], adapters: Mapping[str, SQLiteReadOnlyAdapter], blockers: list[Blocker]) -> None:
        # Outbox rows are transport events, not durable authoritative graph
        # edges. They are validated through consumption/checkpoint rules, not
        # dangling-reference resolution.
        refs = tuple(ref for ref in refs if ref.source_table not in {"domain_outbox", "rule_domain_outbox", "rule_evidence_outbox"})
        # Only explicitly typed references are checked. Ambiguous JSON IDs are
        # retained in the report but never guessed into an unrelated table.
        cache: dict[tuple[str, str, str], set[str]] = {}
        for ref in refs:
            if ref.target_domain not in adapters or ref.target_id in {"", "0", "None"}:
                continue
            target_spec = self.registry[ref.target_domain]
            target_table = ref.target_table
            target_column = ref.target_column
            if not target_table or not target_column:
                continue
            if target_table not in target_spec.tables or target_column not in target_spec.tables[target_table].columns:
                self._block(blockers, "reference_rule_invalid", ref.source_domain, "reference rule target is not authoritative", ref.source_table, target_domain=ref.target_domain, target_table=target_table, target_column=target_column)
                continue
            cache_key = (ref.target_domain, target_table, target_column)
            if cache_key not in cache:
                values: set[str] = set()
                adapter = adapters[ref.target_domain]
                try:
                    with adapter.connect() as conn:
                        if target_table in adapter._table_names(conn) and target_column:
                            values.update(str(row[0]) for row in conn.execute(f'SELECT "{target_column}" FROM "{target_table}" WHERE "{target_column}" IS NOT NULL').fetchall())
                except sqlite3.Error as exc:
                    self._block(blockers, "logical_reference_unreadable", ref.source_domain, str(exc), ref.source_table, target_domain=ref.target_domain, target_table=target_table)
                    continue
                cache[cache_key] = values
            if ref.target_id not in cache[cache_key]:
                self._block(blockers, "dangling_logical_reference", ref.source_domain, "logical reference target is absent", ref.source_table, target_domain=ref.target_domain, target_id=ref.target_id)

    def _epoch_candidates(self, references: Sequence[Reference], blockers: Sequence[Blocker], adapters: Mapping[str, SQLiteReadOnlyAdapter], previous: Iterable[str] | None = None) -> tuple[str, ...]:
        # A candidate is an existing content blob with no allow-listed logical
        # reference and no active hold.  We never mutate the source while
        # deriving this set; a newly inserted hold therefore removes the item
        # from epoch two and from the intersection.
        referenced = {ref.target_id for ref in references if ref.target_domain == "content" and ref.source_table != "content_blobs"}
        blob_ids: set[str] = set()
        held_ids: set[str] = set()
        adapter = adapters.get("content")
        if adapter is not None:
            try:
                with adapter.connect() as conn:
                    names = set(adapter._table_names(conn))
                    if "content_blobs" in names:
                        blob_ids = {str(row[0]) for row in conn.execute('SELECT blob_id FROM "content_blobs"').fetchall()}
                    for hold_table in ("content_holds", "holds", "asset_holds"):
                        if hold_table not in names:
                            continue
                        columns = set(adapter._columns(conn, hold_table))
                        id_column = "blob_id" if "blob_id" in columns else "asset_id" if "asset_id" in columns else ""
                        if not id_column:
                            continue
                        if "active" in columns:
                            held_ids.update(str(row[0]) for row in conn.execute(f'SELECT "{id_column}" FROM "{hold_table}" WHERE active IN (1,\'1\',\'true\')').fetchall())
                        else:
                            held_ids.update(str(row[0]) for row in conn.execute(f'SELECT "{id_column}" FROM "{hold_table}"').fetchall())
            except sqlite3.Error as exc:
                self._block(blockers, "candidate_scan_failed", "content", str(exc), "content_blobs")
                return ()
        blocked_ids = {str(b.detail.get("target_id")) for b in blockers if b.code == "dangling_logical_reference"}
        candidates = sorted((blob_ids - referenced - held_ids) - blocked_ids)
        if previous is not None:
            candidates = sorted(set(candidates) & set(previous))
        return tuple(candidates)

    def audit(self, *, previous: Result | None = None, epoch: int | None = None, prior_candidates: Iterable[str] | None = None) -> Result:
        blockers: list[Blocker] = []
        refs: list[Reference] = []
        pages: list[Page] = []
        adapters: dict[str, SQLiteReadOnlyAdapter] = {}
        fingerprints: dict[str, str] = {}
        generation: int | None = None
        if previous is not None and previous.registry_digest != self.registry.digest:
            self._block(blockers, "registry_drift", "system", "authoritative registry changed between audit epochs", before=previous.registry_digest, after=self.registry.digest)
        for spec in self.registry:
            path = self.registry.path_for(self.workspace, spec.name)
            try:
                assert_lexical_safe(path, self.workspace)
            except (ReadOnlyAdapterError, OSError, ValueError) as exc:
                self._block(blockers, "unsafe_authoritative_path", spec.name, str(exc))
                continue
            if not path.is_file():
                self._block(blockers, "missing_database", spec.name, "authoritative V2 database is missing", detail={"path": str(path)})
                continue
            try:
                adapter = SQLiteReadOnlyAdapter(path, spec, domain=spec.name)
                fingerprint, manifest_generation = self._validate_schema(spec, adapter, blockers)
                adapters[spec.name] = adapter
                fingerprints[spec.name] = fingerprint
                if previous is not None and fingerprint and previous.schema_fingerprints.get(spec.name) not in (None, fingerprint):
                    self._block(blockers, "schema_drift", spec.name, "schema fingerprint changed between audit epochs", detail={"before": previous.schema_fingerprints.get(spec.name), "after": fingerprint})
                if manifest_generation is not None:
                    generation = manifest_generation
                    if previous is not None and previous.manifest_generation is not None and generation != previous.manifest_generation:
                        self._block(blockers, "manifest_generation_drift", "system", "manifest generation changed between audit epochs", detail={"before": previous.manifest_generation, "after": generation})
                with adapter.connect() as conn:
                    conn.execute("BEGIN")
                    scan_snapshot = adapter.schema(conn)
                    if fingerprint and scan_snapshot.fingerprint != fingerprint:
                        self._block(blockers, "schema_drift", spec.name, "schema changed between validation and read epoch", before=fingerprint, after=scan_snapshot.fingerprint)
                    if spec.name == "system":
                        scan_generation = self._manifest_generation(spec, adapter, conn)
                        if scan_generation is None:
                            self._block(blockers, "manifest_generation_unavailable", spec.name, "manifest generation is missing or corrupt", "manifest")
                        elif generation is not None and scan_generation != generation:
                            self._block(blockers, "manifest_generation_drift", spec.name, "manifest generation changed before the read epoch", before=generation, after=scan_generation)
                        generation = scan_generation
                    self._ledger_and_outbox(spec, adapter, blockers, connection=conn)
                    refs.extend(self._scan_json_and_refs(spec, adapter, blockers, pages, connection=conn, snapshot=scan_snapshot))
                    conn.rollback()
            except (ReadOnlyAdapterError, sqlite3.Error, OSError, ValueError) as exc:
                self._block(blockers, "domain_unreadable", spec.name, str(exc))
        self._dangling_refs(refs, adapters, blockers)
        refs = sorted({ref.key: ref for ref in refs}.values(), key=lambda r: r.key)
        first = () if blockers else self._epoch_candidates(refs, blockers, adapters, prior_candidates)
        if previous is not None:
            intersection = tuple(sorted(set(first) & set(previous.candidates or previous.candidate_intersection)))
            epochs = tuple(previous.epoch_candidates) + (first,)
        else:
            intersection = first
            epochs = (first,)
        status = "BLOCKED" if blockers else "PASS"
        return Result(status, self.registry.names, tuple(refs), tuple(blockers), first, intersection, epochs, fingerprints, self.registry.digest, generation, tuple(pages), {"capability": False, "reason": "hold_first_not_proven", "deleted": 0})

    run = audit
    execute = audit


def audit_workspace(workspace: str | Path, **kwargs: Any) -> Result:
    return ReferenceAudit(workspace, **kwargs).audit()


def run_reference_audit(workspace: str | Path, **kwargs: Any) -> Result:
    return audit_workspace(workspace, **kwargs)


ReferenceAuditResult = Result
ReferenceAuditBlocker = Blocker
ReferenceAuditPage = Page
AuditResult = Result


__all__ = ["Reference", "Blocker", "Page", "Result", "AuditProtocol", "DomainSpec", "ReferenceRule", "ReferenceAuditResult", "ReferenceAuditBlocker", "ReferenceAuditPage", "AuditResult", "ReferenceAudit", "audit_workspace", "run_reference_audit"]
