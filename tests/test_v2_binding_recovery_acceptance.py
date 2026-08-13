from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from memoryguard.memory.store import MemoryAtomStore
from memoryguard.migration.upgrade import run_upgrade
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.system.manifest import ManifestManager, ManifestState


def _legacy_row(memory_id: str, body: str) -> tuple[object, ...]:
    return (
        memory_id,
        body,
        "fact",
        "active",
        0.9,
        0,
        "relevant",
        0,
        "[]",
        "[]",
        "legacy-agent",
        "2026-08-01T00:00:00+00:00",
        "2026-08-01T00:00:00+00:00",
        hashlib.sha256(body.encode()).hexdigest(),
        "relevant",
    )


def _write_legacy_group(root: Path, group_id: str = "shared-team") -> Path:
    database = root / ".memoryguard" / "shared-memory" / group_id / "memory.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE records ("
            "memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT, "
            "confidence REAL, locked INTEGER, injection_policy TEXT, priority INTEGER, "
            "supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, "
            "updated_at TEXT, canonical_hash TEXT, dedup_domain TEXT)"
        )
        connection.execute(
            "CREATE TABLE decisions (event_id TEXT PRIMARY KEY, actor TEXT, "
            "action TEXT, target_ids TEXT, created_at TEXT)"
        )
        connection.execute(
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _legacy_row("legacy-memory", "migrated memory"),
        )
        connection.commit()
    return database


def _write_legacy_binding(
    root: Path,
    binding_id: str,
    agent_instance_id: str,
    group_id: str,
    *,
    status: str,
) -> Path:
    binding = root / ".memoryguard" / "agent-bindings" / f"{binding_id}.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(
        json.dumps(
            {
                "binding_id": binding_id,
                "agent_instance_id": agent_instance_id,
                "share_group_id": group_id,
                "mcp_server_name": "memoryguard",
                "native_memory_mode": "observed",
                "status": status,
                "redirect_paths": [],
                "bound_at": "2026-08-01T00:00:00+00:00",
                "last_drift_check": "",
            }
        ),
        encoding="utf-8",
    )
    return binding


def _upgrade_to_ready(root: Path) -> dict[str, object]:
    report = run_upgrade(root, data_home=root, apply=True)
    assert report["ok"] is True, report
    assert report["status"] == ManifestState.V2_READY.value
    return report


def _activate(root: Path) -> dict[str, object]:
    report = run_upgrade(root, data_home=root, apply=True, confirm="V2_ACTIVE")
    assert report["ok"] is True, report
    assert report["status"] == ManifestState.V2_ACTIVE.value
    return report


def test_v1_active_and_inactive_bindings_keep_membership_relationships_after_upgrade(tmp_path: Path) -> None:
    _write_legacy_group(tmp_path)
    _write_legacy_binding(tmp_path, "binding-active", "agent-active", "shared-team", status="active")
    _write_legacy_binding(tmp_path, "binding-inactive", "agent-inactive", "shared-team", status="inactive")

    _upgrade_to_ready(tmp_path)
    service = GroupControlService(tmp_path)

    bindings = service.list_bindings(include_inactive=True)["bindings"]
    by_id = {item["binding_id"]: item for item in bindings}
    assert set(by_id) == {"binding-active", "binding-inactive"}
    assert by_id["binding-active"]["agent_instance_id"] == "agent-active"
    assert by_id["binding-active"]["status"] == "active"
    assert by_id["binding-inactive"]["agent_instance_id"] == "agent-inactive"
    assert by_id["binding-inactive"]["status"] == "inactive"

    assert service.list_bindings(include_inactive=False)["bindings"] == [by_id["binding-active"]]
    preview = service.group_preview("shared-team")
    assert preview["members"] == ["agent-active"]
    assert preview["member_count"] == 1


def test_migrated_group_with_zero_members_is_listable_and_accepts_new_agent_without_native_memory(
    tmp_path: Path,
) -> None:
    _write_legacy_group(tmp_path, "shared-orphaned")

    _upgrade_to_ready(tmp_path)
    service = GroupControlService(tmp_path)

    listed = service.list_share_groups()
    group = next(item for item in listed["groups"] if item["share_group_id"] == "shared-orphaned")
    assert group["members"] == []
    assert group["member_count"] == 0
    assert group["record_count"] == 1

    before = service.group_preview("shared-orphaned")
    assert before["member_count"] == 0
    assert before["memory_count"] == 1

    bound = service.bind_agent("agent-without-native-memory", "shared-orphaned")
    assert bound["ok"] is True
    assert bound["member_count"] == 1
    assert service.selected_source_ids("agent-without-native-memory") == []
    assert service.group_preview("shared-orphaned")["members"] == ["agent-without-native-memory"]


