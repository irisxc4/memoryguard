"""Read-only V1 rules migration into the V2 shadow rules database.

The reader opens legacy SQLite files with ``mode=ro`` and never constructs a
legacy store.  Rule facts and evidence references are staged atomically in
``rules.db``.  Evidence bodies stay outside this database; only digest/ref
metadata and an idempotent evidence outbox are written.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..rule_definition import RuleDefinition, build_definition
from ..rules.v2_store import EvidenceProjectionError, EvidenceProjector, RuleV2Store, stable_digest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RulesMigrationError(RuntimeError):
    """Migration failed before the rules staging transaction committed."""


class MigrationReadError(RulesMigrationError):
    """A V1 source could not be inspected safely in read-only mode."""


@dataclass
class RulesMigrationReport(Mapping[str, Any]):
    migration_id: str
    status: str
    source_digest: str = ""
    target_digest: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    binding_multiset_before: Counter[tuple[Any, ...]] = field(default_factory=Counter)
    binding_multiset_after: Counter[tuple[Any, ...]] = field(default_factory=Counter)
    binding_multiset_diff: int = 0
    system_auto_expansion: int = 0
    unknown_columns: int = 0
    evidence_status: str = "NOT_EVALUATED"
    evidence_pending: int = 0
    idempotency_fence_source_digest: str = ""
    idempotency_fence_target_digest: str = ""
    idempotency_fence_loss: int | None = None
    idempotency_fence_conflicts: int = 0
    _idempotency_fence_rows: list[tuple[Any, ...]] = field(default_factory=list, repr=False)
    source_hashes: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"MIGRATED", "IDEMPOTENT"} and self.binding_multiset_diff == 0 and self.idempotency_fence_loss in (None, 0) and self.evidence_status != "FAILED"

    @property
    def ready(self) -> bool:
        return self.ok and self.evidence_status == "PASS"

    @property
    def binding_identity_multiset_diff(self) -> int:
        return self.binding_multiset_diff

    def to_dict(self) -> dict[str, Any]:
        def serialise_counter(counter: Counter[tuple[Any, ...]]) -> dict[str, int]:
            return {json.dumps(list(key), ensure_ascii=False): int(value) for key, value in sorted(counter.items(), key=lambda item: repr(item[0]))}
        payload = {
            "migration_id": self.migration_id,
            "status": self.status,
            "ok": self.ok,
            "ready": self.ready,
            "source_digest": self.source_digest,
            "target_digest": self.target_digest,
            "counts": dict(self.counts),
            "binding_multiset_before": serialise_counter(self.binding_multiset_before),
            "binding_multiset_after": serialise_counter(self.binding_multiset_after),
            "binding_multiset_diff": self.binding_multiset_diff,
            "binding_identity_multiset_diff": self.binding_multiset_diff,
            "system_auto_expansion": self.system_auto_expansion,
            "unknown_columns": self.unknown_columns,
            "evidence_status": self.evidence_status,
            "evidence_pending": self.evidence_pending,
            "idempotency_fence_source_digest": self.idempotency_fence_source_digest,
            "idempotency_fence_target_digest": self.idempotency_fence_target_digest,
            "idempotency_fence_loss": self.idempotency_fence_loss,
            "idempotency_fence_conflicts": self.idempotency_fence_conflicts,
            "source_hashes": dict(self.source_hashes),
            "errors": list(self.errors),
            "validation": dict(self.validation),
        }
        return payload

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


@dataclass(frozen=True)
class _SourceDB:
    kind: str
    group_id: str
    path: Path
    rows: Mapping[str, tuple[dict[str, Any], ...]]
    columns: Mapping[str, tuple[str, ...]]
    digest: str
    row_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _quote_identifier(value: str) -> str:
    """Quote an SQLite identifier; source table names are untrusted data."""

    return '"' + str(value).replace('"', '""') + '"'


def _safe_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _without_evidence_body(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only non-body outbox metadata."""

    forbidden = {"body", "raw_content", "content", "text", "evidence", "original_content", "transcript"}
    result = {str(key): item for key, item in value.items() if str(key).casefold() not in forbidden}
    # Nested payloads can carry a raw body too.
    for key, item in list(result.items()):
        if isinstance(item, Mapping):
            result[key] = _without_evidence_body(item)
    return result


# Rule Intelligence's merge tables grew governance/judge columns after the
# V2 store deliberately reduced them to stable scalar columns plus
# ``metadata_json``.  Keep the classification explicit: these fields are
# migrated as structured metadata, while free-form judge rationale is retained
# only as a digest/reference (never as body text).
_MERGE_PROPOSAL_METADATA_COLUMNS = frozenset({
    "conflict_type", "first_merge_acknowledged", "judge_confidence",
    "judge_model", "judge_recommendation", "judge_score", "judge_source",
    "negative_score",
})
_MERGE_DECISION_METADATA_COLUMNS = frozenset({
    "contradiction_ok", "first_merge_acknowledged", "judge_confidence",
    "judge_model", "judge_recommendation", "judge_score", "judge_source",
    "negative_ok", "parameters_ok", "polarity_ok", "readiness_at_merge",
    "strength_ok",
})
_MERGE_RATIONALE_COLUMN = "judge_rationale"


def _merge_metadata(row: Mapping[str, Any], *, table: str, source_ref: str, source_row_id: str) -> dict[str, Any]:
    """Return body-free structured metadata for a legacy merge row.

    All classified scalar fields are copied verbatim under ``legacy``.  The
    rationale is intentionally not copied: only its digest, source reference,
    and source-row occurrence are retained so the provenance is auditable
    without leaking free-form text into rules.db.
    """

    metadata = _without_evidence_body(row)
    rationale = metadata.pop(_MERGE_RATIONALE_COLUMN, None)
    legacy = {
        key: row.get(key)
        for key in (_MERGE_PROPOSAL_METADATA_COLUMNS if table == "rule_merge_proposals" else _MERGE_DECISION_METADATA_COLUMNS)
        if key in row
    }
    if legacy:
        metadata["legacy"] = legacy
    if rationale is not None:
        metadata["judge_rationale_digest"] = _digest_text(rationale)
        metadata["judge_rationale_provenance"] = {
            "source_ref": source_ref,
            "source_table": table,
            "source_row_id": source_row_id,
        }
    return metadata


