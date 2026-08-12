# -*- coding: utf-8 -*-
"""V2 migration batch does not re-open its target connection while writing."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import sqlite3
from pathlib import Path

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.migration.memory import V1GroupReader, V1MemoryMigrator
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _legacy_row(memory_id: str, body: str, **extra) -> tuple:
    values = {
        "memory_id": memory_id,
        "body": body,
        "kind": "fact",
        "status": "active",
        "confidence": 0.9,
        "locked": 0,
        "injection_policy": "relevant",
        "priority": 0,
        "supersedes": "[]",
        "provenance": "[]",
        "agent_instance_id": "agent-0",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "canonical_hash": hashlib.sha256(body.encode()).hexdigest(),
        "dedup_domain": "relevant",
    }
    values.update(extra)
    return tuple(
        values[key]
        for key in (
            "memory_id", "body", "kind", "status", "confidence", "locked",
            "injection_policy", "priority", "supersedes", "provenance",
            "agent_instance_id", "created_at", "updated_at", "canonical_hash",
            "dedup_domain",
        )
    )


def _legacy_group(root: Path, group_id: str, rows: list[tuple]) -> Path:
    path = root / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, "
            "supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, "
            "updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        conn.execute(
            "CREATE TABLE decisions (event_id TEXT PRIMARY KEY, actor TEXT, "
            "action TEXT, target_ids TEXT, created_at TEXT)"
        )
        conn.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?)",
            (f"decision-{group_id}", "operator", "inventory", "[]", "now"),
        )
    return path


def _activate_v2(root: Path) -> None:
    initialize_all(WorkspaceV2Layout(root))
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="transaction-self-lock")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="transaction-self-lock-source",
        target_digest="transaction-self-lock-target",
        manifest_digest="transaction-self-lock-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _migrator(source: Path, target: Path, source_path: Path) -> V1MemoryMigrator:
    GroupControlService(target, write=True).bind_agent("migration-agent", "self-lock-v2")
    return V1MemoryMigrator(
        source,
        target=target,
        groups={"self-lock-v1": source_path},
        group_targets={"self-lock-v1": "self-lock-v2"},
        include_managed=False,
        immutable_sources=True,
    )


def test_v2_migration_batch_uses_one_target_connection(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_path = _legacy_group(
        source,
        "self-lock-v1",
        [
            _legacy_row("sl-1", "must run tests before commit"),
            _legacy_row("sl-2", "must run lint before commit"),
        ],
    )
    _activate_v2(target)
    migrator = _migrator(source, target, source_path)

    first = migrator.migrate()
    assert first.ok, first.to_dict()
    assert V1GroupReader(source, "self-lock-v1", source_path, immutable=True).inventory().records == 2

    opened = {"batches": 0, "nested_target_connections": 0}
    real_batch = MemoryAtomStore.migration_batch
    real_connect = MemoryAtomStore._checked_connect

    @contextmanager
    def tracking_batch(self):
        opened["batches"] += 1
        with real_batch(self) as conn:
            yield conn

    def tracking_connect(self, *args, **kwargs):
        if getattr(self._migration_state, "conn", None) is not None:
            opened["nested_target_connections"] += 1
        return real_connect(self, *args, **kwargs)

    monkeypatch.setattr(MemoryAtomStore, "migration_batch", tracking_batch)
    monkeypatch.setattr(MemoryAtomStore, "_checked_connect", tracking_connect)

    second = migrator.migrate()

    assert second.ok, second.to_dict()
    assert opened == {"batches": 1, "nested_target_connections": 0}
    memory = MemoryAtomStore(target)
    assert len(memory.list_source_mappings()) == 2
    assert memory.validate(migrator.evidence_store, include_building=True).orphan_count == 0


def test_v2_migration_source_mapping_read_works_outside_batch(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_path = _legacy_group(source, "self-lock-read-v1", [_legacy_row("slr-1", "read after migration")])
    _activate_v2(target)
    GroupControlService(target, write=True).bind_agent("migration-agent", "self-lock-read-v2")
    migrator = V1MemoryMigrator(
        source,
        target=target,
        groups={"self-lock-read-v1": source_path},
        group_targets={"self-lock-read-v1": "self-lock-read-v2"},
        include_managed=False,
        immutable_sources=True,
    )

    result = migrator.migrate()
    assert result.ok, result.to_dict()

    memory = MemoryAtomStore(target)
    scope = MemoryReadScope(
        workspace_id=str(target.resolve()),
        share_group_id="self-lock-read-v2",
        admin=True,
    )
    atoms = memory.list_atoms(scope=scope, include_building=True)
    assert [(atom.memory_id, atom.body) for atom in atoms] == [("slr-1", "read after migration")]
    mappings = memory.list_source_mappings()
    assert mappings and mappings[0]["source_record_id"] == "slr-1"