def test_binding_and_native_memory_selection_are_independent(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)

    bound_without_selection = service.bind_agent("agent-without-selection", "shared-team")
    assert bound_without_selection["ok"] is True
    assert service.selected_source_ids("agent-without-selection") == []

    selected_without_binding = service.record_selection(
        "agent-without-binding", ["native-source-1"], "selection-digest"
    )
    assert selected_without_binding["ok"] is True
    assert service.selected_source_ids("agent-without-binding") == ["native-source-1"]
    assert service.active_binding_for_agent("agent-without-binding") is None

    service.bind_agent("agent-without-binding", "shared-team")
    assert service.selected_source_ids("agent-without-binding") == ["native-source-1"]


def test_active_control_break_fails_closed_on_preview_and_recovers_on_apply(tmp_path: Path) -> None:
    _write_legacy_group(tmp_path)
    legacy_binding = _write_legacy_binding(
        tmp_path, "binding-recover", "agent-recover", "shared-team", status="active"
    )
    _upgrade_to_ready(tmp_path)
    _activate(tmp_path)

    layout = WorkspaceV2Layout(tmp_path)
    with sqlite3.connect(layout.manifest_db) as connection:
        connection.execute("DELETE FROM agent_group_bindings")
        connection.execute(
            "DELETE FROM group_operation_receipts WHERE operation='migrate_legacy_agent_bindings'"
        )
        connection.execute(
            "DELETE FROM group_outbox WHERE event_type='migrate_legacy_agent_bindings'"
        )
        connection.commit()

    service = GroupControlService(tmp_path)
    assert service.list_bindings(include_inactive=True)["total"] == 0

    preview = run_upgrade(tmp_path, data_home=tmp_path)
    assert preview["ok"] is False
    assert preview["status"] == "BLOCKED"
    assert preview["stage"] == "preflight"
    assert preview["code"] == "active_control_repair_required"
    assert preview["writes_performed"] is False
    assert preview["detail"]["missing_binding_ids"] == [legacy_binding.stem]
    assert ManifestManager(tmp_path).current().state is ManifestState.V2_ACTIVE

    repaired = run_upgrade(tmp_path, data_home=tmp_path, apply=True)
    assert repaired["ok"] is True, repaired
    assert repaired["status"] == ManifestState.V2_ACTIVE.value
    assert repaired["code"] == "active_control_repaired"
    assert repaired["writes_performed"] is True
    restored = GroupControlService(tmp_path).active_binding_for_agent("agent-recover")
    assert restored is not None
    assert restored["binding_id"] == legacy_binding.stem
    assert restored["share_group_id"] == "shared-team"


def test_activation_exposes_migrated_memory_and_active_upgrade_repairs_split_state(tmp_path: Path) -> None:
    _write_legacy_group(tmp_path)
    _upgrade_to_ready(tmp_path)
    active = _activate(tmp_path)
    assert active["detail"]["memory"]["after"]["status"] == "PASS"

    memory_db = WorkspaceV2Layout(tmp_path).memory_db
    with sqlite3.connect(memory_db) as connection:
        assert connection.execute("SELECT DISTINCT visibility FROM atoms").fetchall() == [("active",)]
        assert connection.execute(
            "SELECT state FROM domain_state WHERE domain='memory'"
        ).fetchone() == ("ACTIVE",)
        connection.execute("UPDATE atoms SET visibility='building'")
        connection.execute(
            "UPDATE domain_state SET state='BUILDING' WHERE domain='memory'"
        )
        connection.commit()

    preview = run_upgrade(tmp_path, data_home=tmp_path, apply=False)
    assert preview["ok"] is False
    assert preview["code"] == "active_memory_repair_required"
    repaired = run_upgrade(tmp_path, data_home=tmp_path, apply=True)
    assert repaired["ok"] is True, repaired
    assert repaired["code"] == "active_memory_repaired"
    assert repaired["detail"]["memory"]["status"] == "PASS"


def test_activation_creates_governance_ledger_and_active_upgrade_repairs_missing_ledger(tmp_path: Path) -> None:
    _write_legacy_group(tmp_path)
    _upgrade_to_ready(tmp_path)
    _activate(tmp_path)

    ledger = tmp_path / ".memoryguard" / "governance_v2" / "decisions.db"
    assert ledger.is_file()
    connection = sqlite3.connect(ledger)
    try:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
    assert {"decisions", "request_ledger", "decision_outbox"} <= tables

    split_root = tmp_path / "historical-split"
    _write_legacy_group(split_root)
    _upgrade_to_ready(split_root)
    manager = ManifestManager(split_root)
    ready = manager.current()
    manager.activate_v2(expected_generation=ready.generation)
    MemoryAtomStore(split_root).set_visibility("active")

    split_ledger = split_root / ".memoryguard" / "governance_v2" / "decisions.db"
    assert not split_ledger.exists()
    preview = run_upgrade(split_root, data_home=split_root, apply=False)
    assert preview["ok"] is False
    assert preview["code"] == "active_governance_repair_required"

    repaired = run_upgrade(split_root, data_home=split_root, apply=True)
    assert repaired["ok"] is True, repaired
    assert repaired["code"] == "active_governance_repaired"
    assert repaired["detail"]["governance"]["status"] == "PASS"
    assert split_ledger.is_file()
