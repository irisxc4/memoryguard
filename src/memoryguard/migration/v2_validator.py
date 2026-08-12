"""Read-only validation for the V2 Phase 2 shadow build.

The validator is deliberately stricter than a database health check.  It
checks source bytes, source/target mappings, authorization identity and
outbox state, while treating absent optional sources as explicit
``NO_SOURCE``/``NOT_CONFIGURED`` states.  A successful result is evidence for
the current build only; ``can_promote`` is always false because Phase 2 has no
runtime cut-over.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from ..storage.database import open_database
from ..storage.layout import WorkspaceV2Layout
from ..system.manifest import ManifestManager
from ..rules.v2_store import canonical_migration_source_id


# FTS virtual tables and all of their SQLite-managed shadow tables are
# rebuildable projections, not authoritative source rows.  Keep the explicit
# names for diagnostics while accepting future ``*_fts_*`` shadow variants.
DERIVED_NAMES = {
    "chunks_fts", "chunks_fts_data", "chunks_fts_idx", "chunks_fts_content",
    "chunks_fts_docsize", "chunks_fts_config",
    "history_fts", "history_fts_data", "history_fts_idx", "history_fts_content",
    "history_fts_docsize", "history_fts_config", "embeddings",
}

_HISTORY_RECEIPT_COLUMNS: tuple[str, ...] = (
    "idempotency_key",
    "operation",
    "payload_digest",
    "result_json",
    "created_at",
)


def _is_derived_table(name: str) -> bool:
    value = str(name or "")
    return value in DERIVED_NAMES or value.startswith("history_fts_") or value.startswith("chunks_fts_")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _nonempty_wal(path: Path) -> bool:
    """Immutable reads must not silently ignore frames in a WAL sidecar."""

    wal = path.with_name(path.name + "-wal")
    try:
        return wal.is_file() and wal.stat().st_size > 0
    except OSError:
        return True


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@dataclass
class DomainValidation:
    name: str
    status: str = "PASS"
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "PASS" and not self.errors

    def fail(self, message: str) -> None:
        self.status = "BLOCKED"
        self.errors.append(str(message))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "ok": self.ok, "metrics": dict(self.metrics), "errors": list(self.errors)}


@dataclass
class V2ValidationResult(Mapping[str, Any]):
    status: str
    domains: dict[str, DomainValidation] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    source_hashes: dict[str, str] = field(default_factory=dict)
    expected_source_hashes: dict[str, str] = field(default_factory=dict)
    source_status: dict[str, str] = field(default_factory=dict)
    migration_id: str = ""
    can_promote: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "PASS" and not self.errors

    @property
    def ready(self) -> bool:
        return False

    @property
    def blocked(self) -> bool:
        return self.status == "BLOCKED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "ready": False,
            "can_promote": False,
            "migration_id": self.migration_id,
            "domains": {key: value.to_dict() for key, value in self.domains.items()},
            "metrics": dict(self.metrics),
            "errors": list(self.errors),
            "source_hashes": dict(self.source_hashes),
            "expected_source_hashes": dict(self.expected_source_hashes),
            "source_status": dict(self.source_status),
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


class V2MigrationValidator:
    """Validate one workspace's V2 shadow databases without creating files."""

    def __init__(
        self,
        workspace: str | Path | WorkspaceV2Layout,
        *,
        data_home: str | Path | None = None,
        migration_id: str | None = None,
        expected_source_hashes: Mapping[str, str] | None = None,
        source_workspace: str | Path | None = None,
        source_data_home: str | Path | None = None,
    ) -> None:
        self.layout = workspace if isinstance(workspace, WorkspaceV2Layout) else WorkspaceV2Layout(Path(workspace))
        self.workspace = self.layout.workspace
        self.data_home = Path(data_home).expanduser().resolve() if data_home is not None else None
        self.source_workspace = Path(source_workspace).expanduser().resolve() if source_workspace is not None else self.workspace
        self.source_data_home = Path(source_data_home).expanduser().resolve() if source_data_home is not None else self.data_home
        self._explicit_source_workspace = source_workspace is not None
        self._explicit_source_data_home = source_data_home is not None
        self._validation_source_workspace: Path | None = None
        self._validation_source_data_home: Path | None = None
        self.migration_id = str(migration_id or "")
        self.expected_source_hashes = {str(key): str(value) for key, value in (expected_source_hashes or {}).items()}
        self.last_result: V2ValidationResult | None = None

    # ---- source inventory -----------------------------------------
    def _source_paths(self) -> dict[str, tuple[Path | None, str]]:
        source_workspace = self._validation_source_workspace or self.source_workspace
        source_data_home = (
            self._validation_source_data_home
            if self._validation_source_data_home is not None
            else self.source_data_home
        )
        result: dict[str, tuple[Path | None, str]] = {
            "history": (source_workspace / ".memoryguard" / "history" / "history.sqlite", "configured"),
            "knowledge": (None, "NOT_CONFIGURED"),
        }
        if source_data_home is not None:
            result["knowledge"] = (source_data_home / "knowledge" / "knowledge.db", "configured")
        group_roots = [source_workspace / ".memoryguard" / "shared-memory", source_workspace / ".memoryguard" / "shared_memory"]
        for groups in group_roots:
            if groups.is_dir():
                for child in sorted(groups.iterdir(), key=lambda path: path.name):
                    path = child / "memory.db"
                    if child.is_dir():
                        result.setdefault(f"memory:{child.name}", (path, "configured"))
        ri = source_workspace / ".memoryguard" / "rule-intelligence" / "memory.db"
        result["rule_intelligence"] = (ri, "configured")
        return result

    def _frozen_source_snapshot(self) -> tuple[Path, Path | None] | None:
        """Return the manifest-owned source snapshot for target validation.

        Workspace preparation intentionally inventories live V1 before taking
        a backup.  Once a build is recorded, target mappings point at the
        frozen snapshot instead.  Validation must use that same immutable
        identity unless the caller explicitly selected another source root.
        """

        if self._explicit_source_workspace or self._explicit_source_data_home:
            return None
        try:
            manifest = ManifestManager(self.layout).current(immutable=True)
            checkpoints = manifest.checkpoints if isinstance(manifest.checkpoints, Mapping) else {}
            phase2 = checkpoints.get("phase2_sources") if isinstance(checkpoints, Mapping) else None
            snapshot = phase2.get("snapshot") if isinstance(phase2, Mapping) else None
            if not isinstance(snapshot, Mapping) or str(snapshot.get("mode") or "") != "frozen":
                return None
            raw_workspace = str(snapshot.get("workspace") or "")
            if not raw_workspace:
                return None
            snapshot_workspace = Path(raw_workspace).expanduser().resolve()
            backup_root = (self.workspace / ".memoryguard" / "migration-backups").resolve()
            try:
                snapshot_workspace.relative_to(backup_root)
            except ValueError:
                return None
            if not snapshot_workspace.is_dir():
                return None
            raw_data_home = str(snapshot.get("data_home") or "")
            snapshot_data_home = None
            if raw_data_home and raw_data_home != "NOT_CONFIGURED":
                candidate = Path(raw_data_home).expanduser().resolve()
                try:
                    candidate.relative_to(backup_root)
                except ValueError:
                    return None
                snapshot_data_home = candidate if candidate.is_dir() else None
            return snapshot_workspace, snapshot_data_home
        except (OSError, sqlite3.Error, ValueError):
            return None

    def source_inventory(self) -> dict[str, dict[str, Any]]:
        inventory: dict[str, dict[str, Any]] = {}
        for key, (path, configured) in self._source_paths().items():
            item: dict[str, Any] = {"status": configured, "path": str(path) if path else ""}
            if path is None:
                inventory[key] = item
                continue
            if not path.is_file():
                item["status"] = "NO_SOURCE" if configured == "configured" else configured
                inventory[key] = item
                continue
            try:
                if _nonempty_wal(path):
                    raise sqlite3.OperationalError("immutable read blocked by non-empty WAL")
                item["sha256"] = _file_hash(path)
                with open_database(path, readonly=True, immutable=True) as conn:
                    item["integrity"] = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
                    item["foreign_keys"] = len(conn.execute("PRAGMA foreign_key_check").fetchall())
                    item["tables"] = [str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                item["status"] = "READY"
            except Exception as exc:
                item["status"] = "BLOCKED"
                item["error"] = f"{type(exc).__name__}: {exc}"
            inventory[key] = item
        return inventory

    # ---- database checks ------------------------------------------
    def _check_db(self, domain: DomainValidation, path: Path, *, marker: str | None = None, schema_domain: str | None = None, aux_marker: str | None = None) -> None:
        domain.metrics.setdefault("db_path", str(path))
        if not path.is_file():
            domain.fail(f"missing V2 database: {path}")
            return
        try:
            if _nonempty_wal(path):
                domain.fail(f"immutable read blocked by non-empty WAL: {path}")
                return
            with open_database(path, readonly=True, immutable=True) as conn:
                integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check").fetchall()]
                fk = conn.execute("PRAGMA foreign_key_check").fetchall()
                domain.metrics["integrity"] = integrity
                domain.metrics["foreign_key_errors"] = len(fk)
                if integrity != ["ok"]:
                    domain.fail(f"integrity_check failed for {path}: {integrity}")
                if fk:
                    domain.fail(f"foreign_key_check failed for {path}: {len(fk)}")
                if marker:
                    table = {
                        "memory": "memory_schema_meta",
                        "evidence": "evidence_schema_meta",
                    }.get(schema_domain or "", "schema_meta" if schema_domain != "rules" else "rules_schema_meta")
                    if table not in {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                        domain.fail(f"schema marker table missing: {table}")
                    else:
                        if schema_domain == "rules":
                            row = conn.execute("SELECT version,marker FROM rules_schema_meta WHERE schema_id='rules'").fetchone()
                        elif schema_domain in {"memory", "evidence"}:
                            row = conn.execute(f"SELECT version,marker FROM {table} WHERE domain=?", (schema_domain,)).fetchone()
                        else:
                            row = conn.execute("SELECT version,marker FROM schema_meta WHERE domain=?", (schema_domain or "",)).fetchone()
                        if row is None or str(row[1]) != marker:
                            domain.fail(f"schema marker mismatch for {path}: {tuple(row) if row else None}")
                        if schema_domain in {"memory", "evidence"}:
                            base = conn.execute("SELECT version,marker FROM schema_meta WHERE domain=?", (schema_domain,)).fetchone()
                            if base is None or int(base[0]) != 1 or str(base[1]) != "memoryguard-v2-phase1":
                                domain.fail(f"base schema marker mismatch for {path}: {tuple(base) if base else None}")
                if aux_marker:
                    row = conn.execute("SELECT value FROM content_schema_meta WHERE key='version'").fetchone()
                    if row is None or str(row[0]) != str(aux_marker):
                        domain.fail(f"content auxiliary schema marker missing: {path}")
        except Exception as exc:
            domain.fail(f"cannot inspect {path}: {type(exc).__name__}: {exc}")

    def _target_checks(self, result: V2ValidationResult) -> None:
        layout = self.layout
        phase1 = DomainValidation("storage")
        for domain, path in layout.iter_db_paths():
            # Phase-2 domain services widen three Phase-1 databases with
            # their own markers.  The remaining databases retain the Phase-1
            # structural marker until their later migration owns them.
            marker = {
                "memory": "memoryguard-v2-phase2-memory",
                "evidence": "memoryguard-v2-phase2-evidence",
                "rules": "memoryguard-v2-phase2-rules",
            }.get(domain, "memoryguard-v2-phase1")
            schema_domain = "projection.profile" if domain == "projection" and path.name == "profile.db" else ("projection.scenario" if domain == "projection" else domain)
            self._check_db(phase1, path, marker=marker, schema_domain=schema_domain)
        result.domains["storage"] = phase1
        self._check_db(result.domains.setdefault("content", DomainValidation("content")), layout.content_db, marker="memoryguard-v2-phase1", schema_domain="content", aux_marker="3")
        self._check_db(result.domains.setdefault("memory", DomainValidation("memory")), layout.memory_db, marker="memoryguard-v2-phase2-memory", schema_domain="memory")
        self._check_db(result.domains.setdefault("evidence", DomainValidation("evidence")), layout.evidence_db, marker="memoryguard-v2-phase2-evidence", schema_domain="evidence")
        self._check_db(result.domains.setdefault("rules", DomainValidation("rules")), layout.rules_db, marker="memoryguard-v2-phase2-rules", schema_domain="rules")

    # ---- source and target semantics -------------------------------
    def _compare_source_hashes(self, result: V2ValidationResult, inventory: Mapping[str, Mapping[str, Any]]) -> None:
        expected = dict(self.expected_source_hashes)
        if not expected:
            try:
                manager = ManifestManager(self.layout)
                if _nonempty_wal(manager.db_path):
                    raise sqlite3.OperationalError("immutable manifest read blocked by non-empty WAL")
                manifest = manager.current(immutable=True)
                expected = {str(key): str(value) for key, value in (manifest.checkpoints.get("phase2_sources", {}).get("hashes", {}) if isinstance(manifest.checkpoints, Mapping) else {}).items()}
            except Exception as exc:
                result.errors.append(f"manifest_read_blocked:{type(exc).__name__}")
                expected = {}
        result.expected_source_hashes = expected
        for key, item in inventory.items():
            status = str(item.get("status") or "")
            result.source_status[key] = status
            if status == "BLOCKED":
                result.errors.append(f"source_read_blocked:{key}")
            digest = str(item.get("sha256") or "")
            if digest:
                result.source_hashes[key] = digest
            prior = expected.get(key) or expected.get(str(item.get("path") or ""))
            if prior and digest and prior != digest:
                result.errors.append(f"source_hash_changed:{key}")
        result.metrics["source_hashes_unchanged"] = not any(error.startswith("source_hash_changed:") for error in result.errors)

    @staticmethod
    def _rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
        return conn.execute(f"SELECT * FROM {_quote(table)}").fetchall()

    @staticmethod
    def _receipt_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "history_mutation_receipts" not in tables:
            return []
        rows = conn.execute(
            "SELECT idempotency_key,operation,payload_digest,result_json,created_at "
            "FROM history_mutation_receipts ORDER BY idempotency_key"
        ).fetchall()
        return [
            {column: row[column] for column in _HISTORY_RECEIPT_COLUMNS}
            for row in rows
        ]

    def _history_receipt_metrics(
        self,
        domain: DomainValidation,
        history_item: Mapping[str, Any],
        target: sqlite3.Connection,
    ) -> dict[str, Any]:
        target_rows = self._receipt_rows(target)
        target_digest = _digest(target_rows)
        if history_item.get("status") != "READY":
            return {
                "status": "NO_SOURCE",
                "source_count": None,
                "target_count": len(target_rows),
                "source_digest": "",
                "target_digest": target_digest,
                "count_match": True,
                "content_digest_match": True,
                "loss": 0,
            }

        source_path = Path(str(history_item.get("path") or ""))
        with open_database(source_path, readonly=True, immutable=True) as source:
            source_tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            source_rows = self._receipt_rows(source)
        source_digest = _digest(source_rows)
        if "history_mutation_receipts" not in source_tables:
            status = "NO_SOURCE" if not target_rows else "BLOCKED"
            if target_rows:
                domain.fail("history_mutation_receipts_target_without_source")
            return {
                "status": status,
                "source_count": None,
                "target_count": len(target_rows),
                "source_digest": "",
                "target_digest": target_digest,
                "count_match": not target_rows,
                "content_digest_match": not target_rows,
                "loss": len(target_rows),
            }

        count_match = len(source_rows) == len(target_rows)
        digest_match = source_digest == target_digest
        loss = 0 if count_match and digest_match else max(abs(len(source_rows) - len(target_rows)), 1)
        if not count_match:
            domain.fail(
                f"history_mutation_receipts_count_mismatch:{len(source_rows)}/{len(target_rows)}"
            )
        if not digest_match:
            domain.fail("history_mutation_receipts_content_digest_mismatch")
        return {
            "status": "PASS" if loss == 0 else "BLOCKED",
            "source_count": len(source_rows),
            "target_count": len(target_rows),
            "source_digest": source_digest,
            "target_digest": target_digest,
            "count_match": count_match,
            "content_digest_match": digest_match,
            "loss": loss,
        }

    def _content_metrics(self, domain: DomainValidation, source_inventory: Mapping[str, Mapping[str, Any]]) -> None:
        path = self.layout.content_db
        if not path.is_file():
            return
        history_item = source_inventory.get("history", {})
        knowledge_item = source_inventory.get("knowledge", {})
        with open_database(path, readonly=True, immutable=True) as conn:
            maps = conn.execute("SELECT source_db,source_table,source_pk,target_type,target_id,source_hash,acl_digest FROM migration_map").fetchall()
            by_db = Counter(str(row[0]) for row in maps)
            domain.metrics.update({"migration_map": len(maps), "migration_map_source_dbs": dict(by_db), "blobs": int(conn.execute("SELECT COUNT(*) FROM content_blobs").fetchone()[0]), "occurrences": int(conn.execute("SELECT COUNT(*) FROM content_occurrences").fetchone()[0]), "active_occurrences": int(conn.execute("SELECT COUNT(*) FROM content_occurrences WHERE active=1").fetchone()[0])})
            receipt_metrics = self._history_receipt_metrics(domain, history_item, conn)
            domain.metrics["history_mutation_receipts"] = receipt_metrics
            domain.metrics.update({
                "history_mutation_receipts_source_count": receipt_metrics["source_count"],
                "history_mutation_receipts_target_count": receipt_metrics["target_count"],
                "history_mutation_receipts_source_digest": receipt_metrics["source_digest"],
                "history_mutation_receipts_target_digest": receipt_metrics["target_digest"],
                "history_mutation_receipts_loss": receipt_metrics["loss"],
            })
            for key, item in (("history", history_item), ("knowledge", knowledge_item)):
                if item.get("status") != "READY":
                    domain.metrics[f"{key}_loss"] = 0
                    continue
                source_db = str(item.get("path") or "")
                mapped = int(by_db.get(source_db, 0))
                source_rows = 0
                try:
                    with open_database(Path(source_db), readonly=True, immutable=True) as source_conn:
                        for table in source_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                            name = str(table[0])
                            if _is_derived_table(name):
                                continue
                            source_rows += int(source_conn.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0])
                except Exception as exc:
                    domain.fail(f"source_row_count_failed:{key}:{exc}")
                    continue
                domain.metrics[f"{key}_source_rows"] = source_rows
                domain.metrics[f"{key}_mapped_rows"] = mapped
                domain.metrics[f"{key}_loss"] = max(0, source_rows - mapped)
                if mapped < source_rows:
                    domain.fail(f"migration_map_incomplete:{key}:{mapped}/{source_rows}")
            # ACL digest is calculated from structured columns, not the JSON
            # compatibility field.
            acl_rows = [tuple(row) for row in conn.execute("SELECT workspace_id,agent_instance_id,project_ref,share_group_id,policy_class,provider,active FROM content_occurrences ORDER BY occurrence_id")]
            domain.metrics["acl_digest"] = _digest(acl_rows)
            domain.metrics["evidence_orphan"] = 0

    def _memory_metrics(self, domain: DomainValidation, source_inventory: Mapping[str, Mapping[str, Any]]) -> None:
        if not self.layout.memory_db.is_file():
            return
        with open_database(self.layout.memory_db, readonly=True, immutable=True) as conn:
            pending = int(conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='pending'").fetchone()[0])
            atoms = int(conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0])
            ready = int(conn.execute("SELECT COUNT(*) FROM atoms WHERE visibility IN ('ready','active')").fetchone()[0])
            # Every atom must have at least one valid Evidence reference before
            # this domain can validate.  Receipts prove only that the memory
            # outbox was projected; the evidence DB is authoritative for
            # status/validity and is checked below.
            evidence_orphan = 0
            if self.layout.evidence_db.is_file():
                with open_database(self.layout.evidence_db, readonly=True, immutable=True) as evidence_conn:
                    for atom_row in conn.execute("SELECT atom_id FROM atoms").fetchall():
                        atom_id = str(atom_row[0])
                        valid = int(evidence_conn.execute("SELECT COUNT(*) FROM evidence_links l JOIN evidence e ON e.evidence_id=l.evidence_id WHERE l.subject_type='atom' AND l.subject_id=? AND e.status='valid'", (atom_id,)).fetchone()[0])
                        if valid < 1:
                            evidence_orphan += 1
            scopes = [tuple(row) for row in conn.execute("SELECT workspace_id,agent_instance_id,share_group_id,project_ref,provider,runtime_role,effect FROM scope_acl ORDER BY acl_id")]
            domain.metrics.update({"atoms": atoms, "ready_atoms": ready, "outbox_pending": pending, "evidence_orphan": evidence_orphan, "scope_digest": _digest(scopes)})
            if pending:
                domain.fail(f"memory_outbox_pending:{pending}")
            if evidence_orphan:
                domain.fail(f"memory_evidence_orphan:{evidence_orphan}")
            # Authoritative legacy records are counted per configured group;
            # absent groups are explicitly excluded.
            expected = 0
            mapped = 0
            for key, item in source_inventory.items():
                if not key.startswith("memory:") or item.get("status") != "READY":
                    continue
                with open_database(Path(str(item["path"])), readonly=True, immutable=True) as source:
                    source_count = int(source.execute("SELECT COUNT(*) FROM records").fetchone()[0]) if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone() else 0
                expected += source_count
                with open_database(self.layout.memory_db, readonly=True, immutable=True) as target:
                    group = key.split(":", 1)[1]
                    mapped += int(target.execute("SELECT COUNT(*) FROM source_mappings WHERE source_domain='shared_memory' AND source_ref LIKE ?", (f"{group}/%",)).fetchone()[0])
            domain.metrics.update({"source_records": expected, "mapped_records": mapped, "loss": max(0, expected - mapped)})
            if mapped < expected:
                domain.fail(f"memory_loss:{expected - mapped}")

    def _rule_identity(self, source_path: Path, group: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (group, str(row.get("target_type", "") or ""), str(row.get("target_id", "") or ""), str(row.get("project_ref", "") or ""), str(row.get("provider", "") or ""), str(row.get("runtime_role", "") or ""), str(row.get("effect", "include") or "include"), int(row.get("priority_override", row.get("priority", 0)) or 0))

    @staticmethod
    def _source_fence_key(row: Mapping[str, Any]) -> str:
        """Legacy shared-memory snapshots used idempotency_key instead of key."""
        return str(row.get("key") or row.get("idempotency_key") or "")

    @staticmethod
    def _canonical_fence_key(key: Any) -> str:
        raw = str(key or "")
        marker = "#conflict-"
        return raw.split(marker, 1)[0] if marker in raw else raw

    @classmethod
    def _fence_identity(cls, group: str, source_ref: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(group or ""),
            cls._canonical_fence_key(cls._source_fence_key(row)),
            str(row.get("request_fingerprint", "") or ""),
            str(row.get("memory_id", "") or ""),
            str(row.get("event_id", "") or ""),
            str(row.get("decision_id", "") or ""),
            str(row.get("created_at", "") or ""),
            str(source_ref or ""),
        )

    def _rule_intelligence_coverage(
        self,
        domain: DomainValidation,
        source_inventory: Mapping[str, Mapping[str, Any]],
        target: sqlite3.Connection,
    ) -> None:
        """Report an explicit disposition for every P3 Rule Intelligence table.

        A missing source is represented as ``NO_SOURCE`` and never coerced to
        a numeric zero.  Present source rows must have an equal-or-larger
        target count; otherwise validation is blocked.  This keeps a source
        table from being silently treated as an already-lossless empty table.
        """

        mapping = {
            "agent_reputation": "rule_agent_reputation",
            "project_profile": "rule_project_profile",
            "rule_definition_versions": "rule_definition_versions",
            "rule_definition_runtime_stats": "rule_runtime_stats",
            "rule_evidence_contributions": "rule_evidence_contributions",
            "rule_evidence_effective": "rule_evidence_effective",
            "governance_capabilities": "rule_governance_capabilities",
            "rule_merge_native_requests": "rule_merge_native_requests",
        }
        item = source_inventory.get("rule_intelligence", {})
        source_status = str(item.get("status") or "NO_SOURCE")
        coverage: dict[str, dict[str, Any]] = {}
        if source_status != "READY":
            disposition = "NO_SOURCE" if source_status in {"NO_SOURCE", "configured", "NOT_CONFIGURED", ""} else "BLOCKED"
            for source_table, target_table in mapping.items():
                coverage[source_table] = {
                    "source_table": source_table,
                    "target_table": target_table,
                    "status": disposition,
                    "source_rows": None,
                    "target_rows": None,
                    "loss": {"status": disposition, "value": None},
                }
            if disposition == "BLOCKED":
                domain.fail(f"rule_intelligence_source:{source_status}")
            domain.metrics["rule_intelligence_coverage"] = coverage
            return

        source_path = Path(str(item.get("path") or ""))
        target_tables = {str(row[0]) for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        try:
            with open_database(source_path, readonly=True, immutable=True) as source:
                source_tables = {str(row[0]) for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                for source_table, target_table in mapping.items():
                    if source_table not in source_tables:
                        coverage[source_table] = {
                            "source_table": source_table,
                            "target_table": target_table,
                            "status": "NO_SOURCE",
                            "source_rows": None,
                            "target_rows": int(target.execute(f"SELECT COUNT(*) FROM {_quote(target_table)}").fetchone()[0]) if target_table in target_tables else None,
                            "loss": {"status": "NO_SOURCE", "value": None},
                        }
                        continue
                    if target_table not in target_tables:
                        coverage[source_table] = {
                            "source_table": source_table,
                            "target_table": target_table,
                            "status": "BLOCKED",
                            "source_rows": None,
                            "target_rows": None,
                            "loss": {"status": "BLOCKED", "value": None},
                        }
                        domain.fail(f"rule_intelligence_target_missing:{target_table}")
                        continue
                    source_rows = int(source.execute(f"SELECT COUNT(*) FROM {_quote(source_table)}").fetchone()[0])
                    target_rows = int(target.execute(f"SELECT COUNT(*) FROM {_quote(target_table)}").fetchone()[0])
                    if target_rows < source_rows:
                        status = "BLOCKED"
                        loss = {"status": "BLOCKED", "value": source_rows - target_rows}
                        domain.fail(f"rule_intelligence_loss:{source_table}:{source_rows - target_rows}")
                    else:
                        status = "MIGRATED"
                        loss = {"status": "PASS", "value": 0}
                    coverage[source_table] = {
                        "source_table": source_table,
                        "target_table": target_table,
                        "status": status,
                        "source_rows": source_rows,
                        "target_rows": target_rows,
                        "loss": loss,
                    }
        except Exception as exc:
            domain.fail(f"rule_intelligence_coverage_error:{type(exc).__name__}:{exc}")
            for source_table, target_table in mapping.items():
                coverage.setdefault(source_table, {
                    "source_table": source_table,
                    "target_table": target_table,
                    "status": "BLOCKED",
                    "source_rows": None,
                    "target_rows": None,
                    "loss": {"status": "BLOCKED", "value": None},
                })
        domain.metrics["rule_intelligence_coverage"] = coverage

    def _rules_metrics(self, domain: DomainValidation, source_inventory: Mapping[str, Mapping[str, Any]]) -> None:
        if not self.layout.rules_db.is_file():
            return
        expected: Counter[tuple[Any, ...]] = Counter()
        expected_fences: Counter[tuple[Any, ...]] = Counter()
        expected_decisions: dict[tuple[str, str, str], str] = {}
        expected_unknown_occurrences = 0
        expected_preserved_occurrences = 0
        known_rule_tables = {
            "rule_definitions", "rule_definition_versions", "rule_bindings", "rule_binding_contributions", "rule_evidence", "rule_negative_evidence", "rule_runtime_feedback", "rule_effective_feedback_projection", "rule_merge_proposals", "rule_merge_decisions", "rule_merge_approvals", "rule_merge_native_requests", "rule_definition_aliases", "rule_source_links", "rule_canonical_state", "rule_reconciliation_jobs", "rule_projection_state", "rule_projection_checkpoints", "agent_reputation", "project_profile", "rule_definition_runtime_stats", "rule_evidence_contributions", "rule_evidence_effective", "governance_capabilities", "governance_capability_consumptions",
        }

        # Source inventory order is not semantic.  The target phase is defined
        # below the source loop so it cannot observe a partial expected
        # multiset/provenance state; it is invoked exactly once, including for
        # an empty inventory.

        def decision_semantic(row: Mapping[str, Any]) -> str:
            def decoded(value: Any) -> Any:
                if isinstance(value, (Mapping, list, tuple)):
                    return value
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except (TypeError, ValueError):
                        return value
                return value

            def text_digest(value: Any) -> str:
                return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

            before = row.get("before", row.get("before_state", ""))
            after = row.get("after", row.get("after_state", ""))
            payload = {
                "actor": str(row.get("actor", "") or ""),
                "owner_agent_id": str(row.get("owner_agent_id", "") or ""),
                "action": str(row.get("action", "") or ""),
                "before_hash": text_digest(before),
                "after_hash": text_digest(after),
                "before_json": json.dumps(decoded(before), ensure_ascii=False),
                "after_json": json.dumps(decoded(after), ensure_ascii=False),
                "reason": str(row.get("reason", "") or ""),
                "confidence": float(row.get("confidence", 1.0) or 1.0),
                "undo_id": str(row.get("undo_id", "") or ""),
                "target_ids_json": json.dumps(decoded(row.get("target_ids", [])), ensure_ascii=False),
                "created_at": str(row.get("created_at", "") or ""),
            }
            return _digest(payload)

        for key, item in source_inventory.items():
            if key == "rule_intelligence" and item.get("status") == "READY":
                with open_database(Path(str(item["path"])), readonly=True, immutable=True) as source:
                    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_bindings'").fetchone():
                        # Rule Intelligence bindings are authoritative P3
                        # scope rows in their own right.  They do not have a
                        # shared-memory ``records``/``rule_assignments``
                        # parent, so omitting them makes a lossless migration
                        # report a false binding diff equal to their count.
                        for raw in source.execute("SELECT * FROM rule_bindings").fetchall():
                            row = {str(k): raw[k] for k in raw.keys()}
                            expected[self._rule_identity(Path(str(item["path"])), str(row.get("share_group_id", "") or ""), row)] += 1
                    for table_row in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                        table = str(table_row[0])
                        if table in known_rule_tables:
                            continue
                        count = int(source.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
                        columns = int(source.execute(f"SELECT COUNT(*) FROM pragma_table_info({_quote(table)})").fetchone()[0])
                        expected_unknown_occurrences += count * columns
                    # ``judge_rationale`` is the one deliberately preserved
                    # unknown column in otherwise known merge tables.  Its
                    # digest/reference is durable in metadata_json; the raw
                    # body remains occurrence-bound in the unknown ledger.
                    for table in ("rule_merge_proposals", "rule_merge_decisions"):
                        if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            continue
                        columns = {str(info[1]) for info in source.execute(f"PRAGMA table_info({_quote(table)})").fetchall()}
                        if "judge_rationale" in columns:
                            rationale_rows = int(source.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
                            expected_unknown_occurrences += rationale_rows
                            expected_preserved_occurrences += rationale_rows
            if not key.startswith("memory:") or item.get("status") != "READY":
                continue
            path = Path(str(item["path"])); group = key.split(":", 1)[1]
            with open_database(path, readonly=True, immutable=True) as source:
                if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_idempotency_fences'").fetchone():
                    for fence in source.execute("SELECT * FROM rule_idempotency_fences").fetchall():
                        expected_fences[self._fence_identity(group, str(path), {str(k): fence[k] for k in fence.keys()})] += 1
                for table in ("rule_decisions", "decisions"):
                    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    for raw in source.execute(f"SELECT * FROM {_quote(table)}").fetchall():
                        row = {str(k): raw[k] for k in raw.keys()}
                        source_id = str(row.get("decision_id", row.get("event_id", "")) or "") or _digest((str(path), table, row))
                        expected_decisions[(group, table, source_id)] = decision_semantic(row)
                if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='records'").fetchone():
                    continue
                record_columns = {str(info[1]) for info in source.execute("PRAGMA table_info(records)").fetchall()}
                policy_column = "injection_policy" if "injection_policy" in record_columns else ("policy" if "policy" in record_columns else "")
                if not policy_column:
                    rows = []
                else:
                    rows = source.execute(f"SELECT * FROM records WHERE lower(COALESCE({_quote(policy_column)},''))='always'").fetchall()
                for row in rows:
                    memory_id = str(row["memory_id"] if "memory_id" in row.keys() else row[0])
                    if source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_assignments'").fetchone():
                        assignments = source.execute("SELECT * FROM rule_assignments WHERE memory_id=?", (memory_id,)).fetchall()
                        for assignment in assignments:
                            expected[self._rule_identity(path, group, {str(k): assignment[k] for k in assignment.keys()})] += 1
        def evaluate_targets() -> None:
            # ``immutable=1`` deliberately ignores WAL frames.  Refuse the
            # target phase while a sidecar is non-empty rather than reporting
            # metrics from a stale database snapshot.
            if _nonempty_wal(self.layout.rules_db):
                domain.metrics.update({
                    "target_metrics_status": "BLOCKED",
                    "target_metrics_error": "immutable read blocked by non-empty WAL",
                })
                domain.fail(f"immutable read blocked by non-empty WAL: {self.layout.rules_db}")
                return
            with open_database(self.layout.rules_db, readonly=True, immutable=True) as conn:
                pending = int(conn.execute("SELECT COUNT(*) FROM rule_evidence_outbox WHERE consumed_at='' OR consumed_at IS NULL").fetchone()[0])
                auto = int(conn.execute("SELECT COUNT(*) FROM rule_bindings WHERE created_by IN ('auto','backfill')").fetchone()[0])
                # Preserved unknowns (for example judge_rationale retained as
                # auditable metadata) are intentional V2 evidence, not
                # migration loss. Only unresolved ledger entries block
                # readiness.
                unknown = int(conn.execute("SELECT COUNT(*) FROM rule_unknown_columns_ledger WHERE status NOT IN ('migrated','ignored','PRESERVED')").fetchone()[0])
                decision_rows = conn.execute("SELECT decision_id,actor,owner_agent_id,action,before_hash,after_hash,before_json,after_json,reason,confidence,undo_id,target_ids_json,created_at FROM rule_decisions").fetchall()
                decision_ids = {str(row[0] or "") for row in decision_rows}
                decision_maps = conn.execute("SELECT source_group_id,source_table,source_id,target_id,metadata_json FROM rule_migration_map WHERE target_table='rule_decisions' AND source_table IN ('rule_decisions','decisions')").fetchall()
                preserved_decision_siblings: set[tuple[str, str, str, str]] = set()
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_decision_anomalies'").fetchone():
                    preserved_decision_siblings = {
                        (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""), str(row[3] or ""))
                        for row in conn.execute("SELECT source_group_id,source_table,original_decision_id,sibling_decision_id FROM rule_decision_anomalies WHERE status='PRESERVED'").fetchall()
                    }
                mapped_keys: set[tuple[str, str, str]] = set()
                mapped_targets: set[str] = set()
                for row in decision_maps:
                    group = str(row[0] or "")
                    table = str(row[1] or "")
                    source_id = str(row[2] or "")
                    target_id = str(row[3] or "")
                    canonical_id = canonical_migration_source_id(source_id, row[4])
                    # A canonical alias is authoritative only when the target
                    # is backed by a PRESERVED immutable-payload anomaly.  This
                    # prevents arbitrary/extra rows from being hidden merely by
                    # marking their migration-map metadata.
                    if canonical_id != source_id and (group, table, canonical_id, target_id) not in preserved_decision_siblings:
                        canonical_id = source_id
                    mapped_keys.add((group, table, canonical_id))
                    mapped_targets.add(target_id)
                missing_decisions = set(expected_decisions) - mapped_keys
                missing_targets = {target for target in mapped_targets if target not in decision_ids}
                unmarked_decisions = decision_ids - mapped_targets
                decision_loss = len(missing_decisions) + len(missing_targets) + len(unmarked_decisions)
                decision_anomalies = int(conn.execute("SELECT COUNT(*) FROM rule_decision_anomalies WHERE status='PRESERVED'").fetchone()[0]) if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_decision_anomalies'").fetchone() else 0
                anomaly_rows = conn.execute("SELECT details_json FROM rule_unknown_column_anomalies WHERE status='PRESERVED'").fetchall() if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='rule_unknown_column_anomalies'").fetchone() else []
                unknown_anomalies = len(anomaly_rows)
                weak_legacy_anomalies = 0
                preserved_body_anomalies = 0
                for anomaly in anomaly_rows:
                    try:
                        details = json.loads(str(anomaly[0] or "{}"))
                    except (TypeError, ValueError):
                        details = {}
                    if str(details.get("reason", "")) == "weak_source_row_id":
                        weak_legacy_anomalies += 1
                    elif str(details.get("reason", "")) == "body_free_metadata_only":
                        preserved_body_anomalies += 1
                # A weak legacy ledger row may be retained alongside its
                # occurrence-bound replacement.  Other preserved anomalies already
                # correspond one-for-one with the expected rationale occurrence;
                # they must not mask an unmarked extra column.
                unknown_allowed = expected_unknown_occurrences + weak_legacy_anomalies
                unknown_excess = max(0, unknown - unknown_allowed)
                unknown_loss = max(0, unknown_allowed - unknown)
                decision_reference_complete = not missing_decisions and not missing_targets
                actual: Counter[tuple[Any, ...]] = Counter()
                for row in conn.execute("SELECT share_group_id,target_type,target_id,project_ref,provider,runtime_role,effect,priority FROM rule_bindings"):
                    actual[tuple(row)] += 1
                diff = sum((expected - actual).values()) + sum((actual - expected).values())
                actual_fences: Counter[tuple[Any, ...]] = Counter()
                fence_rows = conn.execute("SELECT share_group_id,key,request_fingerprint,memory_id,event_id,decision_id,created_at,source_ref FROM rule_idempotency_fences").fetchall()
                conflict_keys = {
                    (str(row[0] or ""), self._canonical_fence_key(row[1]))
                    for row in fence_rows
                    if "#conflict-" in str(row[1] or "")
                }
                expected_keys = {(identity[0], identity[1]) for identity in expected_fences}
                for fence in fence_rows:
                    identity = tuple(str(value or "") for value in fence)
                    canonical_key = (identity[0], self._canonical_fence_key(identity[1]))
                    if canonical_key in conflict_keys and canonical_key in expected_keys:
                        # Historical siblings are retained for audit, but only
                        # the current source payload participates in loss checks.
                        canonical = (identity[0], canonical_key[1], *identity[2:])
                        if canonical not in expected_fences:
                            continue
                        identity = canonical
                    actual_fences[identity] += 1
                fence_diff = sum((expected_fences - actual_fences).values()) + sum((actual_fences - expected_fences).values())
                fence_references_complete = all(all(bool(str(value or "")) for value in row) for row in fence_rows)
                # NO_SOURCE is a proven zero-loss state, not an unknown metric.
                # Readiness aggregates every ``*_loss`` leaf numerically; using
                # None here incorrectly turns an empty, fully validated source
                # into a BLOCKED loss result.
                fence_loss = fence_diff if (expected_fences or actual_fences) else 0
                anomaly_count = 0
                if "rule_idempotency_fence_anomalies" in {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
                    anomaly_count = int(conn.execute("SELECT COUNT(*) FROM rule_idempotency_fence_anomalies WHERE status='PRESERVED'").fetchone()[0])
                fence_status = "NO_SOURCE" if not expected_fences and not actual_fences else (("PRESERVED_CONFLICT" if anomaly_count else "MIGRATED") if fence_diff == 0 and fence_references_complete else "BLOCKED")
                # Scope/ACL identity is part of the binding multiset; expose a
                # separate digest so a validator consumer can compare it without
                # parsing the full rule rows.
                domain.metrics.update({"rule_evidence_outbox_pending": pending, "binding_identity_multiset_diff": diff, "auto_scope_expansion": auto, "unknown_columns": unknown, "unknown_source_occurrences": expected_unknown_occurrences, "unknown_preserved_expected": expected_preserved_occurrences, "unknown_target_occurrences": unknown, "unknown_loss": unknown_loss, "unknown_preserved_anomalies": unknown_anomalies, "unknown_preserved_body_anomalies": preserved_body_anomalies, "unknown_weak_legacy_anomalies": weak_legacy_anomalies, "binding_digest": _digest(sorted(actual.items())), "acl_digest": _digest(sorted(actual.items())), "loss": 0 if diff == 0 else diff, "idempotency_fence_status": fence_status, "idempotency_fence_source_count": sum(expected_fences.values()), "idempotency_fence_target_count": sum(actual_fences.values()), "idempotency_fence_loss": fence_loss, "idempotency_fence_source_digest": _digest(sorted(expected_fences.items())) if expected_fences else "", "idempotency_fence_target_digest": _digest(sorted(actual_fences.items())) if actual_fences else "", "idempotency_fence_reference_complete": fence_references_complete, "idempotency_fence_conflicts": anomaly_count, "decision_source_count": len(expected_decisions), "decision_target_count": len(decision_ids), "decision_loss": decision_loss, "decision_reference_complete": decision_reference_complete, "decision_conflicts": decision_anomalies, "decision_status": (("PRESERVED_CONFLICT" if decision_anomalies else "MIGRATED") if decision_loss == 0 else "BLOCKED"), "decision_digest": _digest(sorted((key, value) for key, value in expected_decisions.items()))})
                self._rule_intelligence_coverage(domain, source_inventory, conn)
                if pending:
                    domain.fail(f"rule_evidence_outbox_pending:{pending}")
                if diff:
                    domain.fail(f"binding_identity_multiset_diff:{diff}")
                if fence_diff:
                    domain.fail(f"idempotency_fence_loss:{fence_diff}")
                if decision_loss:
                    domain.fail(f"decision_loss:{decision_loss}")
                if not fence_references_complete:
                    domain.fail("idempotency_fence_reference_incomplete")
                if auto:
                    domain.fail(f"auto_scope_expansion:{auto}")
                if unknown and (unknown_loss or unknown_excess):
                    domain.fail(f"unknown_authoritative_columns:{unknown}")
                if expected_preserved_occurrences > preserved_body_anomalies:
                    domain.fail(f"unknown_preservation_evidence_missing:{expected_preserved_occurrences - preserved_body_anomalies}")

        evaluate_targets()

    def _unknown_sources(self, result: V2ValidationResult, inventory: Mapping[str, Mapping[str, Any]]) -> None:
        allowed = {
            "history": {"conversation_sessions", "conversation_turns", "session_summaries", "observations", "evidence_links", "history_mutation_receipts", "history_fts"},
            "knowledge": {"books", "documents", "chunks", "entities", "relations", "chunk_entities", "embeddings", "memory_candidates", "deleted_books", "index_jobs", "chunks_fts"},
            "memory": {"records", "events", "decisions", "conflicts", "quarantine", "versions", "active_version", "rule_assignments", "rule_exceptions", "rule_decisions", "rule_scope_stats", "rule_scope_evaluations", "rule_match_receipts", "rule_match_feedbacks", "rule_event_outbox", "rule_idempotency_fences", "schema_meta", "records_fts", "records_fts_data", "records_fts_idx", "records_fts_content", "records_fts_docsize", "records_fts_config"},
            "rule_intelligence": {"rule_definitions", "rule_bindings", "rule_binding_contributions", "rule_evidence", "rule_negative_evidence", "rule_runtime_feedback", "rule_effective_feedback_projection", "rule_merge_proposals", "rule_merge_decisions", "rule_merge_approvals", "rule_merge_native_requests", "rule_definition_aliases", "rule_source_links", "rule_canonical_state", "rule_reconciliation_jobs", "rule_projection_state", "rule_projection_checkpoints", "agent_reputation", "project_profile", "rule_definition_versions", "rule_definition_runtime_stats", "rule_evidence_contributions", "rule_evidence_effective", "governance_capabilities", "governance_capability_consumptions"},
        }
        for key, item in inventory.items():
            tables = set(item.get("tables") or ())
            base = "memory" if key.startswith("memory:") else key
            if base not in allowed or item.get("status") != "READY":
                continue
            unknown = sorted(name for name in tables if name not in allowed[base] and not _is_derived_table(name))
            if unknown:
                result.errors.append(f"unknown_authoritative_tables:{key}:{','.join(unknown)}")

    def validate(self, *, migration_id: str | None = None) -> V2ValidationResult:
        frozen = self._frozen_source_snapshot()
        if frozen is not None:
            self._validation_source_workspace, self._validation_source_data_home = frozen
        else:
            self._validation_source_workspace = None
            self._validation_source_data_home = None
        result = V2ValidationResult(status="PASS", migration_id=str(migration_id or self.migration_id))
        inventory = self.source_inventory()
        self._compare_source_hashes(result, inventory)
        self._unknown_sources(result, inventory)
        self._target_checks(result)
        for name, method in (("content", self._content_metrics), ("memory", self._memory_metrics), ("rules", self._rules_metrics)):
            domain = result.domains.setdefault(name, DomainValidation(name))
            try:
                method(domain, inventory)
            except Exception as exc:
                domain.fail(f"validator_exception:{type(exc).__name__}:{exc}")
        evidence = result.domains.setdefault("evidence", DomainValidation("evidence"))
        if self.layout.evidence_db.is_file():
            with open_database(self.layout.evidence_db, readonly=True, immutable=True) as conn:
                orphans = int(conn.execute("SELECT COUNT(*) FROM evidence_links l LEFT JOIN evidence e ON e.evidence_id=l.evidence_id WHERE e.evidence_id IS NULL").fetchone()[0])
                migration_maps = int(conn.execute("SELECT COUNT(*) FROM migration_map").fetchone()[0]) if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_map'").fetchone() else 0
                evidence.metrics["evidence_orphan"] = orphans
                evidence.metrics["migration_map"] = migration_maps
                if orphans:
                    evidence.fail(f"evidence_orphan:{orphans}")
        result.metrics = {name: domain.metrics for name, domain in result.domains.items()}
        errors = list(result.errors)
        for domain in result.domains.values():
            errors.extend(f"{domain.name}:{error}" for error in domain.errors)
        result.errors = errors
        result.status = "BLOCKED" if errors or any(domain.status != "PASS" for domain in result.domains.values()) else "PASS"
        result.can_promote = False
        self._validation_source_workspace = None
        self._validation_source_data_home = None
        self.last_result = result
        return result

    run = validate


__all__ = ["DomainValidation", "V2MigrationValidator", "V2ValidationResult"]