class V1RulesMigrator:
    """Migrate V1 always-rules and Rule Intelligence snapshots in shadow mode."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        mode: str = "ro",
        store: RuleV2Store | None = None,
        migration_id: str | None = None,
        evidence_sink: Any | None = None,
        fail_at: str | None = None,
        fault_hook: Callable[[str], Any] | None = None,
        immutable_sources: bool = False,
    ):
        if str(mode).casefold() not in {"ro", "read_only", "readonly"}:
            raise ValueError("V1RulesMigrator only supports mode='ro'")
        self.workspace = Path(workspace).expanduser().resolve()
        self.mode = "ro"
        self.store = store or RuleV2Store(self.workspace)
        self.migration_id = migration_id
        self.evidence_sink = evidence_sink
        self.fail_at = fail_at
        self.fault_hook = fault_hook
        self.immutable_sources = bool(immutable_sources)
        self.last_report: RulesMigrationReport | None = None

    # ---- source discovery/read --------------------------------------
    def source_paths(self) -> list[tuple[str, str, Path]]:
        root = self.workspace / ".memoryguard" / "shared-memory"
        result: list[tuple[str, str, Path]] = []
        if root.is_dir():
            for child in sorted(root.iterdir(), key=lambda path: path.name):
                path = child / "memory.db"
                if child.is_dir() and path.is_file():
                    result.append(("shared_memory", child.name, path.resolve()))
        ri = self.workspace / ".memoryguard" / "rule-intelligence" / "memory.db"
        if ri.is_file():
            result.append(("rule_intelligence", "", ri.resolve()))
        return result

    discover_sources = source_paths

    @staticmethod
    def _read_sqlite(kind: str, group_id: str, path: Path, *, immutable: bool = False) -> _SourceDB:
        if not path.is_file():
            raise MigrationReadError(f"missing V1 source: {path}")
        digest = _sha256(path)
        try:
            uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
                if integrity != ["ok"]:
                    raise MigrationReadError(f"integrity check failed: {path}")
                if conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise MigrationReadError(f"foreign key check failed: {path}")
                names = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                rows: dict[str, tuple[dict[str, Any], ...]] = {}
                columns: dict[str, tuple[str, ...]] = {}
                row_ids: dict[str, tuple[str, ...]] = {}
                for table in names:
                    info = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
                    columns[table] = tuple(str(row[1]) for row in info)
                    fetched = conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall()
                    rows[table] = tuple(_dict_row(item) for item in fetched)
                    try:
                        rowid_rows = conn.execute(f"SELECT rowid FROM {_quote_identifier(table)}").fetchall()
                        row_ids[table] = tuple(str(item[0]) for item in rowid_rows)
                    except sqlite3.Error:
                        # WITHOUT ROWID/virtual tables: deterministic full-row
                        # digest plus occurrence ordinal prevents duplicate
                        # rows from collapsing under one weak identity.
                        row_ids[table] = tuple(stable_digest((table, columns[table], _dict_row(item), index)) for index, item in enumerate(fetched))
                return _SourceDB(kind, group_id, path, rows, columns, digest, row_ids)
            finally:
                conn.close()
        except MigrationReadError:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise MigrationReadError(f"cannot read V1 source {path}: {exc}") from exc

    def read_sources(self) -> list[_SourceDB]:
        return [self._read_sqlite(kind, group, path, immutable=self.immutable_sources) for kind, group, path in self.source_paths()]

    # ---- migration helpers ------------------------------------------
    def _fault(self, name: str) -> None:
        if self.fail_at and self.fail_at == name:
            raise RuntimeError(f"injected rules migration failure at {name}")
        if self.fault_hook is not None:
            self.fault_hook(name)

    @staticmethod
    def _rows(source: _SourceDB, table: str) -> tuple[dict[str, Any], ...]:
        return source.rows.get(table, ())

    @staticmethod
    def _body(row: Mapping[str, Any]) -> str:
        return str(row.get("body", row.get("text", row.get("canonical_text", ""))) or "")

    @staticmethod
    def _is_always(row: Mapping[str, Any]) -> bool:
        value = row.get("injection_policy", row.get("policy", ""))
        return str(value or "").casefold() == "always"

    @staticmethod
    def _identity(source_group: str, row: Mapping[str, Any], *, definition_id: str = "") -> tuple[Any, ...]:
        target_type = str(row.get("target_type", "") or "")
        target_id = str(row.get("target_id", "") or "")
        project = str(row.get("project_ref", "") or "")
        provider = str(row.get("provider", "") or "")
        runtime_role = str(row.get("runtime_role", "") or "")
        effect = str(row.get("effect", "include") or "include")
        priority = int(row.get("priority_override", row.get("priority", 0)) or 0)
        # Definition is intentionally excluded: this is permission identity,
        # matching the legacy RuleBinding audience invariant.
        return (source_group, target_type, target_id, project, provider, runtime_role, effect, priority)

    @staticmethod
    def _canonical_fence_key(key: Any) -> str:
        """Return the source key for a preserved historical conflict row."""
        raw = str(key or "")
        marker = "#conflict-"
        return raw.split(marker, 1)[0] if marker in raw else raw

    @staticmethod
    def _source_fence_key(row: Mapping[str, Any]) -> str:
        """Legacy shared-memory snapshots called this column idempotency_key."""
        return str(row.get("key") or row.get("idempotency_key") or "")

    @classmethod
    def _idempotency_fence_identity(cls, source_group: str, source_ref: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
        """Stable, body-free identity used for fence count/hash validation."""
        return (
            str(source_group or ""),
            cls._canonical_fence_key(cls._source_fence_key(row)),
            str(row.get("request_fingerprint", "") or ""),
            str(row.get("memory_id", "") or ""),
            str(row.get("event_id", "") or ""),
            str(row.get("decision_id", "") or ""),
            str(row.get("created_at", "") or ""),
            str(source_ref or ""),
        )

    @staticmethod
    def _definition_for_record(row: Mapping[str, Any]) -> RuleDefinition:
        body = V1RulesMigrator._body(row)
        if not body:
            body = f"legacy rule {row.get('memory_id', row.get('rule_id', 'unknown'))}"
        # Preserve an existing V2 definition identity when migrating an
        # already-canonical rule row.  Rebuilding from text alone is not
        # sufficient because aliases and evidence may already reference the
        # source identity.
        return build_definition(
            body,
            definition_id=str(row.get("definition_id", "") or ""),
            kind=str(row.get("kind", "workflow") or "workflow"),
            confidence=float(row.get("confidence", 1.0) or 1.0),
            created_at=str(row.get("created_at", "") or ""),
        )

    def _stage_shared_memory(self, source: _SourceDB, report: RulesMigrationReport, definitions: dict[tuple[str, str, str], str]) -> None:
        # This append-only ledger is authoritative for request idempotency.
        # Preserve each row (including the source group/path) instead of
        # classifying it as an index/derived table.
        for row in self._rows(source, "rule_idempotency_fences"):
            key = self._source_fence_key(row)
            source_ref = str(source.path)
            payload = {
                "request_fingerprint": str(row.get("request_fingerprint", "") or ""),
                "memory_id": str(row.get("memory_id", "") or ""),
                "event_id": str(row.get("event_id", "") or ""),
                "decision_id": str(row.get("decision_id", "") or ""),
                "created_at": str(row.get("created_at", "") or ""),
            }
            # Keep the historical identity for exact replays.  If a source
            # reused a key with changed immutable fields, preserve the old row
            # and write a deterministic sibling instead of replacing it.
            fence_id = stable_digest(("v2-idempotency-fence", source.group_id, key))
            fence = {"fence_id": fence_id, "key": key, **payload, "share_group_id": source.group_id, "source_ref": source_ref}
            conflict = False
            try:
                self.store.record_idempotency_fence(fence)
            except ValueError as exc:
                if "immutable rule_idempotency_fences conflict" not in str(exc):
                    raise
                conflict = True
            if conflict:
                payload_digest = stable_digest((source.group_id, source_ref, key, payload))
                conflict_key = f"{key}#conflict-{payload_digest[:16]}"
                fence_id = stable_digest(("v2-idempotency-fence-conflict", source.group_id, source_ref, key, payload))
                self.store.record_idempotency_fence({"fence_id": fence_id, "key": conflict_key, **payload, "share_group_id": source.group_id, "source_ref": source_ref})
                self.store.record_idempotency_fence_anomaly({
                    "migration_id": self.migration_id,
                    "source_kind": "shared_memory",
                    "source_path": source_ref,
                    "source_group_id": source.group_id,
                    "source_key": key,
                    "original_fence_id": stable_digest(("v2-idempotency-fence", source.group_id, key)),
                    "conflict_fence_id": fence_id,
                    "payload_digest": payload_digest,
                    "details_json": json.dumps({"reason": "immutable_payload_conflict", "preserved_key": conflict_key}, ensure_ascii=False, sort_keys=True),
                    "status": "PRESERVED",
                    "created_at": "",
                })
                report.idempotency_fence_conflicts += 1
                report.counts["idempotency_fence_conflicts"] = report.idempotency_fence_conflicts
            source_id = conflict_key if conflict else key
            self.store.record_migration_map({
                "migration_id": self.migration_id,
                "source_kind": "shared_memory",
                "source_path": source_ref,
                "source_group_id": source.group_id,
                "source_table": "rule_idempotency_fences",
                "source_id": source_id,
                "target_table": "rule_idempotency_fences",
                "target_id": fence_id,
                "source_digest": source.digest,
                "status": "migrated",
                "metadata_json": json.dumps({"source_ref": source_ref, "original_key": key, "preserved_conflict": conflict}, ensure_ascii=False, sort_keys=True),
                "created_at": "",
            })
            report._idempotency_fence_rows.append(self._idempotency_fence_identity(source.group_id, source_ref, row))
            report.counts["idempotency_fences"] = report.counts.get("idempotency_fences", 0) + 1

        records = [row for row in self._rows(source, "records") if self._is_always(row)]
        definition_ids: dict[str, str] = {}
        assignments = self._rows(source, "rule_assignments")
        assignment_by_memory: dict[str, list[dict[str, Any]]] = {}
        for row in assignments:
            assignment_by_memory.setdefault(str(row.get("memory_id", "")), []).append(row)
        for record in records:
            memory_id = str(record.get("memory_id", ""))
            definition = self._definition_for_record(record)
            definition = self.store.upsert_definition(definition)
            definition_ids[memory_id] = definition.definition_id
            definitions[(source.group_id, memory_id, "shared_memory")] = definition.definition_id
            self.store.record_definition_version(definition.definition_id, snapshot=definition.to_dict(), reason="v1_rules_migration", actor="migration", source_ref=f"{source.path}#{memory_id}")
            self.store.upsert_source_link(source_kind="shared_memory", share_group_id=source.group_id, memory_id=memory_id, source_ref=str(source.path), source_revision=str(record.get("updated_at", record.get("created_at", "")) or ""), original_definition_id=memory_id, canonical_definition_id=definition.definition_id, metadata_json=json.dumps({"v1_status": record.get("status", "")}, ensure_ascii=False), created_at=str(record.get("created_at", "") or ""), updated_at=str(record.get("updated_at", "") or ""))
            self.store.record_migration_map({"migration_id": self.migration_id, "source_kind": "shared_memory", "source_path": str(source.path), "source_group_id": source.group_id, "source_table": "records", "source_id": memory_id, "target_table": "rule_definitions", "target_id": definition.definition_id, "source_digest": source.digest, "status": "migrated", "metadata_json": "{}", "created_at": ""})
            report.counts["records"] = report.counts.get("records", 0) + 1
            evidence_id = stable_digest(("shared_memory", source.group_id, memory_id, definition.definition_id))
            self.store.record_evidence_ref({"evidence_id": evidence_id, "definition_id": definition.definition_id, "source_rule_id": memory_id, "share_group_id": source.group_id, "agent_instance_id": record.get("agent_instance_id", ""), "content_digest": _digest_text(self._body(record)), "evidence_ref": f"v1:{source.path}#{memory_id}", "confidence": float(record.get("confidence", 1.0) or 1.0), "observed_at": str(record.get("updated_at", record.get("created_at", "")) or ""), "metadata_json": json.dumps({"source": "shared_memory", "kind": record.get("kind", "")}, ensure_ascii=False)})
            self.store.append_evidence_outbox({"migration_id": self.migration_id, "evidence_id": evidence_id, "definition_id": definition.definition_id, "evidence_ref": f"v1:{source.path}#{memory_id}", "content_digest": _digest_text(self._body(record)), "polarity": "positive", "source_kind": "shared_memory", "source_group_id": source.group_id, "payload_json": json.dumps({"source_ref": f"v1:{source.path}#{memory_id}", "content_digest": _digest_text(self._body(record))}, ensure_ascii=False)})
            for index, assignment in enumerate(assignment_by_memory.get(memory_id, [])):
                audience = dict(assignment)
                legacy_hash = stable_digest((source.group_id, memory_id, audience))
                binding_id = stable_digest(("v2-migration-binding", source.group_id, memory_id, legacy_hash, definition.definition_id))
                binding = {"binding_id": binding_id, "definition_id": definition.definition_id, "share_group_id": source.group_id, "target_type": str(audience.get("target_type", "agent") or "agent"), "target_id": str(audience.get("target_id", "") or ""), "project_ref": str(audience.get("project_ref", "") or ""), "provider": str(audience.get("provider", "") or ""), "runtime_role": str(audience.get("runtime_role", "") or ""), "effect": str(audience.get("effect", "include") or "include"), "priority": int(audience.get("priority_override", audience.get("priority", 0)) or 0), "owner_agent_id": str(record.get("agent_instance_id", "") or ""), "created_by": "migration", "authorization": f"v1:{source.path}", "status": "active", "created_at": str(audience.get("created_at", "") or ""), "updated_at": str(audience.get("updated_at", "") or "")}
                self.store.upsert_binding(binding, contribution={"contribution_id": stable_digest(("contribution", source.group_id, memory_id, legacy_hash)), "source_memory_id": memory_id, "source_revision": str(record.get("updated_at", "") or ""), "legacy_assignment_hash": legacy_hash, "audience": {key: audience.get(key, "") for key in ("target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority_override", "priority")}, "active": True, "status": "active"})
                report.binding_multiset_before[self._identity(source.group_id, audience)] += 1
                report.binding_multiset_after[self._identity(source.group_id, audience)] += 1
                report.counts["bindings"] = report.counts.get("bindings", 0) + 1
            self._stage_legacy_lifecycle(source, record, definition.definition_id, report)
        # Decision rows are authoritative independently of the rule injection
        # policy. Stage each source table once, after the always-rule pass;
        # otherwise unscoped ``decisions`` rows are replayed once per always
        # record and become false conflict siblings, while decisions for
        # relevant rules are skipped entirely.
        self._stage_decision_rows(source, report, definition_ids)

    def _record_decision_migration_map(self, value: Mapping[str, Any]) -> str:
        """Resume old partial decision maps without rewriting immutable metadata.

        The natural migration key is unique.  Older partial runs may carry
        ``metadata_json='{}'``; when target identity is unchanged, retain that
        row verbatim.  A changed target identity remains a hard conflict.
        """
        key = (
            str(value.get("migration_id", "")), str(value.get("source_kind", "")),
            str(value.get("source_group_id", "")), str(value.get("source_table", "")),
            str(value.get("source_id", "")), str(value.get("target_table", "")),
        )
        existing = self.store._read(lambda conn: conn.execute(
            "SELECT map_id,target_id FROM rule_migration_map WHERE migration_id=? AND source_kind=? AND source_group_id=? AND source_table=? AND source_id=? AND target_table=?",
            key,
        ).fetchone())
        if existing is not None:
            old_target = str(existing[1] or "")
            new_target = str(value.get("target_id", "") or "")
            if old_target != new_target:
                raise ValueError(f"immutable rule_migration_map conflict for source_id={key[4]}: target_id {old_target!r} != {new_target!r}")
            return str(existing[0])
        return str(self.store.record_migration_map(value))

    def _stage_decision_rows(self, source: _SourceDB, report: RulesMigrationReport, definition_ids: Mapping[str, str]) -> None:
        """Migrate each authoritative decision occurrence once per source."""
        # ``decisions`` is an unscoped append-only ledger, while
        # ``rule_decisions`` can refer to non-always (relevant) records.  Keep
        # this pass outside the always-rule loop so rows are never replayed
        # once per record and no relevant decision is skipped.
        decision_targets: dict[str, tuple[str, str]] = {}
        for table in ("rule_decisions", "decisions"):
            for row in self._rows(source, table):
                rule_id = str(row.get("rule_id", row.get("memory_id", "")) or "")
                resolved_rule_id = definition_ids.get(rule_id, rule_id)
                original_id = str(row.get("decision_id", row.get("event_id", ""))) or stable_digest((source.path.as_posix(), table, row))
                source_ref = f"{source.path}#{table}"
                before_value = row.get("before", row.get("before_state", ""))
                after_value = row.get("after", row.get("after_state", ""))
                target = {
                    "decision_id": original_id,
                    "actor": str(row.get("actor", "") or ""),
                    "owner_agent_id": str(row.get("owner_agent_id", "") or ""),
                    "rule_id": resolved_rule_id,
                    "action": str(row.get("action", "") or ""),
                    "before_hash": _digest_text(before_value),
                    "after_hash": _digest_text(after_value),
                    "before_json": json.dumps(_safe_json(before_value), ensure_ascii=False),
                    "after_json": json.dumps(_safe_json(after_value), ensure_ascii=False),
                    "reason": str(row.get("reason", "") or ""),
                    "confidence": float(row.get("confidence", 1.0) or 1.0),
                    "undo_id": str(row.get("undo_id", "") or ""),
                    "target_ids_json": json.dumps(_safe_json(row.get("target_ids", [])), ensure_ascii=False),
                    "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
                    "source_ref": source_ref,
                    "created_at": str(row.get("created_at", "") or ""),
                }
                semantic_keys = tuple(key for key in target if key not in {"decision_id", "metadata_json", "source_ref"})
                semantic_digest = stable_digest({key: target[key] for key in semantic_keys})
                prior = decision_targets.get(original_id)
                preserve = False
                if prior is not None:
                    if prior[0] == semantic_digest:
                        self._record_decision_migration_map({"migration_id": self.migration_id, "source_kind": "shared_memory", "source_path": str(source.path), "source_group_id": source.group_id, "source_table": table, "source_id": original_id, "target_table": "rule_decisions", "target_id": prior[1], "source_digest": source.digest, "status": "migrated", "metadata_json": json.dumps({"equivalent_source_table": True, "canonical_decision_id": prior[1]}, ensure_ascii=False, sort_keys=True), "created_at": ""})
                        report.counts["decisions"] = report.counts.get("decisions", 0) + 1
                        continue
                    preserve = True
                else:
                    existing = self.store._read(lambda conn: conn.execute("SELECT actor,owner_agent_id,rule_id,action,before_hash,after_hash,before_json,after_json,reason,confidence,undo_id,target_ids_json,created_at FROM rule_decisions WHERE decision_id=?", (original_id,)).fetchone())
                    if existing is None:
                        decision_targets[original_id] = (semantic_digest, original_id)
                    else:
                        existing_semantic = stable_digest({
                            "actor": str(existing[0] or ""), "owner_agent_id": str(existing[1] or ""), "rule_id": str(existing[2] or ""), "action": str(existing[3] or ""),
                            "before_hash": str(existing[4] or ""), "after_hash": str(existing[5] or ""), "before_json": str(existing[6] or "{}"), "after_json": str(existing[7] or "{}"),
                            "reason": str(existing[8] or ""), "confidence": float(existing[9] if existing[9] is not None else 1.0), "undo_id": str(existing[10] or ""), "target_ids_json": str(existing[11] or "[]"), "created_at": str(existing[12] or ""),
                        })
                        if existing_semantic == semantic_digest:
                            decision_targets[original_id] = (semantic_digest, original_id)
                            self._record_decision_migration_map({"migration_id": self.migration_id, "source_kind": "shared_memory", "source_path": str(source.path), "source_group_id": source.group_id, "source_table": table, "source_id": original_id, "target_table": "rule_decisions", "target_id": original_id, "source_digest": source.digest, "status": "migrated", "metadata_json": json.dumps({"equivalent_existing": True}, ensure_ascii=False, sort_keys=True), "created_at": ""})
                            report.counts["decisions"] = report.counts.get("decisions", 0) + 1
                            continue
                        preserve = True
                if preserve:
                    target_id = stable_digest(("v2-rule-decision-sibling", source.group_id, source.path.as_posix(), table, original_id, semantic_digest))
                    target["decision_id"] = target_id
                    target["metadata_json"] = json.dumps({**_without_evidence_body(row), "_migration": {"preserved_sibling": True, "canonical_decision_id": original_id, "source_table": table}}, ensure_ascii=False, sort_keys=True, default=str)
                    self.store.record_decision(target)
                    payload_digest = stable_digest({key: target[key] for key in semantic_keys})
                    self.store.record_decision_anomaly({"migration_id": self.migration_id, "source_kind": "shared_memory", "source_path": str(source.path), "source_group_id": source.group_id, "source_table": table, "original_decision_id": original_id, "sibling_decision_id": target_id, "payload_digest": payload_digest, "details_json": json.dumps({"reason": "immutable_payload_conflict", "canonical_decision_id": original_id, "source_table": table}, ensure_ascii=False, sort_keys=True), "status": "PRESERVED", "created_at": ""})
                    report.counts["decision_conflicts"] = report.counts.get("decision_conflicts", 0) + 1
                else:
                    self.store.record_decision(target)
                # Keep the map's natural source occurrence key unique when a
                # sibling is preserved, but retain the canonical occurrence
                # identity in metadata.  Validators must be able to match the
                # authoritative source row without mistaking the deterministic
                # ``#conflict`` shadow key for a second source occurrence.
                mapping_source_id = f"{original_id}#conflict-{semantic_digest[:16]}" if preserve else original_id
                self._record_decision_migration_map({"migration_id": self.migration_id, "source_kind": "shared_memory", "source_path": str(source.path), "source_group_id": source.group_id, "source_table": table, "source_id": mapping_source_id, "target_table": "rule_decisions", "target_id": target["decision_id"], "source_digest": source.digest, "status": "migrated", "metadata_json": json.dumps({"preserved_sibling": preserve, "canonical_source_id": original_id, "original_source_id": original_id, "canonical_decision_id": original_id, "original_decision_id": original_id, "source_occurrence_id": mapping_source_id}, ensure_ascii=False, sort_keys=True), "created_at": ""})
                report.counts["decisions"] = report.counts.get("decisions", 0) + 1

    def _stage_legacy_lifecycle(self, source: _SourceDB, record: Mapping[str, Any], definition_id: str, report: RulesMigrationReport) -> None:
        memory_id = str(record.get("memory_id", ""))
        for row in self._rows(source, "rule_exceptions"):
            if str(row.get("parent_rule", row.get("parent_rule_id", ""))) != memory_id: continue
            self.store.upsert_exception({"exception_id": str(row.get("exception_id", "")) or stable_digest((source.path.as_posix(), row)), "parent_rule_id": definition_id, "child_exception_id": str(row.get("child_exception", row.get("child_exception_id", ""))), "parent_rule": definition_id, "child_exception": str(row.get("child_exception", row.get("child_exception_id", ""))), "priority": int(row.get("priority", 0) or 0), "reason": str(row.get("reason", "") or ""), "rollback_json": json.dumps(_safe_json(row.get("rollback", {})), ensure_ascii=False), "active": int(bool(row.get("active", 1))), "source_ref": f"{source.path}#rule_exceptions", "created_at": str(row.get("created_at", "") or ""), "updated_at": str(row.get("updated_at", "") or "")})
            report.counts["exceptions"] = report.counts.get("exceptions", 0) + 1
        receipt_rows = {str(row.get("receipt_id", "")): row for row in self._rows(source, "rule_match_receipts")}
        for receipt_id, row in receipt_rows.items():
            source_rule_id = str(row.get("memory_id", "") or "")
            if source_rule_id and source_rule_id != memory_id: continue
            self.store.record_receipt({"receipt_id": receipt_id, "definition_id": definition_id, "source_rule_id": memory_id, "share_group_id": source.group_id, "agent_instance_id": row.get("agent_instance_id", ""), "project_ref": row.get("project_ref", ""), "session_id": row.get("session_id", ""), "task_hash": row.get("task_hash", ""), "selection_digest": _digest_text(row.get("assignment_ids", "")), "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str), "created_at": row.get("created_at", "")})
            report.counts["receipts"] = report.counts.get("receipts", 0) + 1
            for feedback in self._rows(source, "rule_match_feedbacks"):
                if str(feedback.get("receipt_id", "")) != receipt_id: continue
                self.store.record_feedback({"feedback_id": str(feedback.get("feedback_id", "")) or stable_digest((source.path.as_posix(), feedback)), "receipt_id": receipt_id, "definition_id": definition_id, "outcome": str(feedback.get("outcome", "") or ""), "authority": int(feedback.get("authority", 0) or 0), "evidence_digest": _digest_text(feedback.get("evidence", "")), "metadata_json": json.dumps(_without_evidence_body(feedback), ensure_ascii=False, default=str), "created_at": feedback.get("created_at", "")})
                report.counts["feedback"] = report.counts.get("feedback", 0) + 1
        for row in self._rows(source, "rule_event_outbox"):
            self.store.append_domain_outbox({"event_id": str(row.get("event_id", "")) or stable_digest((source.path.as_posix(), row)), "migration_id": self.migration_id, "event_type": str(row.get("event_type", "") or ""), "source_kind": "shared_memory", "source_group_id": source.group_id, "source_ref": str(source.path), "payload_digest": stable_digest(row), "payload_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str), "created_at": row.get("created_at", "")})
            report.counts["outbox"] = report.counts.get("outbox", 0) + 1

    def _ri_definition(self, row: Mapping[str, Any]) -> RuleDefinition:
        body = str(row.get("canonical_text", row.get("text", "")) or "")
        if body:
            # Keep the canonical V2 identity derivation, but carry source
            # governance fields through the RuleDefinition model.  In
            # particular ``superseded_by`` is a real V2 column, not an
            # unknown source attribute.
            definition = build_definition(
                body,
                kind=str(row.get("rule_kind", "workflow") or "workflow"),
                confidence=float(row.get("confidence", 1.0) or 1.0),
                created_at=str(row.get("created_at", "") or ""),
                definition_id=str(row.get("definition_id", "") or ""),
                rule_strength=str(row.get("rule_strength", "") or "") or "",
            )
            return RuleDefinition.from_dict({
                **definition.to_dict(),
                "status": row.get("status", definition.status),
                "revision": int(row.get("revision", definition.revision) or definition.revision),
                "maturity_state": row.get("maturity_state", definition.maturity_state),
                "updated_at": row.get("updated_at", definition.updated_at),
                "superseded_by": row.get("superseded_by", ""),
            })
        raw = dict(row)
        raw.setdefault("canonical_text", "legacy rule intelligence definition")
        raw.setdefault("normalized_intent", str(row.get("normalized_intent", "") or ""))
        raw.setdefault("rule_kind", str(row.get("rule_kind", "workflow") or "workflow"))
        raw.setdefault("semantic_hash", str(row.get("semantic_hash", "") or ""))
        raw.setdefault("definition_id", str(row.get("definition_id", "")))
        return RuleDefinition.from_dict(raw)

    def _stage_rule_intelligence(self, source: _SourceDB, report: RulesMigrationReport, definitions: dict[tuple[str, str, str], str]) -> None:
        definition_map: dict[str, str] = {}
        superseded_by_rows: list[tuple[str, str]] = []
        for row in self._rows(source, "rule_definitions"):
            old_id = str(row.get("definition_id", "")); definition = self.store.upsert_definition(self._ri_definition(row)); definition_map[old_id] = definition.definition_id
            if "superseded_by" in row:
                superseded_by_rows.append((definition.definition_id, str(row.get("superseded_by", "") or "")))
            self.store.record_migration_map({"migration_id": self.migration_id, "source_kind": "rule_intelligence", "source_path": str(source.path), "source_table": "rule_definitions", "source_id": old_id, "target_table": "rule_definitions", "target_id": definition.definition_id, "source_digest": source.digest, "status": "migrated", "metadata_json": "{}", "created_at": ""})
            if old_id and old_id != definition.definition_id: self.store.record_alias(old_id, definition.definition_id, migration_decision_id=self.migration_id, source_ref=str(source.path))
            report.counts["ri_definitions"] = report.counts.get("ri_definitions", 0) + 1
        # Resolve supersession pointers only after every source definition has
        # a canonical target ID.  Unknown pointers remain verbatim as
        # provenance rather than being silently dropped.
        for target_id, source_superseded_by in superseded_by_rows:
            definition = self.store.get_definition(target_id)
            if definition is None:
                continue
            resolved = definition_map.get(source_superseded_by, source_superseded_by)
            if resolved == definition.superseded_by:
                continue
            self.store.upsert_definition(RuleDefinition.from_dict({**definition.to_dict(), "superseded_by": resolved}))
        binding_map: dict[str, str] = {}
        for row in self._rows(source, "rule_bindings"):
            old_definition = str(row.get("definition_id", "")); definition_id = definition_map.get(old_definition, old_definition)
            if not definition_id or self.store.get_definition(definition_id) is None: continue
            source_binding_id = str(row.get("binding_id", ""))
            binding_id = source_binding_id or stable_digest((source.path.as_posix(), "binding", row))
            binding_map[source_binding_id] = binding_id
            binding = {"binding_id": binding_id, "definition_id": definition_id, "share_group_id": row.get("share_group_id", ""), "target_type": row.get("target_type", "agent"), "target_id": row.get("target_id", ""), "project_ref": row.get("project_ref", ""), "provider": row.get("provider", ""), "runtime_role": row.get("runtime_role", ""), "effect": row.get("effect", "include"), "priority": int(row.get("priority", 0) or 0), "owner_agent_id": row.get("owner_agent_id", ""), "created_by": "migration", "authorization": f"v1:{source.path}", "status": row.get("status", "active"), "revision": int(row.get("revision", 1) or 1), "created_at": row.get("created_at", ""), "updated_at": row.get("updated_at", "")}
            self.store.upsert_binding(binding)
            self.store.record_migration_map({
                "migration_id": self.migration_id,
                "source_kind": "rule_intelligence",
                "source_path": str(source.path),
                "source_group_id": str(row.get("share_group_id", "") or ""),
                "source_table": "rule_bindings",
                "source_id": source_binding_id or binding_id,
                "target_table": "rule_bindings",
                "target_id": binding_id,
                "source_digest": source.digest,
                "status": "migrated",
                "metadata_json": json.dumps({"source_binding_id": source_binding_id, "scope": {key: binding[key] for key in ("target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority", "owner_agent_id", "revision")}}, ensure_ascii=False, sort_keys=True),
                "created_at": "",
            })
            report.counts["ri_bindings"] = report.counts.get("ri_bindings", 0) + 1
        for row in self._rows(source, "rule_binding_contributions"):
            item = dict(row)
            item["definition_id"] = definition_map.get(str(row.get("definition_id", "")), row.get("definition_id", ""))
            item["contribution_id"] = str(row.get("contribution_id", "")) or stable_digest((source.path.as_posix(), "contribution", row))
            item["binding_id"] = binding_map.get(str(row.get("binding_id", "")), str(row.get("binding_id", "")))
            item["audience"] = _safe_json(row.get("audience", row.get("audience_json", {})))
            try:
                contribution_id = self.store.upsert_binding_contribution(item)
            except sqlite3.IntegrityError:
                continue
            self.store.record_migration_map({
                "migration_id": self.migration_id,
                "source_kind": "rule_intelligence",
                "source_path": str(source.path),
                "source_group_id": str(row.get("share_group_id", "") or ""),
                "source_table": "rule_binding_contributions",
                "source_id": str(row.get("contribution_id", "")) or contribution_id,
                "target_table": "rule_binding_contributions",
                "target_id": contribution_id,
                "source_digest": source.digest,
                "status": "migrated",
                "metadata_json": json.dumps({"authoritative_scope": {key: item.get(key, "") for key in ("target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority", "owner_agent_id", "revision")}}, ensure_ascii=False, sort_keys=True),
                "created_at": "",
            })
            report.counts["ri_binding_contributions"] = report.counts.get("ri_binding_contributions", 0) + 1

        # The Rule Intelligence database owns several P3 ledgers which are
        # intentionally not copied into the V2 evidence database.  Preserve
        # their structured facts in rules.db and retain only digests/refs for
        # any body-like values.  Each row receives an explicit migration map
        # entry so a validator can distinguish a migrated table from a source
        # that was never evaluated.
        def ri_map(table: str, source_id: str, target_table: str, target_id: str, *, status: str = "migrated") -> None:
            self.store.record_migration_map({
                "migration_id": self.migration_id,
                "source_kind": "rule_intelligence",
                "source_path": str(source.path),
                "source_table": table,
                "source_id": source_id,
                "target_table": target_table,
                "target_id": target_id,
                "source_digest": source.digest,
                "status": status,
                "metadata_json": "{}",
                "created_at": "",
            })

        for row in self._rows(source, "rule_definition_versions"):
            old_definition = str(row.get("definition_id", "") or "")
            definition_id = definition_map.get(old_definition, old_definition)
            # Versions have a FK to definitions in rules.db.  A malformed RI
            # snapshot must not create an orphan; record the source row as
            # explicitly blocked in the migration ledger instead.
            if not definition_id or self.store.get_definition(definition_id) is None:
                ri_map("rule_definition_versions", str(row.get("version_id", "")) or stable_digest(row), "rule_definition_versions", "", status="BLOCKED_MISSING_DEFINITION")
                report.counts["definition_versions_blocked"] = report.counts.get("definition_versions_blocked", 0) + 1
                continue
            version_id = str(row.get("version_id", "")) or stable_digest((source.path.as_posix(), "rule_definition_versions", row))
            snapshot = _without_evidence_body({
                "definition_id": definition_id,
                "superseded_by": row.get("superseded_by", ""),
                "old_strength": row.get("old_strength", ""),
                "new_strength": row.get("new_strength", ""),
                "evidence_digest": _digest_text(row.get("evidence", "")),
                "revision": row.get("revision", 1),
            })
            self.store.record_definition_version(
                definition_id,
                snapshot=snapshot,
                reason=str(row.get("change_reason", row.get("reason", "")) or ""),
                actor=str(row.get("actor", "") or ""),
                source_ref=f"{source.path}#rule_definition_versions:{version_id}",
                version_id=version_id,
            )
            ri_map("rule_definition_versions", version_id, "rule_definition_versions", version_id)
            report.counts["definition_versions"] = report.counts.get("definition_versions", 0) + 1

        for row in self._rows(source, "agent_reputation"):
            agent_id = str(row.get("agent_id", "") or "") or stable_digest((source.path.as_posix(), "agent_reputation", row))
            self.store.record_agent_reputation({
                "agent_id": agent_id,
                "success_rate": float(row.get("success_rate", 0.0) or 0.0),
                "rule_accuracy": float(row.get("rule_accuracy", 0.0) or 0.0),
                "violation_rate": float(row.get("violation_rate", 0.0) or 0.0),
                "sample_count": int(row.get("sample_count", 0) or 0),
                "feedback_quality": float(row.get("feedback_quality", 0.0) or 0.0),
                "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
            })
            ri_map("agent_reputation", agent_id, "rule_agent_reputation", agent_id)
            report.counts["agent_reputation"] = report.counts.get("agent_reputation", 0) + 1

        for row in self._rows(source, "project_profile"):
            project_ref = str(row.get("project_ref", "") or "") or stable_digest((source.path.as_posix(), "project_profile", row))
            self.store.record_project_profile({
                "project_ref": project_ref,
                "production_level": float(row.get("production_level", 0.0) or 0.0),
                "criticality": float(row.get("criticality", 0.0) or 0.0),
                "owner_verified": int(bool(row.get("owner_verified", 0))),
                "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
            })
            ri_map("project_profile", project_ref, "rule_project_profile", project_ref)
            report.counts["project_profile"] = report.counts.get("project_profile", 0) + 1

        for row in self._rows(source, "rule_definition_runtime_stats"):
            old_definition = str(row.get("definition_id", "") or "")
            definition_id = definition_map.get(old_definition, old_definition)
            stats_id = stable_digest((source.path.as_posix(), "rule_definition_runtime_stats", old_definition))
            self.store.record_runtime_stats({
                "stats_id": stats_id,
                "definition_id": definition_id,
                "followed": int(row.get("followed", 0) or 0),
                "violated": int(row.get("violated", 0) or 0),
                "not_applicable": int(row.get("not_applicable", 0) or 0),
                "exception_count": int(row.get("exception_count", 0) or 0),
                "distinct_sessions": int(row.get("distinct_sessions", 0) or 0),
                "distinct_projects": int(row.get("distinct_projects", 0) or 0),
                "last_observed_at": row.get("last_observed_at", ""),
                "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
            })
            ri_map("rule_definition_runtime_stats", old_definition or stats_id, "rule_runtime_stats", stats_id)
            report.counts["runtime_stats"] = report.counts.get("runtime_stats", 0) + 1

        for row in self._rows(source, "rule_evidence_contributions"):
            old_definition = str(row.get("definition_id", "") or "")
            definition_id = definition_map.get(old_definition, old_definition)
            contribution_id = str(row.get("contribution_id", "")) or stable_digest((source.path.as_posix(), "rule_evidence_contributions", row))
            self.store.record_evidence_contribution({
                "contribution_id": contribution_id,
                "definition_id": definition_id,
                "independence_key": row.get("independence_key", ""),
                "kind": row.get("kind", "evidence"),
                "polarity": row.get("polarity", "positive"),
                "authority": int(row.get("authority", 0) or 0),
                "confidence": float(row.get("confidence", 1.0) or 1.0),
                "observed_at": row.get("observed_at", ""),
                "active": int(bool(row.get("active", 1))),
                "receipt_id": row.get("receipt_id", ""),
                "feedback_id": row.get("feedback_id", ""),
                "source_evidence_id": row.get("source_evidence_id", ""),
                "source_memory_id": row.get("source_memory_id", ""),
                "source_ids_json": json.dumps(_safe_json(row.get("source_ids", row.get("source_ids_json", "{}"))), ensure_ascii=False, default=str),
                "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", ""),
            })
            ri_map("rule_evidence_contributions", contribution_id, "rule_evidence_contributions", contribution_id)
            report.counts["evidence_contributions"] = report.counts.get("evidence_contributions", 0) + 1

        for row in self._rows(source, "rule_evidence_effective"):
            old_definition = str(row.get("definition_id", "") or "")
            definition_id = definition_map.get(old_definition, old_definition)
            effective_id = stable_digest((source.path.as_posix(), "rule_evidence_effective", row))
            self.store.record_evidence_effective({
                "effective_id": effective_id,
                "definition_id": definition_id,
                "independence_key": row.get("independence_key", ""),
                "kind": row.get("kind", "evidence"),
                "winner_contribution_id": row.get("winner_contribution_id", ""),
                "polarity": row.get("polarity", "positive"),
                "authority": int(row.get("authority", 0) or 0),
                "confidence": float(row.get("confidence", 1.0) or 1.0),
                "observed_at": row.get("observed_at", ""),
                "updated_at": row.get("updated_at", ""),
            })
            ri_map("rule_evidence_effective", effective_id, "rule_evidence_effective", effective_id)
            report.counts["evidence_effective"] = report.counts.get("evidence_effective", 0) + 1

        for row in self._rows(source, "governance_capabilities"):
            token_hash = str(row.get("token_hash", "") or "")
            capability_id = token_hash or stable_digest((source.path.as_posix(), "governance_capabilities", row))
            # Store only the token digest.  The bearer token itself is never
            # copied to rules.db or any migration payload.
            self.store.record_governance_capability({
                "capability_id": capability_id,
                "proposal_id": row.get("proposal_id", ""),
                "principal": row.get("principal", ""),
                "scope_json": json.dumps({"scope": row.get("scope", "")}, ensure_ascii=False),
                "issued_at": str(row.get("issued_at", "") or ""),
                "expires_at": str(row.get("expires_at", "") or ""),
                "consumed_at": str(row.get("consumed_at", "") or "") if row.get("consumed_at") is not None else "",
                "token_digest": token_hash,
                "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str),
            })
            ri_map("governance_capabilities", capability_id, "rule_governance_capabilities", capability_id)
            report.counts["governance_capabilities"] = report.counts.get("governance_capabilities", 0) + 1

        for row in self._rows(source, "rule_merge_native_requests"):
            request_key = str(row.get("request_key", "") or "")
            if not request_key:
                request_key = stable_digest((source.path.as_posix(), "rule_merge_native_requests", row))
            self.store.upsert_merge_native_request({
                "request_key": request_key,
                "request_fingerprint": row.get("request_fingerprint", ""),
                "operation": row.get("operation", ""),
                "schema_version": int(row.get("schema_version", 2) or 2),
                "status": row.get("status", "pending"),
                "result_json": row.get("result_json", ""),
                "created_at": row.get("created_at", ""),
                "updated_at": row.get("updated_at", row.get("created_at", "")),
            })
            ri_map("rule_merge_native_requests", request_key, "rule_merge_native_requests", request_key)
            report.counts["merge_native_requests"] = report.counts.get("merge_native_requests", 0) + 1

        for table, negative in (("rule_evidence", False), ("rule_negative_evidence", True)):
            for row in self._rows(source, table):
                item = {"evidence_id": str(row.get("evidence_id", "")) or stable_digest((source.path.as_posix(), table, row)), "definition_id": definition_map.get(str(row.get("definition_id", "")), str(row.get("definition_id", ""))), "source_rule_id": row.get("source_rule_id", ""), "share_group_id": row.get("share_group_id", ""), "agent_instance_id": row.get("agent_instance_id", ""), "project_ref": row.get("project_ref", ""), "session_id": row.get("session_id", ""), "receipt_id": row.get("receipt_id", ""), "content_digest": str(row.get("content_hash", "") or _digest_text(row.get("evidence", ""))), "evidence_ref": f"v1:{source.path}#{table}:{row.get('evidence_id', '')}", "confidence": float(row.get("confidence", 1.0) or 1.0), "observed_at": row.get("observed_at", ""), "metadata_json": json.dumps(_without_evidence_body(row), ensure_ascii=False, default=str)}
                self.store.record_evidence_ref(item, negative=negative)
                self.store.append_evidence_outbox({"migration_id": self.migration_id, "evidence_id": item["evidence_id"], "definition_id": item["definition_id"], "evidence_ref": item["evidence_ref"], "content_digest": item["content_digest"], "polarity": "negative" if negative else "positive", "source_kind": "rule_intelligence", "payload_json": json.dumps(_without_evidence_body({"evidence_ref": item["evidence_ref"], "content_digest": item["content_digest"], "source": str(source.path)}), ensure_ascii=False)})
                report.counts["negative_evidence" if negative else "evidence"] = report.counts.get("negative_evidence" if negative else "evidence", 0) + 1
        mappings = {"rule_runtime_feedback": "runtime_feedback", "rule_runtime_feedback_refs": "runtime_feedback", "rule_effective_feedback_projection": "effective_projection", "rule_merge_proposals": "merge_proposals", "rule_merge_decisions": "merge_decisions", "rule_merge_approvals": "merge_approvals", "rule_canonical_state": "canonical_state", "rule_reconciliation_jobs": "reconciliation_jobs", "rule_projection_state": "projection_checkpoints", "rule_projection_checkpoints": "projection_checkpoints", "rule_definition_aliases": "aliases", "rule_source_links": "source_links"}
        for table, category in mappings.items():
            for row in self._rows(source, table):
                item = dict(row)
                if table == "rule_runtime_feedback": self.store.record_runtime_feedback({**_without_evidence_body(item), "feedback_id": item.get("feedback_id") or stable_digest((source.path.as_posix(), table, row)), "definition_id": definition_map.get(str(item.get("definition_id", "")), item.get("definition_id", "")), "metadata_json": json.dumps(_without_evidence_body(item), ensure_ascii=False, default=str)})
                elif table == "rule_effective_feedback_projection": self.store.record_effective_projection({**_without_evidence_body(item), "definition_id": definition_map.get(str(item.get("definition_id", "")), item.get("definition_id", "")), "projection_digest": stable_digest(_without_evidence_body(item))})
                elif table == "rule_merge_proposals":
                    proposal_id = str(item.get("proposal_id") or stable_digest(item))
                    self.store.record_merge_proposal({"proposal_id": proposal_id, "definition_ids_json": json.dumps(_safe_json(item.get("definition_ids", item.get("definition_ids_json", []))), ensure_ascii=False), "status": item.get("status", "candidate"), "evidence_digest": item.get("evidence_digest", ""), "negative_digest": item.get("negative_digest", ""), "binding_digest": item.get("binding_digest", ""), "runtime_digest": item.get("runtime_digest", ""), "assessment_digest": stable_digest(item), "policy_version": item.get("policy_version", ""), "metadata_json": json.dumps(_merge_metadata(item, table=table, source_ref=str(source.path), source_row_id=proposal_id), ensure_ascii=False, default=str), "source_ref": str(source.path), "created_at": item.get("created_at", ""), "updated_at": item.get("last_evaluated_at", item.get("updated_at", ""))})
                elif table == "rule_merge_decisions":
                    decision_id = str(item.get("decision_id") or stable_digest(item))
                    self.store.record_merge_decision({"decision_id": decision_id, "proposal_id": item.get("proposal_id", ""), "canonical_definition_id": item.get("canonical_definition_id", ""), "merged_definition_ids_json": json.dumps(_safe_json(item.get("merged_definition_ids", item.get("merged_definition_ids_json", []))), ensure_ascii=False), "before_bindings_json": json.dumps(_safe_json(item.get("before_bindings", item.get("before_bindings_json", []))), ensure_ascii=False), "after_bindings_json": json.dumps(_safe_json(item.get("after_bindings", item.get("after_bindings_json", []))), ensure_ascii=False), "source_digest": item.get("source_digest", ""), "actor": item.get("actor", ""), "status": item.get("status", "merged"), "undo_state_digest": item.get("undo_state_digest", ""), "metadata_json": json.dumps(_merge_metadata(item, table=table, source_ref=str(source.path), source_row_id=decision_id), ensure_ascii=False, default=str), "created_at": item.get("created_at", ""), "undone_at": item.get("undone_at", "")})
                elif table == "rule_merge_approvals": self.store.record_merge_approval({**_without_evidence_body(item), "approval_id": item.get("approval_id") or stable_digest(item), "expected_revisions_json": json.dumps(_safe_json(item.get("expected_definition_revisions", item.get("expected_revisions_json", {}))), ensure_ascii=False), "created_at": item.get("created_at", "")})
                elif table == "rule_definition_aliases": self.store.record_alias(str(item.get("old_definition_id", "")), definition_map.get(str(item.get("new_definition_id", "")), str(item.get("new_definition_id", ""))), migration_decision_id=str(item.get("migration_decision_id", "")), source_ref=str(source.path))
                elif table == "rule_source_links": self.store.upsert_source_link(source_kind="rule_intelligence", share_group_id=item.get("share_group_id", ""), memory_id=item.get("memory_id", ""), source_ref=str(source.path), source_revision=item.get("source_revision", ""), original_definition_id=item.get("original_definition_id", ""), canonical_definition_id=definition_map.get(str(item.get("canonical_definition_id", "")), item.get("canonical_definition_id", "")), metadata_json=json.dumps(_without_evidence_body(item), ensure_ascii=False, default=str))
                elif table in {"rule_canonical_state", "rule_reconciliation_jobs", "rule_projection_state", "rule_projection_checkpoints"}:
                    if table.startswith("rule_canonical"): self.store.record_canonical_state({**_without_evidence_body(item), "scope_id": item.get("scope_id") or item.get("share_group_id") or stable_digest(item), "canonical_digest": item.get("canonical_digest", ""), "read_path": "legacy", "activation_status": "shadow"})
                    elif table.startswith("rule_reconciliation"): self.store.record_reconciliation_job({**_without_evidence_body(item), "job_id": item.get("job_id") or stable_digest(item), "migration_id": self.migration_id})
                    else: self.store.record_projection_checkpoint({**_without_evidence_body(item), "checkpoint_id": item.get("checkpoint_id") or item.get("scope_id") or stable_digest(item)})
                report.counts[category] = report.counts.get(category, 0) + 1
        known_columns = {
            "rule_definitions": {"definition_id", "canonical_text", "normalized_intent", "rule_kind", "polarity", "semantic_hash", "parameter_schema", "status", "confidence", "revision", "rule_strength", "maturity_state", "created_at", "updated_at", "superseded_by"},
            "rule_bindings": {"binding_id", "definition_id", "share_group_id", "target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority", "owner_agent_id", "created_by", "authorization", "status", "revision", "created_at", "updated_at"},
            "rule_binding_contributions": {"contribution_id", "binding_id", "definition_id", "share_group_id", "source_memory_id", "source_revision", "legacy_assignment_hash", "target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority", "owner_agent_id", "audience", "audience_json", "active", "status", "revision", "created_at", "updated_at"},
            "rule_evidence": {"evidence_id", "definition_id", "source_rule_id", "agent_instance_id", "project_ref", "provider", "session_id", "receipt_id", "content_hash", "semantic_hash", "confidence", "observed_at", "independence_key", "share_group_id", "source_root_id", "source_object_id", "session_trusted", "feedback_id", "feedback_authority", "active"},
            "rule_negative_evidence": {"evidence_id", "definition_id", "source_rule_id", "agent_instance_id", "project_ref", "content_hash", "confidence", "observed_at", "independence_key", "share_group_id", "session_id", "receipt_id", "feedback_id", "feedback_authority", "source_root_id", "source_object_id", "session_trusted", "active"},
            "rule_runtime_feedback": {"feedback_id", "definition_id", "receipt_id", "outcome", "agent_instance_id", "project_ref", "session_id", "source", "authority", "session_trusted", "created_at"},
            "rule_effective_feedback_projection": {"receipt_id", "effective_feedback_id", "definition_id", "outcome", "positive_evidence_id", "negative_evidence_id", "session_trusted", "session_source", "updated_at"},
            "rule_merge_proposals": {"proposal_id", "definition_ids", "similarity_score", "evidence_count", "agent_count", "project_count", "contradiction_score", "readiness_score", "readiness_components", "readiness_digest", "governance_reasons", "cooldown_until", "status", "explanation", "created_at", "candidate_since", "last_evaluated_at", "assessment_revision", "definition_revision_a", "definition_revision_b", "evidence_digest", "negative_digest", "binding_digest", "runtime_digest", "policy_version", "weight_breakdown", *_MERGE_PROPOSAL_METADATA_COLUMNS},
            "rule_merge_decisions": {"decision_id", "proposal_id", "canonical_definition_id", "merged_definition_ids", "before_bindings", "after_bindings", "migration", "actor", "status", "created_at", "undone_at", *_MERGE_DECISION_METADATA_COLUMNS},
            "rule_merge_approvals": {"approval_id", "proposal_id", "approved_by", "capability_id", "expected_definition_revisions", "approval_scope", "created_at", "expires_at"},
            "rule_definition_aliases": {"old_definition_id", "new_definition_id", "migration_decision_id", "created_at"},
            "rule_source_links": {"share_group_id", "memory_id", "source_revision", "original_definition_id", "canonical_definition_id", "status", "created_at", "updated_at"},
            "rule_canonical_state": {"share_group_id", "activation_status", "canonical_digest", "read_path", "activated_at", "updated_at"},
            "rule_reconciliation_jobs": {"job_id", "share_group_id", "source_digest", "status", "phase", "attempt_count", "model_mode", "result_json", "canonical_digest_before", "canonical_digest_after", "projection_version", "last_error", "reason", "created_at", "updated_at"},
            "rule_projection_state": {"scope_id", "last_outbox_event_id", "last_projected_event_id", "projection_lag", "projection_error", "updated_at"},
            "rule_projection_checkpoints": {"checkpoint_id", "scope_id", "last_event_id", "projection_digest", "status", "error", "updated_at"},
            "agent_reputation": {"agent_id", "success_rate", "rule_accuracy", "violation_rate", "sample_count", "feedback_quality", "created_at", "updated_at"},
            "project_profile": {"project_ref", "production_level", "criticality", "owner_verified", "created_at", "updated_at"},
            "rule_definition_versions": {"version_id", "definition_id", "superseded_by", "old_strength", "new_strength", "change_reason", "actor", "evidence", "revision", "reason", "source_ref", "created_at"},
            "rule_definition_runtime_stats": {"definition_id", "followed", "violated", "not_applicable", "exception_count", "distinct_sessions", "distinct_projects", "last_observed_at"},
            "rule_evidence_contributions": {"contribution_id", "definition_id", "independence_key", "kind", "polarity", "authority", "confidence", "observed_at", "active", "receipt_id", "feedback_id", "source_rule_id", "source_evidence_id", "source_memory_id", "source_ids", "agent_instance_id", "project_ref", "share_group_id", "session_id", "source_root_id", "source_object_id", "session_trusted", "created_at", "updated_at"},
            "rule_evidence_effective": {"definition_id", "independence_key", "kind", "winner_contribution_id", "polarity", "authority", "confidence", "observed_at", "updated_at"},
            "governance_capabilities": {"token_hash", "principal", "scope", "proposal_id", "nonce", "issued_at", "expires_at", "consumed", "consumed_at", "recovery_proof_hash", "token_version", "revoked"},
            "rule_merge_native_requests": {"request_key", "request_fingerprint", "operation", "schema_version", "status", "result_json", "created_at", "updated_at"},
        }
        for table, rows in source.rows.items():
            extra_columns = [col for col in source.columns.get(table, ()) if col not in known_columns.get(table, set())]
            if not extra_columns:
                continue
            for col in extra_columns:
                row_values = rows or ({},)
                identity_columns = ("definition_id", "binding_id", "evidence_id", "event_id", "memory_id", "rule_id", "receipt_id", "feedback_id", "source_id", "id", "key")
                candidate_ids = [next((str(row.get(column) or "") for column in identity_columns if str(row.get(column) or "")), "") for row in row_values]
                duplicate_ids = {item for item in candidate_ids if item and candidate_ids.count(item) > 1}
                for index, row in enumerate(row_values):
                    source_row_id = candidate_ids[index]
                    if source_row_id in duplicate_ids:
                        occurrence = (source.row_ids.get(table, ()) + ())[index] if index < len(source.row_ids.get(table, ())) else stable_digest((table, source.columns.get(table, ()), row, index))
                        source_row_id = f"{source_row_id}#occ-{occurrence}"
                    if not source_row_id:
                        occurrence = source.row_ids.get(table, ())
                        source_row_id = occurrence[index] if index < len(occurrence) else stable_digest((table, source.columns.get(table, ()), row, index))
                    legacy_weak = self.store._read(lambda conn: conn.execute(
                        "SELECT ledger_id FROM rule_unknown_columns_ledger WHERE migration_id=? AND source_path=? AND source_table=? AND source_row_id='' AND column_name=?",
                        (self.migration_id, str(source.path), table, col),
                    ).fetchone())
                    if legacy_weak is not None:
                        self.store.record_unknown_column_anomaly({"migration_id": self.migration_id, "source_path": str(source.path), "source_table": table, "column_name": col, "legacy_ledger_id": str(legacy_weak[0]), "status": "PRESERVED", "details_json": json.dumps({"reason": "weak_source_row_id", "new_source_row_id": source_row_id}, ensure_ascii=False, sort_keys=True), "created_at": ""})
                    ledger_id = self.store.record_unknown_column({"migration_id": self.migration_id, "source_kind": "rule_intelligence", "source_path": str(source.path), "source_table": table, "source_row_id": source_row_id, "column_name": col, "value_digest": stable_digest(row.get(col)), "status": "PRESERVED" if (table in {"rule_merge_proposals", "rule_merge_decisions"} and col == _MERGE_RATIONALE_COLUMN) else "NOT_MIGRATED", "created_at": ""})
                    if table in {"rule_merge_proposals", "rule_merge_decisions"} and col == _MERGE_RATIONALE_COLUMN:
                        # ``judge_rationale`` is free-form body text.  The
                        # structured merge metadata stores only its digest and
                        # source reference; this occurrence-bound anomaly is
                        # the explicit evidence that no equivalent body column
                        # exists in the V2 store.
                        self.store.record_unknown_column_anomaly({"migration_id": self.migration_id, "source_path": str(source.path), "source_table": table, "column_name": col, "legacy_ledger_id": ledger_id, "status": "PRESERVED", "details_json": json.dumps({"reason": "body_free_metadata_only", "source_row_id": source_row_id, "value_digest": stable_digest(row.get(col)), "target_table": table, "target_metadata": "metadata_json.judge_rationale_digest"}, ensure_ascii=False, sort_keys=True), "created_at": ""})
                    report.unknown_columns += 1

    def migrate(self, *, evidence_sink: Any | None = None) -> RulesMigrationReport:
        sources = self.read_sources()
        source_hashes = {str(source.path): source.digest for source in sources}
        source_digest = stable_digest(source_hashes)
        self.migration_id = self.migration_id or f"rules-{source_digest[:24]}"
        report = RulesMigrationReport(self.migration_id, "BUILDING", source_digest=source_digest, source_hashes=source_hashes)
        already_migrated = bool(self.store._read(lambda conn: conn.execute("SELECT 1 FROM rule_migration_map WHERE migration_id=? LIMIT 1", (self.migration_id,)).fetchone()))
        definitions: dict[tuple[str, str, str], str] = {}
        try:
            with self.store.transaction():
                self._fault("before_stage")
                for source in sources:
                    if source.kind == "shared_memory": self._stage_shared_memory(source, report, definitions)
                    else: self._stage_rule_intelligence(source, report, definitions)
                self._fault("after_stage")
                # Migration domain events are audit evidence for the build
                # transaction, not a runtime pending queue.  Once the staging
                # transaction commits, mark these migration-owned events as
                # consumed so readiness does not confuse completed migration
                # evidence with an unfinished projection.
                self.store._write(lambda conn: conn.execute("UPDATE rule_domain_outbox SET consumed_at=? WHERE migration_id=? AND (consumed_at='' OR consumed_at IS NULL)", (_now(), self.migration_id)))
                self._fault("before_commit")
        except Exception as exc:
            report.status = "ROLLED_BACK"
            report.errors.append(str(exc))
            self.last_report = report
            raise RulesMigrationError(str(exc)) from exc
        sink = evidence_sink if evidence_sink is not None else self.evidence_sink
        report.evidence_status = "NOT_EVALUATED"
        if sink is not None:
            try:
                report.evidence_status = "PASS"
                result = EvidenceProjector(self.store, sink).project(migration_id=self.migration_id)
                report.evidence_pending = int(result.get("pending", 0))
                if report.evidence_pending: report.evidence_status = "FAILED"
            except EvidenceProjectionError as exc:
                report.evidence_status = "FAILED"; report.errors.append(str(exc)); report.evidence_pending = len(self.store.list_evidence_outbox(migration_id=self.migration_id, unconsumed=True))
        report.binding_multiset_diff = sum((report.binding_multiset_before - report.binding_multiset_after).values()) + sum((report.binding_multiset_after - report.binding_multiset_before).values())
        metrics = self.store.metrics(); report.system_auto_expansion = int(metrics.get("system_auto_expansion", 0)); report.unknown_columns = max(report.unknown_columns, int(metrics.get("rule_unknown_columns_ledger", 0)))
        target_fences = self.store._read(lambda conn: [dict(row) for row in conn.execute("SELECT key,request_fingerprint,memory_id,event_id,decision_id,created_at,share_group_id,source_ref FROM rule_idempotency_fences").fetchall()])
        source_counter = Counter(report._idempotency_fence_rows)
        # A conflict row marks a canonical key as having historical siblings.
        # Ignore only target siblings that are not present in the current
        # source snapshot; unrelated or unmarked extras remain loss.
        conflict_keys = {
            (str(row.get("share_group_id", "") or ""), self._canonical_fence_key(row.get("key", "")))
            for row in target_fences
            if "#conflict-" in str(row.get("key", "") or "")
        }
        target_fence_rows = []
        for row in target_fences:
            identity = self._idempotency_fence_identity(str(row.get("share_group_id", "") or ""), str(row.get("source_ref", "") or ""), row)
            key = (identity[0], identity[1])
            if key in conflict_keys and identity not in source_counter:
                continue
            target_fence_rows.append(identity)
        target_counter = Counter(target_fence_rows)
        report.idempotency_fence_source_digest = stable_digest(sorted(source_counter.items())) if source_counter else ""
        report.idempotency_fence_target_digest = stable_digest(sorted(target_counter.items())) if target_counter else ""
        report.idempotency_fence_loss = sum((source_counter - target_counter).values()) + sum((target_counter - source_counter).values()) if (source_counter or target_counter) else None
        report.target_digest = stable_digest(metrics)
        report.validation = {"status": ("PASS_WITH_PRESERVED_CONFLICT" if report.idempotency_fence_conflicts and report.idempotency_fence_loss in (None, 0) else ("PASS" if report.binding_multiset_diff == 0 and report.system_auto_expansion == 0 and (report.idempotency_fence_loss in (None, 0)) else "FAIL")), "read_path": "legacy", "canonical_state": "shadow", "loss": "NOT_EVALUATED", "unknown_columns": report.unknown_columns, "idempotency_fence_loss": report.idempotency_fence_loss, "idempotency_fence_conflicts": report.idempotency_fence_conflicts}
        report.status = "UNVERIFIED" if report.evidence_status == "FAILED" else ("IDEMPOTENT" if already_migrated else "MIGRATED")
        self.last_report = report
        return report

    run = migrate
    migrate_rules = migrate


def _dict_row(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


__all__ = ["MigrationReadError", "RulesMigrationError", "RulesMigrationReport", "V1RulesMigrator"]
