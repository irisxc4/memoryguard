# -*- coding: utf-8 -*-
"""V1 group discovery and migration through the V2 control/data planes."""
from __future__ import annotations

import hashlib
import json
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
    return tuple(values[key] for key in (
        "memory_id", "body", "kind", "status", "confidence", "locked",
        "injection_policy", "priority", "supersedes", "provenance",
        "agent_instance_id", "created_at", "updated_at", "canonical_hash",
        "dedup_domain",
    ))


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
    manager.transition(ManifestState.V2_BUILDING, migration_id="group-migration-core")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="group-migration-source",
        target_digest="group-migration-target",
        manifest_digest="group-migration-manifest",
        digests={"validator_passed": True, "checkpoints": {"core": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def test_v1_reader_preflight_lists_only_populated_groups(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _legacy_group(ws, "shared-a", [_legacy_row("r1", "one")])
    _legacy_group(ws, "shared-empty", [])

    inventories = V1GroupReader.discover(ws)
    populated = [item for item in inventories if item.records > 0]
    assert [item.group_id for item in populated] == ["shared-a"]
    assert populated[0].records == 1
    assert populated[0].active == 1
    assert all(item.ok for item in inventories)


def test_v1_reader_preflight_selects_largest_group(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _legacy_group(ws, "shared-a", [_legacy_row(f"r{i}", f"body {i}") for i in range(2)])
    _legacy_group(ws, "shared-b", [_legacy_row("r9", "solo")])

    largest = max(V1GroupReader.discover(ws), key=lambda item: (item.records, item.group_id))
    assert largest.group_id == "shared-a"
    assert largest.records == 2


def test_v1_memory_migrator_round_trip_and_idempotency(tmp_path):
    src_ws = tmp_path / "src"
    tgt_ws = tmp_path / "tgt"
    src_ws.mkdir()
    tgt_ws.mkdir()
    source_path = _legacy_group(src_ws, "shared-legacy", [
        _legacy_row("mig-0", "rule 0"),
        _legacy_row("mig-1", "rule 1"),
        _legacy_row(
            "mig-rule", "must rule", kind="preference",
            injection_policy="always", priority=5,
        ),
    ])
    _activate_v2(tgt_ws)
    GroupControlService(tgt_ws, write=True).bind_agent("migration-agent", "shared-new")

    migrator = V1MemoryMigrator(
        src_ws,
        target=tgt_ws,
        groups={"shared-legacy": source_path},
        group_targets={"shared-legacy": "shared-new"},
        include_managed=False,
    )
    before = MemoryAtomStore(tgt_ws).status()["atoms"]

    dry = migrator.preview()
    assert dry.ok and dry.source_records == 3 and dry.atoms == 0
    assert MemoryAtomStore(tgt_ws).status()["atoms"] == before

    result = migrator.migrate()
    assert result.ok, result.to_dict()
    scope = MemoryReadScope(
        workspace_id=str(tgt_ws.resolve()),
        share_group_id="shared-new",
        admin=True,
    )
    memory = MemoryAtomStore(tgt_ws)
    atoms = memory.list_atoms(scope=scope, include_building=True)
    by_id = {atom.memory_id: atom for atom in atoms}
    assert len(by_id) == 3
    assert by_id["mig-0"].body == "rule 0"
    assert by_id["mig-rule"].injection_policy == "always"
    assert by_id["mig-rule"].priority == 5
    mappings = memory.list_source_mappings()
    assert any(
        item["source_ref"].startswith("shared-legacy/")
        and item["source_record_id"] == "mig-1"
        for item in mappings
    )

    second = migrator.migrate()
    assert second.ok, second.to_dict()
    assert len(memory.list_atoms(scope=scope, include_building=True)) == 3
    assert len(memory.list_source_mappings()) == len(mappings)
    assert memory.validate(migrator.evidence_store, include_building=True).orphan_count == 0


def test_explicit_v1_migration_preview_is_the_legacy_preflight(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_db = _legacy_group(source, "shared-legacy", [_legacy_row("r1", "data")])
    _activate_v2(target)

    reader = V1GroupReader(source, "shared-legacy", source_db, immutable=True)
    inventory = reader.inventory()
    assert inventory.ok and inventory.records == 1 and inventory.active == 1

    migrator = V1MemoryMigrator(
        source,
        target=target,
        groups={"shared-legacy": source_db},
        group_targets={"shared-legacy": "shared-new"},
        include_managed=False,
        immutable_sources=True,
    )
    preview = migrator.preview()
    assert preview.ok
    assert preview.source_records == 1
    assert preview.groups["shared-legacy"]["target_group"] == "shared-new"
    assert MemoryAtomStore(target).status()["atoms"] == 0


def test_v2_bind_agents_does_not_read_retired_v1_layout(tmp_path, monkeypatch):
    _legacy_group(tmp_path, "shared-legacy", [_legacy_row("r1", "data")])
    _activate_v2(tmp_path)
    service = GroupControlService(tmp_path, write=True)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("V2 group binding must not inspect the retired V1 layout")

    monkeypatch.setattr(V1GroupReader, "discover", fail_if_called)
    result = service.bind_agents(["a", "b", "c"], idempotency_key="v2-auto-group")

    assert result["ok"] is True
    assert result["member_count"] == 3
    assert not (tmp_path / ".memoryguard" / "shared-memory" / result["share_group_id"]).exists()


def test_shared_memory_store_warns_but_does_not_raise_on_new_group(tmp_path):
    _legacy_group(tmp_path, "shared-legacy", [_legacy_row("r1", "data")])
    _activate_v2(tmp_path)
    service = GroupControlService(tmp_path, write=True)
    result = service.bind_agent("new-agent", "shared-another-fresh")

    assert result["ok"] is True
    assert not (tmp_path / ".memoryguard" / "shared-memory" / "shared-another-fresh").exists()
    assert [item.group_id for item in V1GroupReader.discover(tmp_path)] == ["shared-legacy"]
