"""Authoritative V2 domain registry used by the reference audit.

The registry is intentionally data-only.  It is the one place where the
maintenance plane knows the lexical database paths, schema marker strategy and
authoritative table/column allow-list for all V2 domains (including skills).
Reference audit code never instantiates a domain Store.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping




@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    required_columns: frozenset[str]
    columns: frozenset[str]
    primary_key: str = ""
    key_columns: tuple[str, ...] = ("rowid",)

    def __post_init__(self) -> None:
        if not self.name or any(ch in self.name for ch in "\x00 ;\"'"):
            raise ValueError("invalid authoritative table name")
        required = frozenset(self.required_columns)
        columns = frozenset(self.columns)
        if not required or not columns:
            raise ValueError(f"authoritative table {self.name} requires an exact non-empty column set")
        if required != columns:
            raise ValueError(f"required columns must equal exact columns for {self.name}")
        for column in columns:
            if not column or any(ch in column for ch in "\x00 ;\"'"):
                raise ValueError(f"invalid authoritative column name for {self.name}")
        if self.primary_key and self.primary_key not in columns:
            raise ValueError(f"primary key is not in columns for {self.name}")
        if any(key != "rowid" and key not in columns for key in self.key_columns):
            raise ValueError(f"keyset column is not in columns for {self.name}")
        object.__setattr__(self, "required_columns", required)
        object.__setattr__(self, "columns", columns)
        object.__setattr__(self, "key_columns", tuple(self.key_columns) or ("rowid",))

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required_columns": sorted(self.required_columns),
            "columns": sorted(self.columns),
            "primary_key": self.primary_key,
            "key_columns": list(self.key_columns),
        }


@dataclass(frozen=True, slots=True)
class DomainSpec:
    name: str
    relative_path: str
    db_name: str
    marker: tuple[str, ...]
    version: int | tuple[int, ...]
    tables: Mapping[str, TableSpec]
    json_columns: tuple[str, ...] = ()
    reference_keys: frozenset[str] = frozenset()
    outbox_tables: tuple[str, ...] = ()
    unknown_ledger_tables: tuple[str, ...] = ()
    migration_tables: tuple[str, ...] = ()
    reference_rules: tuple["ReferenceRule", ...] = ()
    optional_tables: frozenset[str] = frozenset()
    supported_user_versions: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("invalid domain name")
        if Path(self.relative_path).is_absolute() or ".." in Path(self.relative_path).parts:
            raise ValueError("domain path must be a fixed relative path")
        markers = (self.marker,) if isinstance(self.marker, str) else tuple(self.marker)
        if not markers or any(not isinstance(v, str) or not v for v in markers):
            raise ValueError("domain marker is required")
        versions = (self.version,) if isinstance(self.version, int) else tuple(self.version)
        if not versions or any(type(v) is not int or v < 1 for v in versions):
            raise ValueError("domain version is required")
        tables = dict(self.tables)
        if not tables:
            raise ValueError("authoritative domain requires tables")
        if self.optional_tables:
            raise ValueError("authoritative V2 target tables cannot be optional")
        object.__setattr__(self, "marker", markers)
        object.__setattr__(self, "version", versions[0] if len(versions) == 1 else versions)
        object.__setattr__(self, "tables", MappingProxyType(tables))
        object.__setattr__(self, "json_columns", tuple(self.json_columns))
        object.__setattr__(self, "reference_keys", frozenset(self.reference_keys))
        object.__setattr__(self, "outbox_tables", tuple(self.outbox_tables))
        object.__setattr__(self, "unknown_ledger_tables", tuple(self.unknown_ledger_tables))
        object.__setattr__(self, "migration_tables", tuple(self.migration_tables))
        object.__setattr__(self, "reference_rules", tuple(self.reference_rules))
        object.__setattr__(self, "optional_tables", frozenset(self.optional_tables))
        object.__setattr__(self, "supported_user_versions", tuple(self.supported_user_versions) or versions)

    @property
    def path(self) -> str:
        return str(Path(self.relative_path) / self.db_name)

    def to_dict(self) -> dict[str, object]:
        version: object = self.version
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "db_name": self.db_name,
            "marker": list(self.marker),
            "version": version,
            "tables": {k: self.tables[k].to_dict() for k in sorted(self.tables)},
            "json_columns": list(self.json_columns),
            "reference_keys": sorted(self.reference_keys),
            "outbox_tables": list(self.outbox_tables),
            "unknown_ledger_tables": list(self.unknown_ledger_tables),
            "migration_tables": list(self.migration_tables),
            "reference_rules": [rule.to_dict() for rule in self.reference_rules],
            "optional_tables": sorted(self.optional_tables),
            "supported_user_versions": list(self.supported_user_versions),
        }


@dataclass(frozen=True, slots=True)
class ReferenceRule:
    source_table: str
    source_column: str
    target_domain: str
    target_table: str
    target_column: str
    kind: str = "logical"
    discriminator_column: str = ""
    discriminator_values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"source_table": self.source_table, "source_column": self.source_column, "target_domain": self.target_domain, "target_table": self.target_table, "target_column": self.target_column, "kind": self.kind, "discriminator_column": self.discriminator_column, "discriminator_values": list(self.discriminator_values)}


def _t(name: str, columns: Iterable[str], *, primary_key: str = "", key_columns: tuple[str, ...] = ("rowid",)) -> TableSpec:
    values = frozenset(columns)
    return TableSpec(name, values, values, primary_key, key_columns)


_COMMON_REF_KEYS = frozenset({
    "id", "ref", "ref_id", "reference_id", "source_ref", "source_id", "source_object_id",
    "target_id", "target_ref", "blob_id", "asset_id", "version_id", "evidence_id",
    "skill_id", "run_id", "node_id", "definition_id", "binding_id", "document_id",
    "namespace_id", "occurrence_id", "projection_id", "profile_key", "scenario_key",
    "aggregate_id", "aggregate_type", "subject_id", "subject_type", "old_id", "new_id",
})


# The table sets include the Phase-1 tables and the durable Phase-2/5/6
# ledgers. Every authoritative table below has one exact static column set;
# marker/version checks never substitute for column validation.
_TABLES: dict[str, dict[str, TableSpec]] = {
    "runtime": {
        "schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"),
        "task_runs": _t("task_runs", ("run_id", "task_type", "state", "requested_by", "started_at", "finished_at", "error_json", "created_at"), primary_key="run_id"),
        "task_nodes": _t("task_nodes", ("node_id", "run_id", "parent_node_id", "node_type", "state", "input_json", "output_json", "error_json", "created_at"), primary_key="node_id"),
        "tool_refs": _t("tool_refs", ("tool_ref_id", "run_id", "node_id", "provider", "tool_name", "request_digest", "response_digest", "metadata_json", "created_at"), primary_key="tool_ref_id"),
        "runtime_v2_schema_meta": _t("runtime_v2_schema_meta", ("key", "value"), primary_key="key"),
        "task_events": _t("task_events", ("event_id", "run_id", "node_id", "sequence", "event_type", "payload_json", "created_at"), primary_key="event_id", key_columns=("sequence", "event_id")),
        "task_heads": _t("task_heads", ("head_id", "run_id", "node_id", "state", "generation", "last_event_seq", "updated_at"), primary_key="head_id"),
        "task_refs": _t("task_refs", ("ref_id", "node_id", "ref_kind", "ref_value", "ref_hash", "relation", "created_at"), primary_key="ref_id"),
        "working_checkpoints": _t("working_checkpoints", ("checkpoint_id", "run_id", "node_id", "checkpoint_key", "checkpoint_digest", "state_json", "created_at"), primary_key="checkpoint_id"),
        "task_idempotency": _t("task_idempotency", ("idempotency_key", "operation", "run_id", "node_id", "payload_digest", "result_json", "created_at"), primary_key="idempotency_key"),
    },
    "memory": {
        "schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"),
        "memory_schema_meta": _t("memory_schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"),
        "atoms": _t("atoms", ("atom_id", "memory_id", "canonical_hash", "kind", "state", "current_revision", "scope_digest", "created_at", "updated_at"), primary_key="atom_id"),
        "atom_revisions": _t("atom_revisions", ("revision_id", "atom_id", "revision", "body_digest", "evidence_digest", "state", "created_at"), primary_key="revision_id"),
        "atom_deltas": _t("atom_deltas", ("delta_id", "atom_id", "from_revision", "to_revision", "delta_digest", "created_at"), primary_key="delta_id"),
        "supersession_edges": _t("supersession_edges", ("edge_id", "old_atom_id", "new_atom_id", "reason", "created_at"), primary_key="edge_id"),
        "scope_acl": _t("scope_acl", ("acl_id", "atom_id", "workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "runtime_role", "effect", "created_at"), primary_key="acl_id"),
        "source_mappings": _t("source_mappings", ("mapping_id", "atom_id", "source_ref", "source_revision", "created_at"), primary_key="mapping_id"),
        "domain_outbox": _t("domain_outbox", ("event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"), primary_key="event_id", key_columns=("sequence", "event_id")),
        "outbox_checkpoints": _t("outbox_checkpoints", ("domain", "last_sequence", "updated_at"), primary_key="domain"),
        "evidence_projection_receipts": _t("evidence_projection_receipts", ("event_id", "projected_at", "status", "error_json"), primary_key="event_id"),
        "domain_state": _t("domain_state", ("domain", "state", "updated_at"), primary_key="domain"),
    },
    "rules": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "rules_schema_meta": _t("rules_schema_meta", ("schema_id", "version", "marker", "updated_at"), primary_key="schema_id")},
    "evidence": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "evidence_schema_meta": _t("evidence_schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "evidence": _t("evidence", ("evidence_id", "evidence_type", "source_ref", "source_revision", "digest", "authority", "status", "observed_at", "invalidated_at", "metadata_json"), primary_key="evidence_id"), "evidence_links": _t("evidence_links", ("link_id", "evidence_id", "subject_type", "subject_id", "relation", "created_at"), primary_key="link_id"), "migration_map": _t("migration_map", ("map_id", "source_domain", "source_ref", "source_id", "target_type", "target_id", "status", "created_at"), primary_key="map_id"), "domain_outbox": _t("domain_outbox", ("event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"), primary_key="event_id", key_columns=("sequence", "event_id")), "outbox_checkpoints": _t("outbox_checkpoints", ("domain", "last_sequence", "updated_at"), primary_key="domain"), "audit_refs": _t("audit_refs", ("audit_id", "source_ref", "digest", "created_at"), primary_key="audit_id")},
    "content": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain")},
    "knowledge": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain")},
    "codegraph": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain")},
    "assets": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "asset_schema_meta": _t("asset_schema_meta", ("key", "value"), primary_key="key"), "assets": _t("assets", ("asset_id", "asset_key", "asset_kind", "namespace_id", "workspace_id", "agent_instance_id", "project_ref", "provider", "share_group_id", "runtime_role", "acl_digest", "state", "metadata_json", "created_at", "updated_at"), primary_key="asset_id"), "asset_versions": _t("asset_versions", ("version_id", "asset_id", "version_key", "version", "content_hash", "size_bytes", "metadata_json", "created_at"), primary_key="version_id"), "asset_references": _t("asset_references", ("reference_id", "asset_id", "version_id", "reference_kind", "target_id", "target_hash", "metadata_json", "created_at"), primary_key="reference_id"), "asset_holds": _t("asset_holds", ("hold_id", "asset_id", "version_id", "reason", "source_ref", "active", "created_at", "released_at"), primary_key="hold_id"), "asset_migration_map": _t("asset_migration_map", ("map_id", "source_domain", "source_ref", "source_id", "target_type", "target_id", "status", "created_at"), primary_key="map_id"), "asset_outbox": _t("asset_outbox", ("event_id", "aggregate_type", "aggregate_id", "event_type", "payload_hash", "payload_json", "status", "attempts", "created_at", "updated_at"), primary_key="event_id"), "asset_unknown_ledger": _t("asset_unknown_ledger", ("unknown_id", "source_domain", "source_ref", "field", "value", "reason", "status", "created_at"), primary_key="unknown_id")},
    "scenario": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "scenario_projections": _t("scenario_projections", ("projection_id", "scenario_key", "generation", "source_digest", "projection_digest", "status", "payload_json", "error_json", "created_at", "updated_at"), primary_key="projection_id")},
    "profile": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "profile_projections": _t("profile_projections", ("projection_id", "profile_key", "generation", "source_digest", "projection_digest", "status", "payload_json", "error_json", "created_at", "updated_at"), primary_key="projection_id")},
    "system": {
        "schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"),
        "manifest": _t("manifest", ("manifest_id", "state", "generation", "migration_id", "source_digest", "target_digest", "manifest_digest", "digests_json", "errors_json", "last_error", "workspace_source_pointer", "global_source_pointer", "data_home_root", "checkpoints_json", "created_at", "updated_at"), primary_key="manifest_id"),
        "migration_ledger": _t("migration_ledger", ("transition_id", "migration_id", "from_state", "to_state", "generation", "source_digest", "target_digest", "status", "error_json", "started_at", "completed_at"), primary_key="transition_id"),
        "outbox_checkpoints": _t("outbox_checkpoints", ("checkpoint_id", "domain", "last_sequence", "updated_at"), primary_key="checkpoint_id"),
        "gui_control_schema_meta": _t("gui_control_schema_meta", ("key", "value"), primary_key="key"),
        "agent_group_bindings": _t("agent_group_bindings", ("binding_id", "agent_instance_id", "share_group_id", "group_kind", "mcp_server_name", "native_memory_mode", "redirect_paths_json", "status", "revision", "created_at", "updated_at"), primary_key="binding_id"),
        "governance_scopes": _t("governance_scopes", ("scope_id", "principal_agent_id", "mode", "agent_instance_id", "share_group_id", "revision", "created_at", "updated_at"), primary_key="scope_id"),
        "selection_manifests": _t("selection_manifests", ("selection_id", "agent_instance_id", "selection_digest", "source_ids_json", "status", "created_at", "updated_at"), primary_key="selection_id"),
        "control_preferences": _t("control_preferences", ("pref_key", "value_json", "revision", "updated_at"), primary_key="pref_key"),
        "group_operation_receipts": _t("group_operation_receipts", ("receipt_id", "operation", "idempotency_key", "request_digest", "result_json", "created_at"), primary_key="receipt_id"),
        "group_outbox": _t("group_outbox", ("event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at"), primary_key="event_id", key_columns=("sequence", "event_id")),
        "agent_lifecycle_marks": _t("agent_lifecycle_marks", ("candidate_id", "product", "dir_path", "status", "reason", "updated_at"), primary_key="candidate_id"),
        "agent_archives": _t("agent_archives", ("archive_id", "candidate_id", "product", "original_path", "archive_path", "status", "created_at", "updated_at"), primary_key="archive_id"),
        "agent_cleanup_history": _t("agent_cleanup_history", ("event_id", "operation", "candidate_id", "archive_id", "status", "detail_json", "created_at"), primary_key="event_id"),
    },
    "skills": {"schema_meta": _t("schema_meta", ("domain", "version", "marker", "updated_at"), primary_key="domain"), "skill_definitions": _t("skill_definitions", ("skill_id", "stable_key", "name", "namespace", "current_version", "current_version_id", "state", "stable_hash", "created_at", "updated_at"), primary_key="skill_id"), "skill_versions": _t("skill_versions", ("version_id", "skill_id", "version", "description", "declaration_json", "entrypoint_ref", "entrypoint_hash", "content_hash", "capabilities_json", "execution_policy_json", "created_at"), primary_key="version_id"), "skill_evidence_refs": _t("skill_evidence_refs", ("ref_id", "version_id", "evidence_id", "source_ref", "digest", "revision", "authority"), primary_key="ref_id"), "skill_asset_refs": _t("skill_asset_refs", ("ref_id", "version_id", "asset_id", "path", "digest", "asset_kind"), primary_key="ref_id"), "domain_outbox": _t("domain_outbox", ("event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"), primary_key="event_id", key_columns=("sequence", "event_id")), "outbox_checkpoints": _t("outbox_checkpoints", ("domain", "last_sequence", "updated_at"), primary_key="domain"), "migration_map": _t("migration_map", ("map_id", "source_path", "source_hash", "source_kind", "skill_id", "version_id", "metadata_json", "created_at"), primary_key="map_id"), "unknown_ledger": _t("unknown_ledger", ("unknown_id", "source_path", "field_name", "value_hash", "details_json", "created_at"), primary_key="unknown_id")},
}


# Stable content identity tables are fully pinned; compatibility payload tables
# remain store-owned above.  This catches ALTER-added columns on the blob/hold
# path that feeds candidate computation.
_CONTENT_STRICT = {
    "content_namespaces": ("namespace_id", "workspace_id", "trust_domain", "sensitivity", "retention_authority", "canonicalization_version", "created_at"),
    "content_blobs": ("blob_id", "namespace_id", "canonical_hash", "normalizer_id", "text", "byte_count", "char_count", "language_hint", "created_at"),
    "content_occurrences": ("occurrence_id", "source_object_id", "occurrence_key", "blob_id", "source_revision", "ordinal", "locator_json", "content_role", "sensitivity", "workspace_id", "agent_instance_id", "project_ref", "share_group_id", "policy_class", "access_scope_json", "active", "first_seen_at", "last_seen_at", "deleted_scan_id", "provider"),
    "content_holds": ("hold_id", "blob_id", "reason", "source_ref", "active", "created_at", "released_at"),
    "content_tombstones": ("tombstone_id", "source_object_id", "occurrence_id", "blob_id", "reason", "scan_id", "metadata_json", "created_at", "restored_at", "active"),
    "history_mutation_receipts": ("idempotency_key", "operation", "payload_digest", "result_json", "created_at"),
}
for _table_name, _columns in _CONTENT_STRICT.items():
    _TABLES["content"][_table_name] = TableSpec(
        _table_name,
        frozenset(_columns),
        frozenset(_columns),
        primary_key=_columns[0],
    )

_RUNTIME_MEMORY_CONTENT_STRICT: dict[str, dict[str, tuple[str, ...]]] = {
    "runtime": {
        "runtime_v2_schema_meta": ("key", "value"), "schema_meta": ("domain", "version", "marker", "updated_at"),
        "task_events": ("event_id", "run_id", "node_id", "event_seq", "event_type", "payload_json", "idempotency_key", "created_at"),
        "task_heads": ("head_id", "run_id", "node_id", "state", "generation", "last_event_seq", "updated_at"),
        "task_idempotency": ("idempotency_key", "operation", "run_id", "node_id", "payload_digest", "result_json", "created_at"),
        "task_nodes": ("node_id", "run_id", "parent_node_id", "node_type", "state", "input_json", "output_json", "error_json", "created_at", "goal", "depends_json", "blocker_json", "result_ref_json", "importance"),
        "task_refs": ("ref_id", "node_id", "ref_kind", "ref_value", "ref_hash", "relation", "created_at"),
        "task_runs": ("run_id", "task_type", "state", "requested_by", "started_at", "finished_at", "error_json", "created_at", "goal", "importance", "workspace_id", "agent_instance_id", "project_ref", "share_group_id", "provider", "runtime_scope"),
        "tool_refs": ("tool_ref_id", "run_id", "node_id", "provider", "tool_name", "request_digest", "response_digest", "metadata_json", "created_at"),
        "working_checkpoints": ("checkpoint_id", "run_id", "node_id", "checkpoint_key", "checkpoint_digest", "state_json", "created_at"),
    },
    "memory": {
        "atom_deltas": ("delta_id", "atom_id", "from_revision", "to_revision", "delta_json", "created_at"), "atom_revisions": ("revision_id", "atom_id", "revision", "body", "status", "canonical_hash", "revision_digest", "metadata_json", "created_at"),
        "atoms": ("atom_id", "memory_id", "body", "kind", "status", "confidence", "locked", "injection_policy", "priority", "canonical_hash", "dedup_domain", "supersedes_json", "provenance_json", "metadata_json", "revision", "visibility", "created_at", "updated_at", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role"),
        "domain_outbox": ("event_id", "sequence", "event_type", "aggregate_id", "payload_json", "status", "attempts", "created_at", "projected_at", "error_json"), "domain_state": ("domain", "state", "generation", "updated_at", "metadata_json"), "evidence_projection_receipts": ("event_id", "evidence_id", "projected_at", "error_json"), "memory_atoms": ("atom_id", "canonical_hash", "kind", "body_ref", "status", "injection_policy", "priority", "revision", "metadata_json", "created_at", "updated_at"), "memory_schema_meta": ("domain", "version", "marker", "updated_at"), "outbox_checkpoints": ("domain", "last_sequence", "updated_at"), "schema_meta": ("domain", "version", "marker", "updated_at"), "scope_acl": ("acl_id", "atom_id", "workspace_id", "agent_instance_id", "share_group_id", "project_ref", "provider", "runtime_role", "effect", "metadata_json", "created_at"), "source_mappings": ("mapping_id", "atom_id", "source_domain", "source_ref", "source_record_id", "source_revision", "digest", "provenance_json", "created_at"), "supersession_edges": ("edge_id", "old_atom_id", "new_atom_id", "reason", "source_ref", "created_at"),
    },
    "content": {
        "content_acl_anomalies": ("anomaly_id", "occurrence_id", "field", "value", "reason", "created_at"), "content_blobs": ("blob_id", "namespace_id", "canonical_hash", "normalizer_id", "text", "byte_count", "char_count", "language_hint", "created_at"), "content_evidence_links": ("link_id", "memory_id", "session_id", "turn_id", "occurrence_id", "status", "created_at", "invalidated_at"), "content_holds": ("hold_id", "blob_id", "reason", "source_ref", "active", "created_at", "released_at"), "content_namespaces": ("namespace_id", "workspace_id", "trust_domain", "sensitivity", "retention_authority", "canonicalization_version", "created_at"), "content_occurrences": ("occurrence_id", "source_object_id", "occurrence_key", "blob_id", "source_revision", "ordinal", "locator_json", "content_role", "sensitivity", "workspace_id", "agent_instance_id", "project_ref", "share_group_id", "policy_class", "access_scope_json", "active", "first_seen_at", "last_seen_at", "deleted_scan_id", "provider"), "content_schema_meta": ("key", "value"), "content_tombstones": ("tombstone_id", "source_object_id", "occurrence_id", "blob_id", "reason", "scan_id", "metadata_json", "created_at", "restored_at", "active"), "conversation_observations": ("observation_id", "session_id", "turn_id", "occurrence_id", "observation_type", "summary_hash", "created_at"), "conversation_sessions": ("session_id", "source_object_id", "external_id", "title", "provider", "workspace_id", "agent_instance_id", "project_ref", "share_group_id", "policy_class", "created_at", "imported_at", "active"), "conversation_summaries": ("summary_id", "session_id", "occurrence_id", "summary_kind", "summary_hash", "updated_at"), "conversation_turns": ("turn_id", "occurrence_id", "session_ref", "role", "ordinal", "metadata_json", "created_at", "session_id", "event_key", "content_type", "source_revision"), "knowledge_records": ("record_id", "source_table", "source_pk", "record_type", "content_blob_id", "status", "derived_status", "metadata_json"), "knowledge_relations": ("relation_id", "source_table", "source_pk", "relation_type", "payload_json"), "migration_map": ("map_id", "source_db", "source_table", "source_pk", "target_type", "target_id", "source_hash", "target_hash", "acl_digest", "status", "metadata_json", "created_at", "updated_at"), "raw_content": ("raw_content_id", "blob_id", "source_ref", "content_kind", "metadata_json", "created_at"), "schema_meta": ("domain", "version", "marker", "updated_at"), "source_connectors": ("source_id", "workspace_id", "provider", "source_type", "external_root_key", "enabled", "created_at", "updated_at"), "source_manifest_items": ("source_id", "external_object_key", "occurrence_key", "source_revision", "content_hash", "active", "last_complete_scan_id"), "source_manifest_staging": ("run_id", "source_id", "external_object_key", "occurrence_key", "source_revision", "content_hash", "coverage_status", "reason"), "source_objects": ("source_object_id", "source_kind", "external_object_key", "title", "metadata_json", "active", "first_seen_at", "last_seen_at", "source_id", "object_type", "parent_object_id", "deleted_scan_id"), "source_sync_anomalies": ("source_id", "error_fingerprint", "error_code", "detail", "first_seen_at", "last_seen_at", "occurrence_count", "resolved_at"), "source_sync_state": ("source_id", "active_run_id", "state", "cursor", "last_complete_scan_id", "manifest_digest", "coverage_digest", "last_started_at", "last_finished_at", "last_error_code", "revision"),
    },
}
for _domain_name, _tables in _RUNTIME_MEMORY_CONTENT_STRICT.items():
    for _table_name, _columns in _tables.items():
        _TABLES[_domain_name][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

# Phase-3 sync proof and evidence identity are authoritative.  Keep these
# columns pinned even when an older compatibility table declaration above is
# present, so ReferenceAudit and blob sweep share the same contract.
for _table_name, _columns in {
    "content_evidence_links": ("link_id", "memory_id", "session_id", "turn_id", "occurrence_id", "blob_id", "source_revision", "status", "created_at", "invalidated_at"),
    "source_sync_state": ("source_id", "active_run_id", "owner_id", "state", "cursor", "cursor_digest", "cursor_source_id", "cursor_run_id", "cursor_owner_id", "cursor_revision", "cursor_position", "cursor_batch_digest", "expected_revision", "expected_manifest_digest", "last_complete_scan_id", "manifest_digest", "coverage_digest", "last_started_at", "last_finished_at", "last_error_code", "revision"),
}.items():
    _TABLES["content"][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

_FORMAL_STRICT: dict[str, dict[str, tuple[str, ...]]] = {
    "codegraph": {
        "affected_queries": ("query_id","scope_id","start_id","depth","result_limit","relation_filter","provenance_filter","result_json","result_digest","created_at"), "checkpoints": ("checkpoint_id","scope_id","domain","sequence","digest","updated_at"), "codegraph_edges": ("edge_id","from_node_id","to_node_id","edge_kind","weight","metadata_json","created_at"), "codegraph_nodes": ("node_id","node_kind","stable_key","label","source_ref","metadata_json","active","created_at","updated_at"), "codegraph_schema_meta": ("key","value"), "edges": ("edge_id","scope_id","revision_id","from_id","to_id","relation","context","provenance","source_location","metadata_json","weight","active","created_at"), "graph_scopes": ("scope_id","workspace_id","agent_instance_id","project_ref","provider","share_group_id","runtime_role","trusted_context","created_at"), "migration_map": ("map_id","source_db","source_table","source_pk","source_hash","target_id","target_type","status","created_at","updated_at"), "outbox": ("event_id","scope_id","sequence","event_type","aggregate_id","payload_hash","status","attempts","error","created_at","projected_at"), "revisions": ("revision_id","file_id","scope_id","content_hash","source_revision","revision_number","created_at"), "schema_meta": ("domain","version","marker","updated_at"), "source_files": ("file_id","scope_id","source_id","path","content_hash","source_revision","language","source_role","provenance","revision_id","active","created_at","updated_at"), "source_tombstones": ("tombstone_id","file_id","scope_id","reason","revision_id","created_at"), "symbols": ("symbol_id","file_id","scope_id","revision_id","name","kind","signature","symbol_hash","line_start","line_end","provenance","source_map_json","metadata_json","active","created_at"), "unknown_ledger": ("ledger_id","source_ref","code","detail","status","source_hash","created_at"),
    },
    "assets": {
        "asset_audit": ("audit_id","operation","aggregate_type","aggregate_id","idempotency_key","actor","authority","payload_hash","context_json","metadata_json","created_at"), "asset_holds": ("hold_id","asset_id","version_id","reason","source_ref","active","created_at","released_at"), "asset_locations": ("location_id","asset_id","version_id","root_ref","relative_path","content_hash","size_bytes","metadata_json","created_at"), "asset_migration_map": ("map_id","source_domain","source_ref","source_id","target_type","target_id","target_hash","status","metadata_json","created_at","updated_at"), "asset_outbox": ("event_id","aggregate_type","aggregate_id","event_type","payload_hash","payload_json","status","attempts","created_at","updated_at"), "asset_references": ("reference_id","asset_id","version_id","reference_kind","target_id","target_hash","metadata_json","created_at"), "asset_registry": ("asset_id","asset_kind","path","digest","media_type","size_bytes","state","metadata_json","created_at","updated_at"), "asset_schema_meta": ("key","value"), "asset_tombstones": ("tombstone_id","asset_id","version_id","reason","metadata_json","active","created_at","restored_at"), "asset_unknown_ledger": ("unknown_id","source_domain","source_ref","field","value","reason","status","metadata_json","created_at"), "asset_versions": ("version_id","asset_id","version_key","version","content_hash","size_bytes","metadata_json","created_at"), "assets": ("asset_id","asset_key","asset_kind","namespace_id","workspace_id","agent_instance_id","project_ref","provider","share_group_id","runtime_role","acl_digest","state","metadata_json","created_at","updated_at"), "schema_meta": ("domain","version","marker","updated_at"),
    },
    "scenario": {
        "projection_acl": ("acl_id","projection_id","workspace_id","agent_instance_id","project_ref","provider","share_group_id","sensitivity","policy_class"), "projection_evidence_links": ("link_id","projection_id","evidence_id","evidence_hash","relation","created_at"), "projection_head_events": ("event_id","projection_kind","projection_key","event_type","projection_id","generation","reason","created_at"), "projection_heads": ("head_id","projection_kind","projection_key","current_projection_id","generation","updated_at"), "projection_items": ("item_id","projection_id","atom_id","atom_hash","evidence_id","evidence_hash","metadata_json"), "projection_ledger": ("ledger_id","source_ref","code","detail","created_at"), "projection_schema_meta": ("key","value"), "projection_tombstones": ("tombstone_id","projection_kind","projection_key","projection_id","reason","created_at"), "scenario_projections": ("projection_id","scenario_key","generation","source_digest","projection_digest","status","payload_json","error_json","created_at","updated_at"), "schema_meta": ("domain","version","marker","updated_at"),
    },
    "profile": {},
}
_FORMAL_STRICT["profile"] = dict(_FORMAL_STRICT["scenario"])
_FORMAL_STRICT["profile"].pop("scenario_projections", None)
_FORMAL_STRICT["profile"]["profile_projections"] = ("projection_id","profile_key","generation","source_digest","projection_digest","status","payload_json","error_json","created_at","updated_at")
for _domain_name, _tables in _FORMAL_STRICT.items():
    for _table_name, _columns in _tables.items():
        _TABLES[_domain_name][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

# Rules is intentionally verbose; these are the durable Phase-2 table
# contracts (all columns are frozen so an ALTER-added field blocks audit).
_RULE_TABLE_COLUMNS = {
"schema_meta":("domain","version","marker","updated_at"),"rules_schema_meta":("schema_id","version","marker","updated_at"),
"rule_definitions":("definition_id","rule_key","text","state","revision","metadata_json","created_at","updated_at","canonical_text","normalized_intent","rule_kind","polarity","semantic_hash","parameter_schema","status","confidence","rule_strength","maturity_state","superseded_by"),
"rule_definition_versions":("version_id","definition_id","revision","snapshot_json","reason","actor","source_ref","created_at"),"rule_bindings":("binding_id","definition_id","share_group_id","target_type","target_id","project_ref","provider","runtime_role","effect","priority","owner_agent_id","created_by","authorization","status","revision","created_at","updated_at"),
"rule_binding_contributions":("contribution_id","binding_id","definition_id","share_group_id","source_memory_id","source_revision","legacy_assignment_hash","target_type","target_id","project_ref","provider","runtime_role","effect","priority","owner_agent_id","audience_json","active","status","revision","created_at","updated_at"),"rule_source_links":("source_link_id","source_kind","share_group_id","memory_id","source_ref","source_revision","original_definition_id","canonical_definition_id","status","metadata_json","created_at","updated_at"),
"rule_exceptions":("exception_id","parent_rule_id","child_exception_id","parent_rule","child_exception","priority","reason","rollback_json","active","source_ref","created_at","updated_at"),"rule_decisions":("decision_id","actor","owner_agent_id","rule_id","action","before_hash","after_hash","before_json","after_json","reason","confidence","undo_id","target_ids_json","metadata_json","source_ref","created_at"),
"rule_receipt_refs":("receipt_id","definition_id","source_rule_id","share_group_id","agent_instance_id","project_ref","session_id","task_hash","selection_digest","metadata_json","created_at"),"rule_feedback_refs":("feedback_id","receipt_id","definition_id","outcome","authority","evidence_digest","metadata_json","created_at"),"rule_runtime_feedback_refs":("feedback_id","definition_id","receipt_id","outcome","source","metadata_json","created_at"),
"rule_effective_feedback_projection":("receipt_id","effective_feedback_id","definition_id","outcome","positive_evidence_ref","negative_evidence_ref","projection_digest","updated_at"),"rule_agent_reputation":("agent_id","success_rate","rule_accuracy","violation_rate","sample_count","feedback_quality","metadata_json","created_at","updated_at"),"rule_project_profile":("project_ref","production_level","criticality","owner_verified","metadata_json","created_at","updated_at"),"rule_runtime_stats":("stats_id","definition_id","followed","violated","not_applicable","exception_count","distinct_sessions","distinct_projects","last_observed_at","metadata_json"),
"rule_evidence_contributions":("contribution_id","definition_id","independence_key","kind","polarity","authority","confidence","observed_at","active","receipt_id","feedback_id","source_evidence_id","source_memory_id","source_ids_json","metadata_json","created_at","updated_at"),"rule_evidence_effective":("effective_id","definition_id","independence_key","kind","winner_contribution_id","polarity","authority","confidence","observed_at","updated_at"),"rule_governance_capabilities":("capability_id","proposal_id","principal","scope_json","issued_at","expires_at","consumed_at","token_digest","metadata_json"),"rule_governance_capability_consumptions":("consumption_id","capability_id","proposal_id","consumed_by","consumed_at","metadata_json"),
"rule_evidence_refs":("evidence_id","definition_id","source_rule_id","share_group_id","agent_instance_id","project_ref","session_id","receipt_id","content_digest","evidence_ref","confidence","observed_at","metadata_json"),
"rule_merge_proposals":("proposal_id","definition_ids_json","status","evidence_digest","negative_digest","binding_digest","runtime_digest","assessment_digest","policy_version","metadata_json","source_ref","created_at","updated_at"),"rule_merge_decisions":("decision_id","proposal_id","canonical_definition_id","merged_definition_ids_json","before_bindings_json","after_bindings_json","source_digest","actor","status","undo_state_digest","metadata_json","created_at","undone_at"),"rule_merge_approvals":("approval_id","proposal_id","approved_by","capability_id","expected_revisions_json","approval_scope","created_at","expires_at"),"rule_merge_native_requests":("request_key","request_fingerprint","operation","schema_version","status","result_json","created_at","updated_at"),
"rule_negative_evidence_refs":("evidence_id","definition_id","source_rule_id","share_group_id","agent_instance_id","project_ref","session_id","receipt_id","content_digest","evidence_ref","confidence","observed_at","metadata_json"),"rule_definition_aliases":("old_definition_id","new_definition_id","migration_decision_id","source_ref","created_at"),"rule_canonical_state":("scope_id","share_group_id","activation_status","canonical_digest","read_path","source_digest","effective_digest","runtime_digest","assessment_digest","policy_version","updated_at"),"rule_idempotency_fence_anomalies":("anomaly_id","migration_id","source_kind","source_path","source_group_id","source_key","original_fence_id","conflict_fence_id","payload_digest","details_json","status","created_at"),"rule_decision_anomalies":("anomaly_id","migration_id","source_kind","source_path","source_group_id","source_table","original_decision_id","sibling_decision_id","payload_digest","details_json","status","created_at"),"rule_unknown_column_anomalies":("anomaly_id","migration_id","source_path","source_table","column_name","legacy_ledger_id","status","details_json","created_at"),
"rule_reconciliation_jobs":("job_id","share_group_id","migration_id","phase","status","source_digest","canonical_digest_before","canonical_digest_after","result_json","last_error","created_at","updated_at"),"rule_projection_checkpoints":("checkpoint_id","scope_id","last_event_id","projection_digest","status","error","updated_at"),"rule_idempotency_fences":("fence_id","key","request_fingerprint","memory_id","event_id","decision_id","created_at","share_group_id","source_ref"),"rule_migration_map":("map_id","migration_id","source_kind","source_path","source_group_id","source_table","source_id","target_table","target_id","source_digest","status","metadata_json","created_at"),"rule_domain_outbox":("event_id","migration_id","event_type","source_kind","source_group_id","source_ref","payload_digest","payload_json","created_at","consumed_at"),"rule_evidence_outbox":("event_id","migration_id","evidence_id","definition_id","evidence_ref","content_digest","polarity","source_kind","source_group_id","payload_json","created_at","consumed_at"),"rule_unknown_columns_ledger":("ledger_id","migration_id","source_kind","source_path","source_group_id","source_table","source_row_id","column_name","value_digest","status","created_at"),
}
for _table_name, _columns in _RULE_TABLE_COLUMNS.items():
    _TABLES["rules"][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

_SKILL_TABLE_COLUMNS = {
 "schema_meta": ("domain","version","marker","updated_at"),
 "skill_definitions": ("skill_id","stable_key","name","namespace","current_version","current_version_id","state","stable_hash","created_at","updated_at"),
 "skill_versions": ("version_id","skill_id","version","description","declaration_json","entrypoint_ref","entrypoint_hash","content_hash","capabilities_json","execution_policy_json","created_at"),
 "skill_bindings": ("binding_id","skill_id","version_id","target_type","target_id","project_ref","share_group_id","provider","runtime_role","effect","binding_hash"),
 "skill_capabilities": ("capability_id","version_id","capability","authority","constraints_json"),
 "skill_evidence_refs": ("ref_id","version_id","evidence_id","source_ref","digest","revision","authority"),
 "skill_asset_refs": ("ref_id","version_id","asset_id","path","digest","asset_kind"),
 "execution_policies": ("policy_id","version_id","policy_json","policy_hash"),
 "receipts": ("receipt_id","operation","skill_id","version_id","idempotency_key","request_hash","status","result_json","created_at"),
 "decisions": ("decision_id","operation","skill_id","before_hash","after_hash","expected_hash","reason","status","context_json","created_at"),
 "domain_outbox": ("event_id","sequence","event_type","aggregate_id","payload_json","status","attempts","created_at","projected_at","error_json"),
 "outbox_checkpoints": ("domain","last_sequence","updated_at"),
 "migration_map": ("map_id","source_path","source_hash","source_kind","skill_id","version_id","metadata_json","created_at"),
 "unknown_ledger": ("unknown_id","source_path","field_name","value_hash","details_json","created_at"),
}
for _table_name, _columns in _SKILL_TABLE_COLUMNS.items():
    _TABLES["skills"][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

_EVIDENCE_STRICT = {
 "schema_meta": ("domain","version","marker","updated_at"), "evidence_schema_meta": ("domain","version","marker","updated_at"),
 "evidence": ("evidence_id","evidence_type","source_ref","source_revision","digest","authority","status","observed_at","invalidated_at","metadata_json","created_at","revision"),
 "evidence_links": ("link_id","evidence_id","subject_type","subject_id","relation","created_at","metadata_json"),
 "migration_map": ("map_id","source_domain","source_ref","source_id","target_type","target_id","metadata_json","created_at"), "domain_outbox": ("event_id","sequence","event_type","aggregate_id","payload_json","status","attempts","created_at","projected_at","error_json"), "outbox_checkpoints": ("domain","last_sequence","updated_at"), "audit_refs": ("audit_id","source_ref","digest","metadata_json","created_at"),
}
for _table_name, _columns in _EVIDENCE_STRICT.items():
    _TABLES["evidence"][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])

_KNOWLEDGE_STRICT = {
 "schema_meta": ("domain","version","marker","updated_at"),
 "knowledge_assets": ("asset_id","asset_type","title","source_ref","status","policy_class","metadata_json","created_at","updated_at"),
 "knowledge_documents": ("document_id","asset_id","path","title","status","metadata_json","created_at","updated_at"),
}
for _table_name, _columns in _KNOWLEDGE_STRICT.items():
    _TABLES["knowledge"][_table_name] = TableSpec(_table_name, frozenset(_columns), frozenset(_columns), primary_key=_columns[0])


_MARKERS: dict[str, tuple[tuple[str, ...], int]] = {
    "runtime": (("1", "memoryguard-v2-phase4-runtime", "memoryguard-v2-phase1"), 1),
    "memory": (("memoryguard-v2-phase2-memory",), 1),
    "rules": (("memoryguard-v2-phase2-rules",), 2),
    "evidence": (("memoryguard-v2-phase2-evidence",), 1),
    "content": (("4", "memoryguard-v2-phase2-content"), 4),
    "knowledge": (("memoryguard-v2-phase5-knowledge", "memoryguard-v2-phase1"), 1),
    # CodeGraph keeps the phase-1 SQLite user_version at 1 while its store-owned
    # auxiliary schema is now v2. SQLiteReadOnlyAdapter reports the aux marker,
    # so the registry version tracks that marker and pins user_version separately.
    "codegraph": (("2", "memoryguard-v2-phase10-codegraph"), 2),
    "assets": (("1", "memoryguard-v2-phase5-assets"), 1),
    "scenario": (("1", "memoryguard-v2-phase3-projection"), 1),
    "profile": (("1", "memoryguard-v2-phase3-projection"), 1),
    "system": (("memoryguard-v2-phase1",), 1),
    "skills": (("memoryguard-v2-phase5-skills",), 1),
}


_REFERENCE_RULES: dict[str, tuple[ReferenceRule, ...]] = {
    "runtime": (ReferenceRule("task_refs", "ref_value", "evidence", "evidence", "evidence_id", discriminator_column="ref_kind", discriminator_values=("evidence",)),),
    "memory": (ReferenceRule("atom_revisions", "atom_id", "memory", "atoms", "atom_id"), ReferenceRule("source_mappings", "atom_id", "memory", "atoms", "atom_id"), ReferenceRule("domain_outbox", "aggregate_id", "memory", "atoms", "atom_id")),
    "rules": (ReferenceRule("rule_bindings", "definition_id", "rules", "rule_definitions", "definition_id"), ReferenceRule("rule_definition_versions", "definition_id", "rules", "rule_definitions", "definition_id"), ReferenceRule("rule_source_links", "canonical_definition_id", "rules", "rule_definitions", "definition_id")),
    "evidence": (ReferenceRule("evidence_links", "evidence_id", "evidence", "evidence", "evidence_id"),),
    "content": (ReferenceRule("content_occurrences", "blob_id", "content", "content_blobs", "blob_id", discriminator_column="active", discriminator_values=("1", "true")), ReferenceRule("content_evidence_links", "blob_id", "content", "content_blobs", "blob_id", kind="evidence", discriminator_column="status", discriminator_values=("valid",)), ReferenceRule("content_holds", "blob_id", "content", "content_blobs", "blob_id", kind="hold", discriminator_column="active", discriminator_values=("1", "true")), ReferenceRule("raw_content", "blob_id", "content", "content_blobs", "blob_id"), ReferenceRule("content_tombstones", "blob_id", "content", "content_blobs", "blob_id", kind="tombstone", discriminator_column="active", discriminator_values=("1", "true")), ReferenceRule("knowledge_records", "content_blob_id", "content", "content_blobs", "blob_id")),
    "knowledge": (ReferenceRule("knowledge_documents", "asset_id", "knowledge", "knowledge_assets", "asset_id"),),
    "codegraph": (ReferenceRule("edges", "from_id", "codegraph", "symbols", "symbol_id"), ReferenceRule("edges", "to_id", "codegraph", "symbols", "symbol_id"), ReferenceRule("codegraph_edges", "from_node_id", "codegraph", "codegraph_nodes", "node_id"), ReferenceRule("codegraph_edges", "to_node_id", "codegraph", "codegraph_nodes", "node_id")),
    "assets": (ReferenceRule("asset_versions", "asset_id", "assets", "assets", "asset_id"), ReferenceRule("asset_locations", "asset_id", "assets", "assets", "asset_id"), ReferenceRule("asset_references", "asset_id", "assets", "assets", "asset_id"), ReferenceRule("asset_holds", "asset_id", "assets", "assets", "asset_id"), ReferenceRule("asset_references", "target_id", "assets", "assets", "asset_id", discriminator_column="reference_kind", discriminator_values=("asset",)), ReferenceRule("asset_references", "target_id", "assets", "asset_versions", "version_id", discriminator_column="reference_kind", discriminator_values=("asset_version",))),
    "scenario": (ReferenceRule("projection_items", "atom_id", "memory", "atoms", "atom_id"), ReferenceRule("projection_items", "evidence_id", "evidence", "evidence", "evidence_id"), ReferenceRule("projection_evidence_links", "evidence_id", "evidence", "evidence", "evidence_id")),
    "profile": (ReferenceRule("projection_items", "atom_id", "memory", "atoms", "atom_id"), ReferenceRule("projection_items", "evidence_id", "evidence", "evidence", "evidence_id"), ReferenceRule("projection_evidence_links", "evidence_id", "evidence", "evidence", "evidence_id")),
    "skills": (ReferenceRule("skill_versions", "skill_id", "skills", "skill_definitions", "skill_id"), ReferenceRule("skill_bindings", "skill_id", "skills", "skill_definitions", "skill_id"), ReferenceRule("skill_evidence_refs", "evidence_id", "evidence", "evidence", "evidence_id"), ReferenceRule("skill_asset_refs", "asset_id", "assets", "assets", "asset_id")),
}


def _domain(name: str, relative_path: str, db_name: str) -> DomainSpec:
    markers, version = _MARKERS[name]
    tables = _TABLES[name]
    json_columns = tuple(sorted({column for spec in tables.values() for column in spec.required_columns if column.endswith("_json") or column in {"metadata_json", "payload_json", "declaration_json", "capabilities_json", "execution_policy_json", "policy_json"}}))
    outbox = tuple(table for table in tables if "outbox" in table)
    unknown = tuple(table for table in tables if "unknown" in table)
    migration = tuple(table for table in tables if "migration" in table)
    supported_user_versions = (1, 2) if name in {"rules", "content"} else (1,)
    return DomainSpec(name, relative_path, db_name, markers, version, tables, json_columns, _COMMON_REF_KEYS, outbox, unknown, migration, _REFERENCE_RULES.get(name, ()), frozenset(), supported_user_versions)


DOMAIN_SPECS: tuple[DomainSpec, ...] = (
    _domain("runtime", ".memoryguard/runtime", "runtime.db"),
    _domain("memory", ".memoryguard/memory", "memory.db"),
    _domain("rules", ".memoryguard/rules", "rules.db"),
    _domain("evidence", ".memoryguard/evidence", "evidence.db"),
    _domain("content", ".memoryguard/content", "content.db"),
    _domain("knowledge", ".memoryguard/knowledge", "knowledge.db"),
    _domain("codegraph", ".memoryguard/codegraph", "codegraph.db"),
    _domain("assets", ".memoryguard/assets", "assets.db"),
    _domain("scenario", ".memoryguard/projection", "scenario.db"),
    _domain("profile", ".memoryguard/projection", "profile.db"),
    _domain("system", ".memoryguard/system", "manifest.db"),
    _domain("skills", ".memoryguard/skills", "skills.db"),
)

AUTHORITATIVE_DOMAINS = tuple(spec.name for spec in DOMAIN_SPECS)


class DomainRegistry:
    """Immutable registry with a stable digest used in every audit result."""

    def __init__(self, specs: Iterable[DomainSpec] = DOMAIN_SPECS) -> None:
        values = tuple(specs)
        names = tuple(spec.name for spec in values)
        if names != AUTHORITATIVE_DOMAINS:
            raise ValueError("registry must contain the explicit 12-domain authoritative order")
        self._specs = MappingProxyType({spec.name: spec for spec in values})

    def __iter__(self):
        return iter(self._specs.values())

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __getitem__(self, name: str) -> DomainSpec:
        return self._specs[name]

    @property
    def names(self) -> tuple[str, ...]:
        return AUTHORITATIVE_DOMAINS

    @property
    def digest(self) -> str:
        payload = [self._specs[name].to_dict() for name in AUTHORITATIVE_DOMAINS]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def path_for(self, workspace: str | Path, domain: str) -> Path:
        if domain not in self._specs:
            raise KeyError(domain)
        root = Path(workspace).expanduser()
        return root / self._specs[domain].relative_path / self._specs[domain].db_name


DEFAULT_REGISTRY = DomainRegistry()
AUTHORITATIVE_REGISTRY = DEFAULT_REGISTRY
DOMAIN_REGISTRY = DEFAULT_REGISTRY
AUTHORITATIVE_DOMAIN_REGISTRY = DEFAULT_REGISTRY
REGISTRY = DEFAULT_REGISTRY

__all__ = ["TableSpec", "ReferenceRule", "DomainSpec", "DomainRegistry", "DEFAULT_REGISTRY", "AUTHORITATIVE_REGISTRY", "DOMAIN_REGISTRY", "AUTHORITATIVE_DOMAIN_REGISTRY", "REGISTRY", "DOMAIN_SPECS", "AUTHORITATIVE_DOMAINS"]
