"""One-time migration of legacy AgentBinding JSON metadata into V2 system control.

The migration reads the old ``.memoryguard/agent-bindings/*.json`` files as a
frozen metadata source.  It deliberately does not import AgentBindingStore or
SharedMemoryStore, and it never reads or mutates legacy memory bodies.

Runtime code must not fall back to these JSON files.  This module exists only
for an explicit operator upgrade step and records a digest-bound receipt in the
V2 system control plane so a changed legacy source fails closed after migration.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..runtime_v2.group_native import (
    GroupControlError,
    SystemControlStore,
    _group_kind,
    personal_group_id,
)
from ..storage.transaction import transaction


class GuiControlMigrationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "gui_control_migration_failed")
        super().__init__(self.code)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _missing_legacy_bindings(conn: Any, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,"
        "native_memory_mode,redirect_paths_json,status FROM agent_group_bindings "
        "ORDER BY binding_id"
    ).fetchall()
    existing_by_id = {str(row[0]): row for row in rows}
    missing: list[dict[str, Any]] = []
    for record in records:
        old = existing_by_id.get(record["binding_id"])
        if old is None:
            missing.append(record)
            continue
        expected = (
            record["agent_instance_id"],
            record["share_group_id"],
            _group_kind(record["share_group_id"]),
            record["mcp_server_name"],
            record["native_memory_mode"],
            _canonical(record["redirect_paths"]),
            record["status"],
        )
        actual = tuple(str(old[index]) for index in range(1, 8))
        if actual != expected:
            raise GuiControlMigrationError("v2_binding_identity_conflict")
    return missing


def _validate_success_receipt(
    receipt: Mapping[str, Any], *, source_digest: str, record_count: int,
) -> Mapping[str, Any]:
    result = receipt.get("result")
    if not isinstance(result, Mapping):
        raise GuiControlMigrationError("control_receipt_corrupt")
    if (
        result.get("ok") is not True
        or result.get("status") != "succeeded"
        or result.get("source_digest") != source_digest
        or result.get("record_count") != record_count
    ):
        raise GuiControlMigrationError("control_receipt_inconsistent")
    return result


def _load_legacy_bindings(workspace: Path) -> tuple[list[dict[str, Any]], str]:
    root = workspace / ".memoryguard" / "agent-bindings"
    if not root.is_dir():
        return [], _digest([])
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GuiControlMigrationError("legacy_binding_json_invalid") from exc
        if not isinstance(raw, Mapping):
            raise GuiControlMigrationError("legacy_binding_record_invalid")
        required = ("binding_id", "agent_instance_id", "share_group_id", "mcp_server_name")
        values = {key: str(raw.get(key) or "").strip() for key in required}
        if not all(values.values()):
            raise GuiControlMigrationError("legacy_binding_record_invalid")
        status = str(raw.get("status") or "active").strip().casefold()
        if status not in {"active", "inactive"}:
            raise GuiControlMigrationError("legacy_binding_status_invalid")
        mode = str(raw.get("native_memory_mode") or "observed").strip() or "observed"
        redirects_raw = raw.get("redirect_paths") or []
        if not isinstance(redirects_raw, list) or any(not isinstance(item, str) for item in redirects_raw):
            raise GuiControlMigrationError("legacy_binding_redirects_invalid")
        record = {
            **values,
            "native_memory_mode": mode,
            "status": status,
            "redirect_paths": list(redirects_raw),
            "bound_at": str(raw.get("bound_at") or "").strip(),
            "last_drift_check": str(raw.get("last_drift_check") or "").strip(),
            "source_file": path.name,
            "source_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        if _group_kind(record["share_group_id"]) == "personal":
            try:
                expected = personal_group_id(record["agent_instance_id"])
            except GroupControlError as exc:
                raise GuiControlMigrationError(exc.code) from exc
            if record["share_group_id"] != expected:
                raise GuiControlMigrationError("personal_group_owner_mismatch")
        records.append(record)

    active_by_agent: dict[str, list[str]] = {}
    for record in records:
        if record["status"] == "active":
            active_by_agent.setdefault(record["agent_instance_id"], []).append(record["binding_id"])
    if any(len(ids) > 1 for ids in active_by_agent.values()):
        raise GuiControlMigrationError("multiple_active_legacy_bindings")
    ids = [record["binding_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise GuiControlMigrationError("duplicate_legacy_binding_id")
    return records, _digest(records)


def inspect_legacy_gui_control(workspace: str | Path) -> dict[str, Any]:
    """Zero-write preview of the legacy binding metadata migration."""
    root = Path(workspace).expanduser().resolve()
    records, source_digest = _load_legacy_bindings(root)
    return {
        "ok": True,
        "status": "PREVIEW",
        "source": "legacy_agent_bindings_json",
        "source_digest": source_digest,
        "record_count": len(records),
        "active_count": sum(item["status"] == "active" for item in records),
        "inactive_count": sum(item["status"] == "inactive" for item in records),
        "write_required": bool(records),
    }


def migrate_legacy_gui_control(
    workspace: str | Path,
    *,
    records: list[Mapping[str, Any]] | None = None,
    source_digest: str | None = None,
) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve()
    if records is None:
        loaded_records, loaded_digest = _load_legacy_bindings(root)
        records = loaded_records
        source_digest = loaded_digest
    else:
        records = [dict(record) for record in records]
        source_digest = str(source_digest or _digest(records))
    source_digest = str(source_digest or _digest(records))
    control = SystemControlStore(root, write=True)
    request = {
        "source": "legacy_agent_bindings_json",
        "source_digest": source_digest,
        "record_count": len(records),
    }

    def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
        existing = conn.execute(
            "SELECT binding_id,agent_instance_id,share_group_id,status FROM agent_group_bindings ORDER BY binding_id"
        ).fetchall()
        existing_by_id = {str(row[0]): row for row in existing}
        _missing_legacy_bindings(conn, records)
        migrated = 0
        active = 0
        inactive = 0
        for record in records:
            old = existing_by_id.get(record["binding_id"])
            if old is not None:
                if (
                    str(old[1]) != record["agent_instance_id"]
                    or str(old[2]) != record["share_group_id"]
                    or str(old[3]) != record["status"]
                ):
                    raise GuiControlMigrationError("v2_binding_identity_conflict")
                continue
            if record["status"] == "active":
                conflicting = conn.execute(
                    "SELECT binding_id,share_group_id FROM agent_group_bindings "
                    "WHERE agent_instance_id=? AND status='active'",
                    (record["agent_instance_id"],),
                ).fetchone()
                if conflicting is not None:
                    raise GuiControlMigrationError("v2_active_binding_conflict")
            created_at = record["bound_at"] or record["last_drift_check"] or "1970-01-01T00:00:00+00:00"
            updated_at = record["last_drift_check"] or created_at
            conn.execute(
                "INSERT INTO agent_group_bindings(binding_id,agent_instance_id,share_group_id,group_kind,mcp_server_name,native_memory_mode,redirect_paths_json,status,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                (
                    record["binding_id"],
                    record["agent_instance_id"],
                    record["share_group_id"],
                    _group_kind(record["share_group_id"]),
                    record["mcp_server_name"],
                    record["native_memory_mode"],
                    _canonical(record["redirect_paths"]),
                    record["status"],
                    created_at,
                    updated_at,
                ),
            )
            migrated += 1
            if record["status"] == "active":
                active += 1
            else:
                inactive += 1
        result = {
            "ok": True,
            "status": "succeeded",
            "source": "legacy_agent_bindings_json",
            "source_digest": source_digest,
            "record_count": len(records),
            "migrated_count": migrated,
            "active_count": active,
            "inactive_count": inactive,
            "changed": migrated > 0,
        }
        return result, "gui-control-migration"

    try:
        request_digest = _digest(request)
        receipt = control.read_receipt("migrate_legacy_agent_bindings", "legacy-agent-bindings-v1")
        if receipt is not None:
            if receipt.get("request_digest") != request_digest:
                raise GuiControlMigrationError("idempotency_key_reused")
            stored = _validate_success_receipt(
                receipt, source_digest=source_digest, record_count=len(records),
            )
            with control.connection(write=True) as conn:
                with transaction(conn):
                    missing = _missing_legacy_bindings(conn, records)
                    if not missing:
                        replay = dict(stored)
                        replay["replayed"] = True
                        if "changed" in replay:
                            replay["changed"] = False
                        if "created" in replay:
                            replay["created"] = False
                        return replay

            repair_key = f"legacy-agent-bindings-v1:repair:{source_digest}"
            suffix = 1
            while control.read_receipt("migrate_legacy_agent_bindings", repair_key) is not None:
                suffix += 1
                repair_key = f"legacy-agent-bindings-v1:repair:{source_digest}:{suffix}"
            repair_request = {
                **request,
                "repair_of": "legacy-agent-bindings-v1",
                "missing_binding_ids": [item["binding_id"] for item in missing],
            }

            def apply_repair(conn: Any) -> tuple[Mapping[str, Any], str]:
                result, aggregate = apply(conn)
                repaired = dict(result)
                repaired["repaired"] = True
                return repaired, aggregate

            return dict(control.mutate(
                "migrate_legacy_agent_bindings", repair_key, repair_request, apply_repair,
            ))
        result = control.mutate(
            "migrate_legacy_agent_bindings",
            "legacy-agent-bindings-v1",
            request,
            apply,
        )
    except GuiControlMigrationError:
        raise
    except GroupControlError as exc:
        raise GuiControlMigrationError(exc.code) from exc
    return dict(result)


__all__ = [
    "GuiControlMigrationError", "inspect_legacy_gui_control",
    "migrate_legacy_gui_control",
]
