"""V1 memory -> V2 MemoryAtom/Evidence shadow migrator.

This module is intentionally the only V2 component that knows V1 table names.
It opens every legacy SQLite source with ``mode=ro`` and reads ManagedStore
JSON files directly.  It never constructs ``SharedMemoryStore`` or
``ManagedStore`` (their constructors may create or migrate files).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..evidence import EvidenceStore
from ..evidence.store import _MIGRATION_CAPABILITY as _EVIDENCE_MIGRATION_CAPABILITY
from ..memory import MemoryAtom, MemoryAtomStore, stable_digest
from ..memory.store import _MIGRATION_CAPABILITY, _json_safe
from ..storage.layout import WorkspaceV2Layout


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(value: Any) -> str:
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, str):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    return stable_digest(value)


def _legacy_text(value: Any, default: str = "") -> str:
    """Render a legacy scalar without turning BLOBs into lossy ``repr`` text."""
    if value is None or value == "":
        return default
    if isinstance(value, (bytes, bytearray, memoryview)):
        return json.dumps(
            _json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return str(value)


def _json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return _json_safe(value)
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return _json_safe(parsed)


def _row_digest(row: Mapping[str, Any]) -> str:
    # Full row bytes are not copied to evidence metadata.  The digest still
    # commits every scalar value (including body bytes) for later audit.
    normalized = {str(key): _json_safe(value) for key, value in row.items()}
    return stable_digest(normalized)


def _sqlite_ro(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"{path.resolve().as_uri()}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _is_derived_table(name: str) -> bool:
    """Return whether a legacy table is a rebuildable search projection."""
    value = str(name or "")
    return value == "records_fts" or value.startswith("records_fts_")


@dataclass(frozen=True)
class MigrationResult:
    """Machine-readable domain-level migration result."""

    source_records: int = 0
    atoms: int = 0
    evidence: int = 0
    links: int = 0
    orphan: int = 0
    scope_digest: str = ""
    source_digest: str = ""
    target_digest: str = ""
    groups: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    counts: Mapping[str, int] = field(default_factory=dict)
    digests: Mapping[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    status: str = "ok"

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.errors and self.orphan == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "source_records": self.source_records,
            "atoms": self.atoms,
            "evidence": self.evidence,
            "links": self.links,
            "orphan": self.orphan,
            "scope_digest": self.scope_digest,
            "source_digest": self.source_digest,
            "target_digest": self.target_digest,
            "groups": {str(key): dict(value) for key, value in self.groups.items()},
            "counts": dict(self.counts),
            "digests": dict(self.digests),
            "errors": list(self.errors),
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class V1MemoryMigrator:
    """Read-only V1 source migrator for shared groups and ManagedStore JSON."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        target: str | Path | WorkspaceV2Layout | None = None,
        memory_store: MemoryAtomStore | None = None,
        evidence_store: EvidenceStore | None = None,
        source_root: str | Path | None = None,
        shared_memory_root: str | Path | None = None,
        groups: Mapping[str, str | Path] | Sequence[str | Path] | None = None,
        managed_root: str | Path | None = None,
        include_managed: bool = True,
        immutable_sources: bool = False,
        fault_hook: Callable[[str], None] | None = None,
        fail_at: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.source_root = Path(source_root).expanduser().resolve() if source_root is not None else self.workspace
        self.shared_memory_root = Path(shared_memory_root).expanduser().resolve() if shared_memory_root is not None else self.workspace / ".memoryguard" / "shared-memory"
        self.managed_root = Path(managed_root).expanduser().resolve() if managed_root is not None else self.workspace / ".memoryguard" / "managed-memory"
        target_value = target if target is not None else self.workspace
        self.memory_store = memory_store or MemoryAtomStore(target_value)
        self.evidence_store = evidence_store or EvidenceStore(target_value)
        self.groups = groups
        self.include_managed = bool(include_managed)
        self.immutable_sources = bool(immutable_sources)
        self.fault_hook = fault_hook
        self.fail_at = fail_at

    def _fault(self, step: str) -> None:
        if self.fail_at == step:
            raise RuntimeError(f"injected migration failure at {step}")
        if self.fault_hook is not None:
            self.fault_hook(step)

    def _group_paths(self) -> list[tuple[str, Path]]:
        if self.groups is not None:
            if isinstance(self.groups, Mapping):
                raw = list(self.groups.items())
            else:
                raw = []
                for item in self.groups:
                    path = Path(item).expanduser().resolve()
                    raw.append((path.parent.name, path))
            result: list[tuple[str, Path]] = []
            for group, value in raw:
                path = Path(value).expanduser().resolve()
                if path.is_dir():
                    path = path / "memory.db"
                result.append((str(group), path))
            return sorted(result, key=lambda pair: pair[0])
        roots = [self.shared_memory_root]
        legacy_alt = self.workspace / ".memoryguard" / "shared_memory"
        if legacy_alt != self.shared_memory_root:
            roots.append(legacy_alt)
        result: dict[str, Path] = {}
        for root in roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir(), key=lambda value: value.name):
                path = child / "memory.db" if child.is_dir() else child
                if path.is_file() and path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                    result.setdefault(child.name if child.is_dir() else path.stem, path.resolve())
        return sorted(result.items(), key=lambda pair: pair[0])

    def _managed_records(self) -> list[tuple[str, Path, list[dict[str, Any]]]]:
        if not self.include_managed or not self.managed_root.is_dir():
            return []
        result: list[tuple[str, Path, list[dict[str, Any]]]] = []
        for agent_dir in sorted(self.managed_root.iterdir(), key=lambda value: value.name):
            if not agent_dir.is_dir():
                continue
            active = agent_dir / "active.json"
            version_id = ""
            if active.is_file():
                try:
                    payload = json.loads(active.read_text(encoding="utf-8"))
                    if isinstance(payload, Mapping):
                        version_id = str(payload.get("version_id") or "")
                except (OSError, UnicodeError, ValueError):
                    version_id = ""
            records_file = agent_dir / "versions" / version_id / "records.jsonl" if version_id else None
            candidates = [records_file] if records_file is not None else []
            if not candidates or not candidates[0] or not candidates[0].is_file():
                candidates = sorted((agent_dir / "versions").glob("*/records.jsonl")) if (agent_dir / "versions").is_dir() else []
            for path in candidates:
                rows: list[dict[str, Any]] = []
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        value = json.loads(line)
                        if isinstance(value, Mapping):
                            rows.append(dict(value))
                except (OSError, UnicodeError, ValueError):
                    continue
                result.append((f"managed:{agent_dir.name}", path, rows))
        return result

    @staticmethod
    def _tables(conn: sqlite3.Connection) -> set[str]:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    @staticmethod
    def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY rowid").fetchall()]
        except sqlite3.Error:
            try:
                return [dict(row) for row in conn.execute(f"SELECT * FROM {_quote(table)}").fetchall()]
            except sqlite3.Error:
                return []

    @staticmethod
    def _provenance(row: Mapping[str, Any]) -> list[dict[str, Any]]:
        value = _json(row.get("provenance"), [])
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, Mapping)]

    def _record_atom(
        self,
        group: str,
        db_path: Path,
        row: Mapping[str, Any],
        *,
        managed: bool = False,
        source_path: Path | None = None,
    ) -> tuple[MemoryAtom, list[dict[str, Any]], dict[str, Any]]:
        memory_id = _legacy_text(row.get("memory_id") or row.get("id") or "")
        if not memory_id:
            raise ValueError("legacy record has no memory_id")
        provenance = self._provenance(row)
        row_digest = _row_digest(row)
        source_label = str(source_path or db_path)
        source_name = Path(source_label).name
        evidence_payload: list[dict[str, Any]] = []
        if provenance:
            for index, prov in enumerate(provenance):
                source_object = _legacy_text(prov.get("source_object_id") or prov.get("source_ref") or "unknown")
                locator = _legacy_text(prov.get("locator") or index)
                source_ref = f"{group}/{source_name}#provenance/{source_object}/{locator}"
                evidence_payload.append({
                    "source_ref": source_ref,
                    "revision": _legacy_text(prov.get("source_revision") or prov.get("revision") or ""),
                    "digest": _legacy_text(prov.get("excerpt_hash") or prov.get("digest") or row_digest),
                    "authority": "legacy_provenance",
                    "status": "valid",
                    "metadata": {"legacy": True, "group": group, "memory_id": memory_id, "locator": locator, "source_object_id": source_object},
                })
        else:
            # No parseable provenance is still explicit evidence; never create
            # an unverifiable atom with an empty evidence list.
            evidence_payload.append({
                "source_ref": f"{group}/{source_name}#records/{memory_id}",
                "revision": _legacy_text(row.get("updated_at") or row.get("created_at") or ""),
                "digest": row_digest,
                "authority": "legacy_record",
                "status": "valid",
                "metadata": {"legacy_record": True, "group": group, "memory_id": memory_id, "source_path": source_label},
            })
        metadata = _json(row.get("metadata"), {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        atom = MemoryAtom(
            atom_id="",
            memory_id=memory_id,
            body=_legacy_text(row.get("body") or ""),
            kind=_legacy_text(row.get("kind") or "fact"),
            status=_legacy_text(row.get("status") or "active"),
            confidence=float(row.get("confidence") if isinstance(row.get("confidence"), (int, float)) else 0.5),
            locked=bool(row.get("locked")),
            injection_policy=_legacy_text(row.get("injection_policy") or "relevant"),
            priority=int(row.get("priority")) if isinstance(row.get("priority"), (int, float)) else 0,
            canonical_hash=_legacy_text(row.get("canonical_hash") or stable_digest(_legacy_text(row.get("body") or ""))),
            dedup_domain=_legacy_text(row.get("dedup_domain") or "relevant"),
            supersedes=[_legacy_text(item) for item in (_json(row.get("supersedes"), []) if isinstance(_json(row.get("supersedes"), []), list) else [])],
            provenance=provenance,
            agent_instance_id=_legacy_text(row.get("agent_instance_id") or row.get("agent_id") or (group.removeprefix("managed:") if managed else "")),
            share_group_id=_legacy_text(group),
            project_ref=_legacy_text(row.get("project_ref") or metadata.get("project_ref") or ""),
            provider=_legacy_text(row.get("provider") or metadata.get("provider") or ""),
            runtime_role=_legacy_text(row.get("runtime_role") or metadata.get("runtime_role") or ""),
            workspace_id=_legacy_text(row.get("workspace_id") or ""),
            revision=int(row.get("revision") or 1),
            visibility="building",
            created_at=_legacy_text(row.get("created_at") or _now()),
            updated_at=_legacy_text(row.get("updated_at") or row.get("created_at") or _now()),
            metadata={"legacy": True, "source": source_label, **dict(metadata)},
        )
        source_map = {
            "source_domain": "managed_memory" if managed else "shared_memory",
            "source_ref": f"{group}/{source_name}",
            "source_record_id": memory_id,
            "source_revision": _legacy_text(row.get("updated_at") or row.get("created_at") or ""),
            "digest": row_digest,
            "provenance": {"provenance": provenance},
        }
        return atom, evidence_payload, source_map

    def _auxiliary_rows(self, conn: sqlite3.Connection, group: str) -> list[tuple[dict[str, Any], str, str]]:
        tables = self._tables(conn)
        ignored = {"records", "schema_meta", "sqlite_sequence", "records_fts", "active_version"}
        result: list[tuple[dict[str, Any], str, str]] = []
        for table in sorted(tables - ignored):
            # FTS5 shadow tables are derived indexes, not authoritative memory
            # events.  Migrating them as evidence can multiply a source by
            # hundreds of rows and make a large build appear never-ending.
            if _is_derived_table(table):
                continue
            for row in self._rows(conn, table):
                identifier = _legacy_text(row.get("event_id") or row.get("decision_id") or row.get("conflict_id") or row.get("group_id") or row.get("quarantine_id") or row.get("version_id") or row.get("feedback_id") or row.get("rule_id") or row.get("id") or stable_digest(_json_safe(row)))
                source_ref = f"{group}/memory.db#{table}/{identifier}"
                result.append((row, table, source_ref))
        return result

    def _migrate_sqlite_group(self, group: str, path: Path) -> dict[str, Any]:
        conn = _sqlite_ro(path, immutable=self.immutable_sources)
        atoms = 0
        source_records = 0
        queued_evidence = 0
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        atom_ids_by_memory: dict[str, str] = {}
        try:
            tables = self._tables(conn)
            rows = self._rows(conn, "records") if "records" in tables else []
            source_records = len(rows)
            with self.memory_store.migration_batch():
                for index, row in enumerate(rows):
                    self._fault(f"{group}:record:{index}:before")
                    atom, evidence_payload, source_map = self._record_atom(group, path, row)
                    persisted = self.memory_store._put_for_migration(atom, evidence=evidence_payload, source_mappings=[source_map], capability=_MIGRATION_CAPABILITY)
                    atom_ids_by_memory[persisted.memory_id] = persisted.atom_id
                    atoms += 1
                    queued_evidence += len(evidence_payload)
                    self._fault(f"{group}:record:{index}:after")
                for row, table, source_ref in self._auxiliary_rows(conn, group):
                    identifier = _legacy_text(row.get("event_id") or row.get("decision_id") or row.get("conflict_id") or row.get("group_id") or row.get("quarantine_id") or row.get("version_id") or row.get("feedback_id") or row.get("rule_id") or stable_digest(_json_safe(row)))
                    digest = _row_digest(row)
                    target_ids: list[str] = []
                    if row.get("memory_id"):
                        target_ids.append(atom_ids_by_memory.get(_legacy_text(row["memory_id"]), ""))
                    for value in _json(row.get("target_ids"), []):
                        value_id = _legacy_text(value)
                        if value_id in atom_ids_by_memory:
                            target_ids.append(atom_ids_by_memory[value_id])
                    target_ids = [value for value in dict.fromkeys(target_ids) if value]
                    evidence = {
                        "source_ref": source_ref,
                        "revision": _legacy_text(row.get("created_at") or row.get("updated_at") or ""),
                        "digest": digest,
                        "authority": "legacy_governance_event",
                        "status": "valid",
                        "metadata": {"legacy": True, "table": table, "group": group, "record_id": identifier},
                    }
                    if target_ids:
                        for target in target_ids:
                            self.memory_store._queue_evidence_for_migration(evidence, subject_type="atom", subject_id=target, aggregate_id=target, capability=_MIGRATION_CAPABILITY)
                    else:
                        self.memory_store._queue_evidence_for_migration(evidence, subject_type="migration", subject_id=group, aggregate_id=group, capability=_MIGRATION_CAPABILITY)
                    queued_evidence += 1
                # Supersession edges are inserted after all records exist and can be
                # replayed idempotently on a second migration run.
                for row in rows:
                    new_id = atom_ids_by_memory.get(_legacy_text(row.get("memory_id") or ""))
                    if not new_id:
                        continue
                    for old_memory in _json(row.get("supersedes"), []) if isinstance(_json(row.get("supersedes"), []), list) else []:
                        old_id = atom_ids_by_memory.get(_legacy_text(old_memory))
                        if old_id and old_id != new_id:
                            try:
                                self.memory_store._supersede_for_migration(old_id, new_id, share_group_id=group, reason="v1 migration", source_ref=f"{group}/memory.db#records/{row.get('memory_id')}", capability=_MIGRATION_CAPABILITY)
                            except KeyError:
                                pass
                self._fault(f"{group}:commit")
        except Exception:
            # No evidence has been projected yet.  Remove only this group's
            # atoms/events so an injected fault cannot leave a half-built scope.
            self.memory_store.rollback_scope(share_group_id=group, atom_ids=list(atom_ids_by_memory.values()))
            raise
        finally:
            conn.close()
        return {"source_records": source_records, "atoms": atoms, "queued_evidence": queued_evidence, "source_digest": source_hash, "atom_ids": atom_ids_by_memory}

    def _migrate_managed(self, group: str, path: Path, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        atoms = 0
        queued = 0
        source_digest = stable_digest([_json_safe(dict(row)) for row in rows])
        atom_ids: dict[str, str] = {}
        try:
            with self.memory_store.migration_batch():
                for index, row in enumerate(rows):
                    self._fault(f"{group}:record:{index}:before")
                    atom, evidence_payload, source_map = self._record_atom(group, path, row, managed=True, source_path=path)
                    persisted = self.memory_store._put_for_migration(atom, evidence=evidence_payload, source_mappings=[source_map], capability=_MIGRATION_CAPABILITY)
                    atom_ids[persisted.memory_id] = persisted.atom_id
                    atoms += 1
                    queued += len(evidence_payload)
                    self._fault(f"{group}:record:{index}:after")
                self._fault(f"{group}:commit")
        except Exception:
            self.memory_store.rollback_scope(share_group_id=group, atom_ids=list(atom_ids.values()))
            raise
        return {"source_records": len(rows), "atoms": atoms, "queued_evidence": queued, "source_digest": source_digest, "atom_ids": atom_ids}

    def migrate(self, *, promote: bool = False, strict: bool = True) -> MigrationResult:
        """Migrate all discovered V1 memory sources into the shadow stores.

        ``promote=False`` is the safe default.  A caller may request promotion
        only after the outbox has drained and validation reports no orphan.
        """
        source_records = atoms = queued = 0
        groups: dict[str, dict[str, Any]] = {}
        source_digests: dict[str, str] = {}
        errors: list[str] = []
        try:
            for group, path in self._group_paths():
                self._fault(f"{group}:before")
                metrics = self._migrate_sqlite_group(group, path)
                source_records += int(metrics["source_records"])
                atoms += int(metrics["atoms"])
                queued += int(metrics["queued_evidence"])
                source_digests[group] = str(metrics["source_digest"])
                groups[group] = {key: value for key, value in metrics.items() if key != "atom_ids"}
                self._fault(f"{group}:after")
            for group, path, rows in self._managed_records():
                self._fault(f"{group}:before")
                metrics = self._migrate_managed(group, path, rows)
                source_records += int(metrics["source_records"])
                atoms += int(metrics["atoms"])
                queued += int(metrics["queued_evidence"])
                source_digests[group] = str(metrics["source_digest"])
                groups[group] = {key: value for key, value in metrics.items() if key != "atom_ids"}
                self._fault(f"{group}:after")
            projection = self.memory_store.project_evidence(self.evidence_store)
            if projection.get("failed"):
                errors.append(f"evidence_projection_failed:{projection['failed']}")
            for mapping in self.memory_store.list_source_mappings():
                self.evidence_store._record_migration_map_for_migration(
                    str(mapping.get("source_domain") or ""),
                    str(mapping.get("source_ref") or ""),
                    str(mapping.get("source_record_id") or ""),
                    "atom",
                    str(mapping.get("atom_id") or ""),
                    metadata={"digest": str(mapping.get("digest") or ""), "source_revision": str(mapping.get("source_revision") or "")},
                    capability=_EVIDENCE_MIGRATION_CAPABILITY,
                )
            validation = self.memory_store.validate(self.evidence_store, include_building=True)
            if validation.orphan_count:
                errors.extend(validation.errors)
            if promote and not errors and validation.ok:
                self.memory_store.promote("ready")
            source_digest = stable_digest(source_digests)
            evidence_status = self.evidence_store.status()
            evidence_count = int(evidence_status.get("evidence", 0))
            link_count = int(evidence_status.get("links", 0))
            target_digest = stable_digest({"atoms": self.memory_store.status(), "evidence": evidence_status, "validation": validation.to_dict()})
            result = MigrationResult(source_records=source_records, atoms=atoms, evidence=evidence_count, links=link_count, orphan=validation.orphan_count, scope_digest=validation.scope_digest, source_digest=source_digest, target_digest=target_digest, groups=groups, counts={"source_records": source_records, "atoms": atoms, "evidence": evidence_count, "links": link_count, "orphan": validation.orphan_count}, digests={"source": source_digest, "target": target_digest, "scope": validation.scope_digest}, errors=tuple(errors), status="ok" if not errors else "failed")
            return result
        except Exception as exc:
            if strict:
                raise
            errors.append(f"{type(exc).__name__}:{exc}")
            return MigrationResult(source_records=source_records, atoms=atoms, evidence=0, orphan=0, scope_digest="", source_digest=stable_digest(source_digests), groups=groups, counts={"source_records": source_records, "atoms": atoms}, digests={"source": stable_digest(source_digests)}, errors=tuple(errors), status="failed")

    run = migrate


__all__ = ["MigrationResult", "V1MemoryMigrator"]
