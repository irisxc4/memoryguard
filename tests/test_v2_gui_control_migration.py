from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard.migration.gui_control import (
    GuiControlMigrationError,
    inspect_legacy_gui_control,
    migrate_legacy_gui_control,
)
from memoryguard.runtime_v2.group_native import (
    GroupControlService,
    SystemControlStore,
    personal_group_id,
)


def _legacy(root: Path, name: str, *, agent: str, group: str, status: str = "active") -> Path:
    path = root / ".memoryguard" / "agent-bindings" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "binding_id": name,
            "agent_instance_id": agent,
            "share_group_id": group,
            "mcp_server_name": "memoryguard",
            "native_memory_mode": "observed",
            "status": status,
            "redirect_paths": [],
            "bound_at": "2026-08-01T00:00:00+00:00",
            "last_drift_check": "",
        }),
        encoding="utf-8",
    )
    return path


def test_gui_control_migration_is_zero_write_in_preview_and_idempotent_on_apply(tmp_path: Path) -> None:
    _legacy(tmp_path, "b-a", agent="agent-a", group="shared-team")
    _legacy(tmp_path, "b-old", agent="agent-a", group="old-team", status="inactive")
    preview = inspect_legacy_gui_control(tmp_path)
    assert preview["record_count"] == 2
    assert preview["active_count"] == 1
    assert preview["inactive_count"] == 1
    assert not (tmp_path / ".memoryguard" / "system" / "manifest.db").exists()

    first = migrate_legacy_gui_control(tmp_path)
    assert first["migrated_count"] == 2
    assert first["changed"] is True
    second = migrate_legacy_gui_control(tmp_path)
    assert second["replayed"] is True
    assert second["changed"] is False
    rows = GroupControlService(tmp_path).list_bindings(include_inactive=True)["bindings"]
    assert {(row["binding_id"], row["status"]) for row in rows} == {
        ("b-a", "active"), ("b-old", "inactive")
    }


def test_gui_control_migration_fails_closed_if_legacy_source_changes_after_receipt(tmp_path: Path) -> None:
    path = _legacy(tmp_path, "b-a", agent="agent-a", group="shared-team")
    migrate_legacy_gui_control(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["share_group_id"] = "changed-team"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GuiControlMigrationError, match="idempotency_key_reused"):
        migrate_legacy_gui_control(tmp_path)
    assert GroupControlService(tmp_path).active_binding_for_agent("agent-a")["share_group_id"] == "shared-team"


def test_gui_control_migration_repairs_missing_binding_after_success_receipt(tmp_path: Path) -> None:
    _legacy(tmp_path, "b-a", agent="agent-a", group="shared-team")
    migrate_legacy_gui_control(tmp_path)

    store = SystemControlStore(tmp_path)
    with store.connection(write=True) as conn:
        conn.execute("DELETE FROM agent_group_bindings WHERE binding_id=?", ("b-a",))
        conn.commit()

    repaired = migrate_legacy_gui_control(tmp_path)
    assert repaired["repaired"] is True
    assert repaired["migrated_count"] == 1
    assert repaired["changed"] is True
    assert GroupControlService(tmp_path).active_binding_for_agent("agent-a")["share_group_id"] == "shared-team"

    replayed = migrate_legacy_gui_control(tmp_path)
    assert replayed["replayed"] is True
    assert replayed["changed"] is False


def test_gui_control_migration_rejects_binding_identity_conflict_after_success_receipt(tmp_path: Path) -> None:
    _legacy(tmp_path, "b-a", agent="agent-a", group="shared-team")
    migrate_legacy_gui_control(tmp_path)

    store = SystemControlStore(tmp_path)
    with store.connection(write=True) as conn:
        conn.execute(
            "UPDATE agent_group_bindings SET share_group_id=? WHERE binding_id=?",
            ("changed-team", "b-a"),
        )
        conn.commit()

    with pytest.raises(GuiControlMigrationError, match="v2_binding_identity_conflict"):
        migrate_legacy_gui_control(tmp_path)


def test_gui_control_migration_rejects_multiple_active_or_invalid_personal_binding(tmp_path: Path) -> None:
    _legacy(tmp_path, "b-a", agent="agent-a", group="shared-one")
    _legacy(tmp_path, "b-b", agent="agent-a", group="shared-two")
    with pytest.raises(GuiControlMigrationError, match="multiple_active_legacy_bindings"):
        migrate_legacy_gui_control(tmp_path)
    assert not (tmp_path / ".memoryguard" / "system" / "manifest.db").exists()

    other = tmp_path / "other"
    other.mkdir()
    _legacy(other, "p-a", agent="agent-a", group=personal_group_id("agent-b"))
    with pytest.raises(GuiControlMigrationError, match="personal_group_owner_mismatch"):
        migrate_legacy_gui_control(other)


def test_gui_control_migration_module_does_not_import_legacy_store() -> None:
    source = Path("src/memoryguard/migration/gui_control.py").read_text(encoding="utf-8")
    assert "from ..agent_binding import" not in source
    assert "from ..shared_memory_store import" not in source
