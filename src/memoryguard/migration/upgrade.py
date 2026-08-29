"""Public, fail-closed V1 -> V2 upgrade orchestration.

The operator-facing ``memoryguard-v2`` command remains available for the
individual lifecycle primitives.  This module is the public product path for
the common upgrade: preview, prepare the V2 shadow, migrate GUI control
metadata, verify the complete ready evidence, and only then optionally
activate after an exact ``V2_ACTIVE`` confirmation.

No legacy runtime adapter is used here.  A missing manifest is the normal
``V1_ACTIVE`` starting point and is handled without creating anything during a
preview.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from .. import host_hooks as _host_hooks
from ..cutover_v2.evidence_assembler import ReadinessEvidenceAssembler
from ..cutover_v2.facade import get_v2_runtime_facade
from ..data_home import resolve_data_home
from ..evidence.store import EvidenceStore
from ..governance_v2 import GovernanceV2
from ..host_hooks import HostHookManager
from ..migration.v2_validator import V2MigrationValidator
from ..memory.store import MemoryAtomStore
from ..runtime_v2.group_native import (
    GroupControlService,
    SystemControlStore,
    _group_kind,
)
from ..runtime_v2.phase4_acceptance import phase4_acceptance_evidence
from ..storage.database import open_database
from ..storage.transaction import transaction
from ..system.manifest import ManifestManager, ManifestState
from .gui_control import (
    GuiControlMigrationError,
    _canonical,
    _load_legacy_bindings,
    _missing_legacy_bindings,
    inspect_legacy_gui_control,
    migrate_legacy_gui_control,
)
from .backup_cleanup import cleanup_migration_backups
from .ready_prepare import prepare_v2_ready
from .workspace_prepare import prepare_v2_workspace, verify_v2_source_snapshot


SCHEMA = "memoryguard-public-upgrade-1"
CONFIRM_ACTIVE = "V2_ACTIVE"
_STAGE_NAMES = ("preflight", "prepare", "gui_control", "verify", "activate")


def _stage(
    status: str = "NOT_RUN",
    *,
    ok: bool = False,
    writes_performed: bool = False,
    code: str = "",
    detail: Any = None,
) -> dict[str, Any]:
    return {
        "status": str(status),
        "ok": bool(ok),
        "writes_performed": bool(writes_performed),
        "code": str(code or ""),
        "detail": detail if detail is not None else {},
    }


def _stages() -> dict[str, dict[str, Any]]:
    return {name: _stage() for name in _STAGE_NAMES}


def _manifest_summary(manager: ManifestManager, current: Any) -> dict[str, Any]:
    return {
        "exists": bool(manager.exists()),
        "state": str(getattr(getattr(current, "state", None), "value", "UNKNOWN")),
        "generation": getattr(current, "generation", None),
        "migration_id": str(getattr(current, "migration_id", "") or ""),
    }


def _state_value(current: Any) -> str:
    return str(getattr(getattr(current, "state", None), "value", "UNKNOWN"))


def _error_code(exc: BaseException, default: str = "upgrade_failed") -> str:
    explicit = str(getattr(exc, "code", "") or "").strip()
    if explicit:
        return explicit
    text = str(exc).casefold()
    if "generation conflict" in text:
        return "manifest_generation_conflict"
    if "snapshot" in text and "missing" in text:
        return "v2_snapshot_missing"
    if "source drift" in text or "drift" in text:
        return "v2_source_drift"
    if "symlink" in text or "reparse" in text:
        return "unsafe_path"
    if "manifest" in text and ("missing" in text or "unread" in text or "invalid" in text):
        return "v2_manifest_unavailable"
    return default


def _next_step(kind: str) -> str:
    if kind == "preview":
        return "review the zero-write plan, then rerun with --apply"
    if kind == "ready":
        return "rerun with --apply --confirm V2_ACTIVE to explicitly activate"
    if kind == "resume":
        return "fix the reported issue, then rerun with --apply; the V2_READY batch is resumable"
    if kind == "active":
        return "none; V2 is already active"
    if kind == "confirmation":
        return "rerun with the exact confirmation word V2_ACTIVE"
    return "inspect the stage detail, fix the issue, and rerun the upgrade"


def _envelope(
    *,
    workspace: Path,
    data_home: Path,
    apply: bool,
    current: Any,
    stages: Mapping[str, Any],
    status: str,
    ok: bool,
    stage: str,
    code: str = "",
    next_step: str = "",
    writes_performed: bool = False,
    activation_required: bool = False,
    detail: Any = None,
) -> dict[str, Any]:
    state = _state_value(current)
    payload = {
        "schema": SCHEMA,
        "command": "upgrade",
        "from_version": "0.6.2",
        "to_runtime": "v2",
        "status": str(status),
        "ok": bool(ok),
        "stage": str(stage),
        "phase": str(stage),
        "code": str(code or ""),
        "next_step": str(next_step or ""),
        "workspace": str(workspace),
        "data_home": str(data_home),
        "workspace_equals_data_home": workspace == data_home,
        "state": state,
        "generation": getattr(current, "generation", None),
        "migration_id": str(getattr(current, "migration_id", "") or ""),
        "apply": bool(apply),
        "writes_performed": bool(writes_performed),
        "activation_required": bool(activation_required),
        "v2_active": state == ManifestState.V2_ACTIVE.value,
        "stages": {name: dict(stages.get(name, _stage())) for name in _STAGE_NAMES},
        "detail": detail if detail is not None else {},
    }
    cleanup = detail.get("cleanup") if isinstance(detail, Mapping) else None
    if isinstance(cleanup, Mapping):
        payload["cleanup"] = dict(cleanup)
        payload["cleanup_warning"] = bool(cleanup.get("cleanup_warning"))
        payload["cleanup_remaining"] = list(cleanup.get("remaining") or [])
    return payload


def _legacy_binding_records(workspace: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for directory in ("agent-bindings", "agent_bindings"):
        root = workspace / ".memoryguard" / directory
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                continue
            binding_id = str(raw.get("binding_id") or "").strip()
            agent_id = str(raw.get("agent_instance_id") or "").strip()
            group_id = str(raw.get("share_group_id") or "").strip()
            identity = binding_id or f"{agent_id}:{group_id}"
            if not identity or identity in seen:
                continue
            seen.add(identity)
            records.append({
                "binding_id": binding_id,
                "agent_instance_id": agent_id,
                "share_group_id": group_id,
                "status": str(raw.get("status") or "").strip(),
            })
    return records


def _legacy_binding_ids(workspace: Path) -> set[str]:
    return {
        item["binding_id"]
        for item in _legacy_binding_records(workspace)
        if item.get("binding_id")
    }


def _remove_legacy_hook_fragments(
    workspace: Path,
    bindings: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Remove pre-share-group generated Hook commands for migrated bindings.

    Older Codex registrations predate ``--share-group-id``.  The normal
    HostHookManager cleanup intentionally rejects such incomplete identities,
    so upgrade owns this narrow compatibility pass.  Only a generated command
    bound to one migrated Agent and the exact legacy workspace is removed;
    current V2 commands and user-owned handlers remain untouched.
    """

    targets = {
        str(item.get("agent_instance_id") or "").strip(): str(
            item.get("share_group_id") or ""
        ).strip()
        for item in bindings
        if str(item.get("agent_instance_id") or "").strip()
        and str(item.get("share_group_id") or "").strip()
    }
    result: dict[str, Any] = {
        "binding_count": 0,
        "handler_count": 0,
        "bindings": [],
    }
    if not targets:
        return result

    root = Path(workspace).expanduser().resolve()
    removed: dict[tuple[str, str, str], int] = {}
    adapters = (
        ("claude", _host_hooks.ClaudeHookAdapter),
        ("codex", _host_hooks.CodexHookAdapter),
        ("cursor", _host_hooks.CursorHookAdapter),
    )

    def is_legacy_handler(handler: Any, provider: str) -> tuple[str, str] | None:
        if not _host_hooks._is_our_handler(handler):
            return None
        if not isinstance(handler, Mapping):
            return None
        for key in ("command", "commandWindows"):
            command = str(handler.get(key) or "")
            if not command:
                continue
            if _host_hooks._command_option(command, "--managed-by") != "memoryguard":
                continue
            if _host_hooks._command_option(command, "--provider").lower() != provider:
                continue
            agent_id = _host_hooks._command_option(command, "--agent-id")
            if agent_id not in targets:
                continue
            bound_workspace = _host_hooks._command_option(command, "--workspace")
            try:
                same_workspace = (
                    Path(bound_workspace).expanduser().resolve() == root
                )
            except (OSError, RuntimeError, ValueError):
                same_workspace = False
            if not same_workspace:
                continue
            # Current V2 commands have a complete binding identity and are
            # handled by HostHookManager.  Missing group identity marks old
            # provider registration, which is the only compatibility target.
            if _host_hooks._command_option(command, "--share-group-id"):
                return None
            return agent_id, targets[agent_id]
        return None

    def filter_config(data: dict[str, Any], provider: str) -> list[tuple[str, str]]:
        hooks = data.get("hooks")
        if not isinstance(hooks, dict):
            return []
        found: list[tuple[str, str]] = []
        for event_name in list(hooks):
            entries = hooks.get(event_name)
            if not isinstance(entries, list):
                continue
            kept_entries: list[Any] = []
            for entry in entries:
                direct = is_legacy_handler(entry, provider)
                if direct is not None:
                    found.append((direct[0], direct[1]))
                    continue
                if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                    kept_entries.append(entry)
                    continue
                kept_handlers: list[Any] = []
                for handler in entry["hooks"]:
                    target = is_legacy_handler(handler, provider)
                    if target is None:
                        kept_handlers.append(handler)
                    else:
                        found.append((target[0], target[1]))
                if kept_handlers:
                    copied = dict(entry)
                    copied["hooks"] = kept_handlers
                    kept_entries.append(copied)
            if kept_entries:
                hooks[event_name] = kept_entries
            else:
                hooks.pop(event_name, None)
        if not hooks:
            data.pop("hooks", None)
        return found

    snapshots: list[tuple[Path, dict[str, Any]]] = []
    try:
        for provider, adapter_cls in adapters:
            adapter = adapter_cls(root)
            path = adapter.config_path()
            data = _host_hooks._load_json_config(path, strict=True)
            original = json.loads(json.dumps(data))
            found = filter_config(data, provider)
            if not found:
                continue
            snapshots.append((path, original))
            _host_hooks._write_json_config(path, data)
            for agent_id, group_id in found:
                key = (provider, agent_id, group_id)
                removed[key] = removed.get(key, 0) + 1
    except Exception:
        for path, data in reversed(snapshots):
            _host_hooks._write_json_config(path, data)
        raise

    result["bindings"] = [
        {
            "provider": provider,
            "agent_instance_id": agent_id,
            "share_group_id": group_id,
            "handler_count": count,
        }
        for (provider, agent_id, group_id), count in sorted(removed.items())
    ]
    result["binding_count"] = len(result["bindings"])
    result["handler_count"] = sum(
        int(item["handler_count"]) for item in result["bindings"]
    )
    return result


def _merge_hook_cleanup(
    primary: Mapping[str, Any],
    compatibility: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge exact current and legacy Hook cleanup receipts."""

    merged: dict[tuple[str, str, str], int] = {}
    for source in (primary, compatibility):
        for item in source.get("bindings", []) or []:
            if not isinstance(item, Mapping):
                continue
            key = (
                str(item.get("provider") or ""),
                str(item.get("agent_instance_id") or ""),
                str(item.get("share_group_id") or ""),
            )
            merged[key] = merged.get(key, 0) + int(item.get("handler_count") or 0)
    bindings = [
        {
            "provider": provider,
            "agent_instance_id": agent_id,
            "share_group_id": group_id,
            "handler_count": count,
        }
        for (provider, agent_id, group_id), count in sorted(merged.items())
    ]
    return {
        "binding_count": len(bindings),
        "handler_count": sum(item["handler_count"] for item in bindings),
        "bindings": bindings,
    }


def _binding_recovery_records(workspace: Path) -> tuple[list[dict[str, Any]], str]:
    """Read immutable migrated binding evidence retained in the V2 manifest."""

    try:
        checkpoints = ManifestManager(workspace).current().checkpoints
    except Exception:
        return [], ""
    raw = checkpoints.get("legacy_binding_recovery", {}) if isinstance(checkpoints, Mapping) else {}
    metadata = raw.get("metadata", {}) if isinstance(raw, Mapping) else {}
    if not isinstance(metadata, Mapping):
        return [], ""
    try:
        decoded = json.loads(str(metadata.get("records_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [], ""
    if not isinstance(decoded, list):
        return [], ""
    records = [dict(item) for item in decoded if isinstance(item, Mapping)]
    return records, str(metadata.get("source_digest") or "")


def _migrate_gui_control_to_target(
    source_workspace: Path,
    target_workspace: Path,
    *,
    records: list[dict[str, Any]] | None = None,
    source_digest: str = "",
) -> dict[str, Any]:
    """Migrate V1 binding metadata from source into separate V2 target.

    ``migration.gui_control`` historically read and wrote one workspace.  The
    public single-plane upgrade can now have a project V1 source and user V2
    target, so reuse its validation helpers while keeping target writes in the
    target system database.
    """

    if records is None:
        records, source_digest = _load_legacy_bindings(source_workspace)
    else:
        records = [dict(record) for record in records]
        source_digest = str(source_digest or _canonical(records))
    control = SystemControlStore(target_workspace, write=True)
    request = {
        "source": "legacy_agent_bindings_json",
        "source_digest": source_digest,
        "record_count": len(records),
    }

    def apply(conn: Any) -> tuple[Mapping[str, Any], str]:
        missing = _missing_legacy_bindings(conn, records)
        migrated = 0
        active = 0
        inactive = 0
        for record in missing:
            if record["status"] == "active":
                conflicting = conn.execute(
                    "SELECT binding_id,share_group_id FROM agent_group_bindings "
                    "WHERE agent_instance_id=? AND status='active'",
                    (record["agent_instance_id"],),
                ).fetchone()
                if conflicting is not None:
                    raise GuiControlMigrationError("v2_active_binding_conflict")
            created_at = (
                record["bound_at"]
                or record["last_drift_check"]
                or "1970-01-01T00:00:00+00:00"
            )
            updated_at = record["last_drift_check"] or created_at
            conn.execute(
                "INSERT INTO agent_group_bindings(binding_id,agent_instance_id,"
                "share_group_id,group_kind,mcp_server_name,native_memory_mode,"
                "redirect_paths_json,status,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,1,?,?) "
                "ON CONFLICT(binding_id) DO UPDATE SET "
                "agent_instance_id=excluded.agent_instance_id,"
                "share_group_id=excluded.share_group_id,"
                "group_kind=excluded.group_kind,"
                "mcp_server_name=excluded.mcp_server_name,"
                "native_memory_mode=excluded.native_memory_mode,"
                "redirect_paths_json=excluded.redirect_paths_json,"
                "status=excluded.status,"
                "updated_at=excluded.updated_at",
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
        return {
            "ok": True,
            "status": "succeeded",
            "source": "legacy_agent_bindings_json",
            "source_digest": source_digest,
            "record_count": len(records),
            "migrated_count": migrated,
            "active_count": active,
            "inactive_count": inactive,
            "changed": migrated > 0,
        }, "gui-control-migration"

    return dict(control.mutate(
        "migrate_legacy_agent_bindings",
        "legacy-agent-bindings-v1",
        request,
        apply,
    ))


def _migrate_gui_control(
    source_workspace: Path,
    target_workspace: Path,
) -> dict[str, Any]:
    recovered: list[dict[str, Any]] | None = None
    recovered_digest = ""
    try:
        source_records, _source_digest = _load_legacy_bindings(source_workspace)
        if not source_records:
            recovered, recovered_digest = _binding_recovery_records(target_workspace)
            if not recovered:
                recovered = None
    except Exception:
        # Keep malformed present source files on the strict validation path.
        recovered = None
    if source_workspace == target_workspace:
        return migrate_legacy_gui_control(
            target_workspace,
            records=recovered,
            source_digest=recovered_digest or None,
        )
    return _migrate_gui_control_to_target(
        source_workspace,
        target_workspace,
        records=recovered,
        source_digest=recovered_digest,
    )


def _control_binding_health(
    workspace: Path,
    *,
    source_workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Compare frozen V1 Agent bindings with authoritative V2 membership."""

    source = (
        Path(source_workspace).expanduser().resolve()
        if source_workspace is not None
        else workspace
    )
    legacy_ids = _legacy_binding_ids(source)
    if not legacy_ids:
        recovered, _digest = _binding_recovery_records(workspace)
        legacy_ids = {
            str(item.get("binding_id") or "")
            for item in recovered
            if str(item.get("binding_id") or "")
        }
    bindings = GroupControlService(workspace, write=False).list_bindings(include_inactive=True)
    v2_ids = {
        str(item.get("binding_id") or "")
        for item in bindings.get("bindings", [])
        if isinstance(item, Mapping)
    }
    missing = sorted(legacy_ids - v2_ids)
    return {
        "legacy_binding_count": len(legacy_ids),
        "v2_binding_count": len(v2_ids),
        "missing_binding_ids": missing,
        "status": "PASS" if not missing else "BLOCKED",
    }


def _memory_activation_health(workspace: Path) -> dict[str, Any]:
    """Report whether the migrated Memory domain is actually runtime-visible."""

    store = MemoryAtomStore(workspace, readonly=True)
    with store._connection() as conn:
        state = conn.execute(
            "SELECT state,generation FROM domain_state WHERE domain='memory'"
        ).fetchone()
        counts = conn.execute(
            "SELECT visibility,COUNT(*) FROM atoms GROUP BY visibility ORDER BY visibility"
        ).fetchall()
    visibility = {str(row[0]): int(row[1]) for row in counts}
    domain_state = str(state[0] if state is not None else "").upper()
    total = sum(visibility.values())
    healthy = total == 0 or (
        domain_state == "ACTIVE"
        and not visibility.get("building", 0)
        and not visibility.get("ready", 0)
    )
    return {
        "status": "PASS" if healthy else "BLOCKED",
        "domain_state": domain_state or "MISSING",
        "generation": int(state[1] if state is not None else 0),
        "visibility_counts": visibility,
    }


def _activate_memory_domain(workspace: Path) -> dict[str, Any]:
    """Expose the verified migration batch and prove the resulting domain state."""

    before = _memory_activation_health(workspace)
    changed = MemoryAtomStore(workspace).set_visibility("active")
    after = _memory_activation_health(workspace)
    if after["status"] != "PASS":
        raise RuntimeError("memory_domain_activation_incomplete")
    return {"before": before, "after": after, "changed": int(changed)}


_GOVERNANCE_LEDGER_TABLES = frozenset({"decisions", "request_ledger", "decision_outbox"})
_GOVERNANCE_DECISION_COLUMNS = frozenset({
    "decision_id", "operation", "target_json", "reason", "confidence", "undo_hash",
    "context_json", "before_json", "after_json", "status", "created_at",
    "idempotency_key", "request_fingerprint",
})


def _governance_activation_health(workspace: Path) -> dict[str, Any]:
    """Verify the GovernanceV2 ledger required by every native write path."""

    ledger = workspace / ".memoryguard" / "governance_v2" / "decisions.db"
    if not ledger.is_file() or ledger.stat().st_size == 0:
        return {
            "status": "BLOCKED",
            "code": "v2_governance_ledger_missing",
            "ledger_present": False,
            "missing_tables": sorted(_GOVERNANCE_LEDGER_TABLES),
            "missing_decision_columns": sorted(_GOVERNANCE_DECISION_COLUMNS),
        }
    try:
        # ``sqlite3.Connection.__exit__`` commits/rolls back but does not close
        # the file handle.  Use the project context manager so Windows upgrade
        # flows never leave decisions.db locked after a health probe.
        with open_database(ledger, readonly=True) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(decisions)")
            }
    except Exception:
        return {
            "status": "BLOCKED",
            "code": "v2_governance_ledger_invalid",
            "ledger_present": True,
            "missing_tables": [],
            "missing_decision_columns": [],
        }
    missing_tables = sorted(_GOVERNANCE_LEDGER_TABLES - tables)
    missing_columns = sorted(_GOVERNANCE_DECISION_COLUMNS - columns)
    healthy = not missing_tables and not missing_columns
    return {
        "status": "PASS" if healthy else "BLOCKED",
        "code": "" if healthy else "v2_governance_ledger_partial",
        "ledger_present": True,
        "missing_tables": missing_tables,
        "missing_decision_columns": missing_columns,
    }


def _activate_governance_domain(workspace: Path) -> dict[str, Any]:
    """Create/upgrade the durable GovernanceV2 ledger, then prove it is usable."""

    before = _governance_activation_health(workspace)
    GovernanceV2(
        workspace,
        memory_store=MemoryAtomStore(workspace, readonly=False),
        evidence_store=EvidenceStore(workspace, readonly=False),
    )
    after = _governance_activation_health(workspace)
    if after["status"] != "PASS":
        raise RuntimeError(str(after.get("code") or "governance_ledger_activation_incomplete"))
    return {"before": before, "after": after, "changed": before["status"] != "PASS"}


_LEGACY_RUNTIME_DIRS = (
    "shared-memory", "shared_memory",
    "managed-memory", "managed_memory",
    "agent-bindings", "agent_bindings",
)


def _migrated_legacy_paths(
    target_workspace: Path,
    source_workspace: Path,
    bindings: list[Mapping[str, Any]],
) -> list[Path]:
    """Return only source files consumed by the verified V2 migration."""

    paths: set[Path] = set()
    source_memory_roots = [
        source_workspace / ".memoryguard" / name
        for name in ("shared-memory", "shared_memory")
    ]
    for root in source_memory_roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            candidate = child / "memory.db" if child.is_dir() else child
            if (
                candidate.is_file()
                and candidate.suffix.casefold() in {".db", ".sqlite", ".sqlite3"}
            ):
                paths.add(candidate)

    managed_roots = [
        source_workspace / ".memoryguard" / name
        for name in ("managed-memory", "managed_memory")
    ]
    for root in managed_roots:
        if not root.is_dir():
            continue
        for agent_dir in root.iterdir():
            if not agent_dir.is_dir():
                continue
            active = agent_dir / "active.json"
            if active.is_file():
                paths.add(active)
            versions = agent_dir / "versions"
            if not versions.is_dir():
                continue
            for records in versions.glob("*/records.jsonl"):
                if records.is_file():
                    paths.add(records)

    migrated_binding_ids: set[str] = set()
    try:
        migrated_binding_ids = {
            str(item.get("binding_id") or "")
            for item in GroupControlService(
                target_workspace, write=False,
            ).list_bindings(include_inactive=True).get("bindings", [])
            if isinstance(item, Mapping) and str(item.get("binding_id") or "")
        }
    except Exception:
        # Do not delete binding files when target membership cannot be proved.
        migrated_binding_ids = set()
    for directory in ("agent-bindings", "agent_bindings"):
        root = source_workspace / ".memoryguard" / directory
        if not root.is_dir():
            continue
        for path in root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(raw, Mapping) and str(raw.get("binding_id") or "") in migrated_binding_ids:
                paths.add(path)

    return sorted(paths, key=lambda path: str(path).casefold())


def _prune_empty_legacy_parents(paths: list[Path], source_workspace: Path) -> list[Path]:
    """Remove only now-empty legacy containers, never non-empty data roots."""

    roots = {
        (source_workspace / ".memoryguard" / name).resolve()
        for name in _LEGACY_RUNTIME_DIRS
    }
    candidates: set[Path] = set()
    for path in paths:
        current = path.parent.resolve()
        while current in roots or any(root in current.parents for root in roots):
            candidates.add(current)
            if current in roots:
                break
            current = current.parent
    removed: list[Path] = []
    for directory in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except (FileNotFoundError, OSError):
            continue
        removed.append(directory)
    return removed


def _cleanup_active_migration(
    workspace: Path,
    current: Any,
    *,
    apply: bool,
    source_workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Retire verified V1 runtime artifacts after V2 activation.

    Activation is authoritative and is never rolled back because cleanup has
    debt.  The receipt therefore reports partial failures as warnings while
    keeping the already-verified V2 state intact.  Re-running is idempotent.
    """

    migration_id = str(getattr(current, "migration_id", "") or "")
    source_root = (
        Path(source_workspace).expanduser().resolve()
        if source_workspace is not None
        else workspace
    )
    bindings = _legacy_binding_records(source_root)
    legacy_files = _migrated_legacy_paths(workspace, source_root, bindings)
    result: dict[str, Any] = {
        "status": "PASS",
        "ok": True,
        "cleanup_warning": False,
        "migration_id": migration_id,
        "removed": False,
        "removed_legacy_paths": [],
        "remaining": [],
        "errors": [],
        "hook_cleanup": {"binding_count": 0, "handler_count": 0, "bindings": []},
    }
    try:
        backup = cleanup_migration_backups(workspace, migration_id, dry_run=not apply)
        result["backup_cleanup"] = backup
        if not bool(backup.get("ok", True)):
            result["cleanup_warning"] = True
            result["status"] = "WARNING"
            result["ok"] = False
            result["errors"].extend(list(backup.get("errors") or []))
            result["remaining"].extend(list(backup.get("remaining") or []))
        else:
            result["status"] = str(backup.get("status") or "PASS")
    except Exception as exc:
        result.update({"status": "WARNING", "ok": False, "cleanup_warning": True})
        result["errors"].append({
            "path": migration_id or "<migration-backup>",
            "error": f"{type(exc).__name__}: {exc}",
        })

    if not apply:
        result["planned_legacy_paths"] = [str(path) for path in legacy_files]
        result["planned_hook_bindings"] = len(bindings)
        return result

    primary_hook_cleanup: Mapping[str, Any] = {
        "binding_count": 0,
        "handler_count": 0,
        "bindings": [],
    }
    try:
        hook_cleanup = HostHookManager(source_root).retire_legacy_generated_bindings(
            bindings,
            active_workspace=workspace,
        )
        primary_hook_cleanup = hook_cleanup.public_result()
    except Exception as exc:
        result.update({"status": "WARNING", "ok": False, "cleanup_warning": True})
        result["errors"].append({
            "path": "host_hooks",
            "error": f"{type(exc).__name__}: {exc}",
        })
    try:
        compatibility_hook_cleanup = _remove_legacy_hook_fragments(
            source_root, bindings,
        )
    except Exception as exc:
        compatibility_hook_cleanup = {
            "binding_count": 0,
            "handler_count": 0,
            "bindings": [],
        }
        result.update({"status": "WARNING", "ok": False, "cleanup_warning": True})
        result["errors"].append({
            "path": "host_hooks.legacy",
            "error": f"{type(exc).__name__}: {exc}",
        })
    result["hook_cleanup"] = _merge_hook_cleanup(
        primary_hook_cleanup, compatibility_hook_cleanup,
    )

    removed_files: list[Path] = []
    for path in legacy_files:
        try:
            path.unlink(missing_ok=True)
            removed_files.append(path)
            result["removed_legacy_paths"].append(str(path))
        except Exception as exc:
            result.update({"status": "WARNING", "ok": False, "cleanup_warning": True})
            result["remaining"].append(str(path))
            result["errors"].append({
                "path": str(path),
                "error": f"{type(exc).__name__}: {exc}",
            })
    for directory in _prune_empty_legacy_parents(removed_files, source_root):
        result["removed_legacy_paths"].append(str(directory))
    result["removed"] = bool(
        result["removed_legacy_paths"]
        or result["hook_cleanup"].get("handler_count")
        or result.get("backup_cleanup", {}).get("removed")
    )
    return result


def _verify_ready(
    workspace: Path,
    data_home: Path,
    manager: ManifestManager,
    control_preview: Mapping[str, Any],
    *,
    source_workspace: str | Path | None = None,
) -> dict[str, Any]:
    """Re-run the production readiness evidence after GUI control migration."""

    current = manager.current()
    if current.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "verification_requires_v2_ready",
            "state": _state_value(current),
        }

    binding_health = _control_binding_health(
        workspace, source_workspace=source_workspace,
    )
    missing_bindings = list(binding_health["missing_binding_ids"])
    control = {
        "legacy_record_count": int(control_preview.get("record_count") or 0),
        "v2_binding_count": int(binding_health["v2_binding_count"]),
        "missing_binding_ids": missing_bindings,
        "status": "PASS" if not missing_bindings else "BLOCKED",
    }
    if missing_bindings:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "gui_control_verification_failed",
            "control": control,
        }

    # A V2_READY batch may have been produced by the hidden operator command
    # before this public flow was introduced.  Its readiness digest is already
    # immutable, so re-running the full assembler after adding GUI control
    # metadata would report a legitimate digest change rather than a resumable
    # migration.  Re-verify the frozen live source, V2 target validator and
    # migrated control records without attempting to rewrite READY evidence.
    if current.state is ManifestState.V2_READY:
        phase2 = current.checkpoints.get("phase2_sources", {}) if isinstance(current.checkpoints, Mapping) else {}
        snapshot = phase2.get("snapshot", {}) if isinstance(phase2, Mapping) else {}
        if not isinstance(snapshot, Mapping) or str(snapshot.get("mode") or "") != "frozen":
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "v2_snapshot_missing",
                "control": control,
            }
        try:
            source_verification = verify_v2_source_snapshot(
                workspace,
                data_home=data_home,
                migration_id=current.migration_id,
            )
            source_workspace = str(snapshot.get("workspace") or "")
            raw_source_data_home = str(snapshot.get("data_home") or "")
            validator = V2MigrationValidator(
                workspace,
                data_home=data_home,
                migration_id=current.migration_id,
                expected_source_hashes=(phase2.get("hashes", {}) if isinstance(phase2, Mapping) else {}),
                source_workspace=source_workspace,
                source_data_home=(None if raw_source_data_home in {"", "NOT_CONFIGURED"} else raw_source_data_home),
            )
            validation = validator.validate(migration_id=current.migration_id).to_dict()
        except Exception as exc:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": _error_code(exc, "v2_ready_verification_failed"),
                "control": control,
                "error": str(exc),
            }
        if source_verification.get("activation_safe") is not True or validation.get("ok") is not True:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "v2_ready_verification_failed",
                "control": control,
                "source_verification": source_verification,
                "validation": validation,
            }
        return {
            "ok": True,
            "status": "PASS",
            "code": "",
            "verification_mode": "resumed_v2_ready",
            "control": control,
            "source_verification": source_verification,
            "validation": validation,
        }

    phase4 = phase4_acceptance_evidence()
    if phase4.get("ok") is not True:
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": "phase4_acceptance_failed",
            "control": control,
            "phase4": phase4,
        }

    try:
        native = get_v2_runtime_facade(str(workspace)).ports.v2
        assembly = ReadinessEvidenceAssembler(
            workspace,
            data_home=data_home,
            phase4_evidence=phase4,
            native_coverage=native,
            manifest_manager=manager,
            require_frozen_sources=True,
        ).assemble()
        readiness = assembly.to_public_dict()
    except Exception as exc:  # stable public error; no traceback in CLI output
        return {
            "ok": False,
            "status": "BLOCKED",
            "code": _error_code(exc, "v2_readiness_verification_failed"),
            "control": control,
            "error": str(exc),
        }

    ready = bool(getattr(assembly, "ready", False))
    blockers = readiness.get("blockers", []) if isinstance(readiness, Mapping) else []
    if ready and current.state is ManifestState.V2_BUILDING:
        payload = dict(getattr(assembly, "transition_payload", {}) or {})
        if not payload:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": "missing_ready_transition_payload",
                "control": control,
                "phase4": phase4,
                "readiness": readiness,
            }
        try:
            marked_ready = manager.mark_v2_ready(**payload)
        except Exception as exc:
            return {
                "ok": False,
                "status": "BLOCKED",
                "code": _error_code(exc, "v2_ready_transition_failed"),
                "control": control,
                "phase4": phase4,
                "readiness": readiness,
                "error": str(exc),
            }
        readiness = dict(readiness)
        readiness["manifest_state"] = marked_ready.state.value
        readiness["generation"] = marked_ready.generation
    return {
        "ok": ready,
        "status": "PASS" if ready else "BLOCKED",
        "code": "" if ready else "v2_readiness_verification_failed",
        "control": control,
        "phase4": phase4,
        "readiness": readiness,
        "blockers": blockers,
    }


def _project_gui_control_outbox(workspace: Path) -> dict[str, Any]:
    """Advance the system projection checkpoint for committed GUI receipts."""

    store = SystemControlStore(workspace, write=False)
    with store.connection(write=True) as conn:
        with transaction(conn):
            row = conn.execute("SELECT MAX(sequence) FROM group_outbox").fetchone()
            maximum = 0 if row is None or row[0] is None else int(row[0])
            if maximum:
                conn.execute(
                    "UPDATE outbox_checkpoints SET last_sequence=?,updated_at=? "
                    "WHERE domain='system' AND last_sequence<?",
                    (maximum, "upgrade", maximum),
                )
    return {"status": "PASS", "max_sequence": maximum}


def _record_binding_recovery_checkpoint(
    manager: ManifestManager,
    migration_id: str,
    source_workspace: Path,
) -> None:
    """Persist binding migration inputs before activation retires V1 files."""

    current = manager.current()
    if current.state is not ManifestState.V2_BUILDING:
        return
    records, source_digest = _load_legacy_bindings(source_workspace)
    if not records:
        return
    manager.record_checkpoint(
        {
            "legacy_binding_recovery": {
                "metadata": {
                    "source": "legacy_agent_bindings_json",
                    "source_digest": source_digest,
                    # Keep migration inputs opaque to the generic JSON
                    # reference auditor; these are recovery metadata, not
                    # live foreign keys in another V2 plane.
                    "records_json": _canonical(records),
                },
            }
        },
        migration_id=migration_id,
    )


def run_upgrade(
    workspace: str | Path = ".",
    *,
    data_home: str | Path | None = None,
    apply: bool = False,
    confirm: str | None = None,
    migration_id: str | None = None,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Run the public upgrade flow and return a stable JSON-compatible report."""

    root = Path(workspace).expanduser().resolve()
    resolved_data_home = (
        Path(data_home).expanduser().resolve()
        if data_home is not None
        else resolve_data_home()
    )
    # ``workspace`` is V2 target; explicit ``data_home`` is V1 source for the
    # public upgrade path.  Legacy source cleanup/GUI control checks must use
    # that source while all manifests and runtime stores stay at ``root``.
    source_root = (
        resolved_data_home if data_home is not None else root
    )
    stages = _stages()
    manager = ManifestManager(root)

    try:
        current = manager.current(immutable=not apply)
    except Exception as exc:
        unknown = type("UnknownState", (), {"state": "UNKNOWN", "generation": None, "migration_id": ""})()
        stages["preflight"] = _stage(
            "BLOCKED", code=_error_code(exc, "v2_manifest_unavailable"), detail={"error": str(exc)}
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=unknown,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code=_error_code(exc, "v2_manifest_unavailable"),
            next_step=_next_step("error"),
            detail={"error": str(exc)},
        )

    state = current.state
    if state is ManifestState.V2_ACTIVE:
        try:
            control_preview = inspect_legacy_gui_control(source_root)
            control_health = _control_binding_health(
                root, source_workspace=source_root,
            )
            memory_health = _memory_activation_health(root)
            governance_health = _governance_activation_health(root)
        except Exception as exc:
            code = _error_code(exc, "active_control_preflight_failed")
            stages["preflight"] = _stage("BLOCKED", code=code, detail={"error": str(exc)})
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=apply,
                current=current, stages=stages, status="BLOCKED", ok=False,
                stage="preflight", code=code, next_step=_next_step("error"),
                detail={"error": str(exc)},
            )
        missing = list(control_health["missing_binding_ids"])
        memory_repair_required = memory_health.get("status") != "PASS"
        governance_repair_required = governance_health.get("status") != "PASS"
        if missing and not apply:
            stages["preflight"] = _stage(
                "BLOCKED", code="active_control_repair_required", detail=control_health
            )
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=False,
                current=current, stages=stages, status="BLOCKED", ok=False,
                stage="preflight", code="active_control_repair_required",
                next_step="rerun with --apply to restore migrated Agent bindings",
                detail=control_health,
            )
        if governance_repair_required and not apply:
            code = "active_runtime_repair_required" if memory_repair_required else "active_governance_repair_required"
            detail = {"governance": governance_health, "memory": memory_health}
            stages["preflight"] = _stage("BLOCKED", code=code, detail=detail)
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=False,
                current=current, stages=stages, status="BLOCKED", ok=False,
                stage="preflight", code=code,
                next_step="rerun memoryguard upgrade with --apply to repair the active V2 runtime",
                detail=detail,
            )
        if memory_repair_required and not apply:
            stages["preflight"] = _stage(
                "BLOCKED", code="active_memory_repair_required", detail=memory_health
            )
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=False,
                current=current, stages=stages, status="BLOCKED", ok=False,
                stage="preflight", code="active_memory_repair_required",
                next_step="rerun memoryguard upgrade to activate migrated memory",
                detail=memory_health,
            )
        governance_repair: dict[str, Any] | None = None
        if governance_repair_required:
            try:
                governance_repair = _activate_governance_domain(root)
                governance_health = dict(governance_repair["after"])
            except Exception as exc:
                code = _error_code(exc, "active_governance_repair_failed")
                stages["verify"] = _stage(
                    "BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)}
                )
                return _envelope(
                    workspace=root, data_home=resolved_data_home, apply=True,
                    current=current, stages=stages, status="BLOCKED", ok=False,
                    stage="verify", code=code, next_step=_next_step("error"),
                    writes_performed=True, detail={"error": str(exc)},
                )
        memory_repair: dict[str, Any] | None = None
        if memory_repair_required:
            try:
                memory_repair = _activate_memory_domain(root)
                memory_health = dict(memory_repair["after"])
            except Exception as exc:
                code = _error_code(exc, "active_memory_repair_failed")
                stages["verify"] = _stage(
                    "BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)}
                )
                return _envelope(
                    workspace=root, data_home=resolved_data_home, apply=True,
                    current=current, stages=stages, status="BLOCKED", ok=False,
                    stage="verify", code=code, next_step=_next_step("error"),
                    writes_performed=True, detail={"error": str(exc)},
                )
        if missing:
            stages["preflight"] = _stage("PASS", ok=True, detail=control_health)
            try:
                repaired = _migrate_gui_control(source_root, root)
                repaired["projection"] = _project_gui_control_outbox(root)
                after = _control_binding_health(
                    root, source_workspace=source_root,
                )
                if after["missing_binding_ids"]:
                    raise GuiControlMigrationError("active_control_repair_incomplete")
            except Exception as exc:
                code = _error_code(exc, "active_control_repair_failed")
                stages["gui_control"] = _stage(
                    "BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)}
                )
                return _envelope(
                    workspace=root, data_home=resolved_data_home, apply=True,
                    current=current, stages=stages, status="BLOCKED", ok=False,
                    stage="gui_control", code=code, next_step=_next_step("error"),
                    writes_performed=True, detail={"error": str(exc)},
                )
            stages["gui_control"] = _stage(
                "PASS", ok=True, writes_performed=True, detail=repaired
            )
            stages["verify"] = _stage(
                "PASS", ok=True, writes_performed=bool(memory_repair or governance_repair),
                detail={"control": after, "memory": memory_health, "governance": governance_health},
            )
            cleanup = _cleanup_active_migration(
                root, current, apply=True, source_workspace=source_root,
            )
            stages["activate"] = _stage(
                "IDEMPOTENT", ok=True, code="already_active",
                detail={**_manifest_summary(manager, current), "cleanup": cleanup},
            )
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=True,
                current=current, stages=stages, status=ManifestState.V2_ACTIVE.value,
                ok=True, stage="complete", code=("active_runtime_repaired" if (memory_repair or governance_repair) else "active_control_repaired"),
                next_step=_next_step("active"), writes_performed=True,
                detail={"control": after, "memory": memory_health, "governance": governance_health, "cleanup": cleanup},
            )
        if memory_repair or governance_repair:
            cleanup = _cleanup_active_migration(
                root, current, apply=True, source_workspace=source_root,
            )
            if memory_repair and governance_repair:
                repair_code = "active_runtime_repaired"
            elif governance_repair:
                repair_code = "active_governance_repaired"
            else:
                repair_code = "active_memory_repaired"
            runtime_health = {"memory": memory_health, "governance": governance_health}
            stages["preflight"] = _stage(
                "PASS", ok=True, writes_performed=True,
                code=repair_code, detail=runtime_health,
            )
            stages["verify"] = _stage(
                "PASS", ok=True, writes_performed=True, detail=runtime_health,
            )
            stages["activate"] = _stage(
                "IDEMPOTENT", ok=True, writes_performed=True,
                code="already_active", detail={**_manifest_summary(manager, current), "cleanup": cleanup},
            )
            return _envelope(
                workspace=root, data_home=resolved_data_home, apply=True,
                current=current, stages=stages, status=ManifestState.V2_ACTIVE.value,
                ok=True, stage="complete", code=repair_code,
                next_step=_next_step("active"), writes_performed=True,
                detail={"control": control_health, **runtime_health, "cleanup": cleanup},
            )
        cleanup = _cleanup_active_migration(
            root, current, apply=apply, source_workspace=source_root,
        )
        stages["preflight"] = _stage(
            "PASS", ok=True, code="already_active",
            detail={**_manifest_summary(manager, current), "control": control_health, "memory": memory_health, "governance": governance_health},
        )
        stages["activate"] = _stage(
            "IDEMPOTENT", ok=True, code="already_active",
            detail={**_manifest_summary(manager, current), "cleanup": cleanup},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status=ManifestState.V2_ACTIVE.value,
            ok=True,
            stage="complete",
            code="already_active",
            next_step=_next_step("active"),
            activation_required=False,
            detail={"control": control_health, "memory": memory_health, "governance": governance_health, "cleanup": cleanup},
        )

    if confirm is not None and not apply:
        stages["preflight"] = _stage(
            "BLOCKED", code="confirmation_requires_apply", detail={"confirm_supplied": True}
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=False,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="confirmation_requires_apply",
            next_step=_next_step("preview"),
        )
    if expected_generation is not None and current.generation != expected_generation:
        stages["preflight"] = _stage(
            "BLOCKED",
            code="manifest_generation_conflict",
            detail={"expected_generation": expected_generation, "current_generation": current.generation},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="manifest_generation_conflict",
            next_step=_next_step("error"),
        )
    if confirm is not None and confirm != CONFIRM_ACTIVE:
        stages["preflight"] = _stage(
            "BLOCKED",
            code="activation_confirmation_mismatch",
            detail={"required": CONFIRM_ACTIVE},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="activation_confirmation_mismatch",
            next_step=_next_step("confirmation"),
        )

    # The preflight is deliberately the only work done by the default path.
    try:
        control_preview = inspect_legacy_gui_control(source_root)
        if state is ManifestState.V2_READY:
            prepare_preview: dict[str, Any] = {
                "status": "NOT_REQUIRED",
                "ok": True,
                "reason": "v2_ready_can_resume",
                "writes_performed": False,
            }
        else:
            prepare_preview = prepare_v2_ready(
                root,
                apply=False,
                data_home=resolved_data_home,
                source_workspace=(source_root if data_home is not None else None),
                migration_id=migration_id,
                expected_generation=expected_generation,
            )
        stages["preflight"] = _stage(
            "PASS",
            ok=True,
            detail={
                "manifest": _manifest_summary(manager, current),
                "prepare": prepare_preview,
                "gui_control": control_preview,
            },
        )
    except Exception as exc:
        code = _error_code(exc, "upgrade_preflight_failed")
        stages["preflight"] = _stage("BLOCKED", code=code, detail={"error": str(exc)})
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=apply,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code=code,
            next_step=_next_step("error"),
            detail={"error": str(exc)},
        )

    if not apply:
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=False,
            current=current,
            stages=stages,
            status="PREVIEW",
            ok=True,
            stage="preflight",
            code="preview",
            next_step=_next_step("preview"),
            activation_required=state is ManifestState.V2_READY,
        )

    # From here on all writes are inside the explicit --apply path.
    working = current
    if working.state in {ManifestState.V1_ACTIVE, ManifestState.V2_BUILDING}:
        try:
            prepared = prepare_v2_workspace(
                root,
                apply=True,
                data_home=resolved_data_home,
                source_workspace=(source_root if data_home is not None else None),
                migration_id=migration_id,
                expected_generation=expected_generation,
            )
        except Exception as exc:
            code = _error_code(exc, "prepare_failed")
            stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)})
            current = manager.current()
            return _envelope(
                workspace=root,
                data_home=resolved_data_home,
                apply=True,
                current=current,
                stages=stages,
                status="BLOCKED",
                ok=False,
                stage="prepare",
                code=code,
                next_step=_next_step("resume"),
                writes_performed=True,
                detail={"error": str(exc)},
            )
        current = manager.current()
        if prepared.get("status") != ManifestState.V2_BUILDING.value or prepared.get("ok") is not True:
            code = "prepare_failed"
            stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail=prepared)
            return _envelope(
                workspace=root,
                data_home=resolved_data_home,
                apply=True,
                current=current,
                stages=stages,
                status="BLOCKED",
                ok=False,
                stage="prepare",
                code=code,
                next_step=_next_step("resume"),
                writes_performed=True,
                detail=prepared,
            )
        stages["prepare"] = _stage("PASS", ok=True, writes_performed=True, detail=prepared)
        working = current
    else:
        stages["prepare"] = _stage(
            "SKIPPED", ok=True, code="already_v2_ready", detail=_manifest_summary(manager, working)
        )

    if working.state not in {ManifestState.V2_BUILDING, ManifestState.V2_READY}:
        code = "prepare_did_not_reach_v2_ready"
        stages["prepare"] = _stage("BLOCKED", writes_performed=True, code=code, detail=_manifest_summary(manager, working))
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=working,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="prepare",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
        )

    try:
        control_result = _migrate_gui_control(source_root, root)
        if control_result.get("ok") is not True:
            raise GuiControlMigrationError("gui_control_migration_failed")
        _record_binding_recovery_checkpoint(
            manager, str(current.migration_id or working.migration_id), source_root,
        )
        control_result["projection"] = _project_gui_control_outbox(root)
        stages["gui_control"] = _stage(
            "PASS", ok=True, writes_performed=True, detail=control_result
        )
    except Exception as exc:
        code = _error_code(exc, "gui_control_migration_failed")
        stages["gui_control"] = _stage("BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)})
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="gui_control",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail={"error": str(exc)},
        )

    verified = _verify_ready(
        root,
        resolved_data_home,
        manager,
        control_preview,
        source_workspace=source_root,
    )
    if verified.get("ok") is not True:
        code = str(verified.get("code") or "v2_readiness_verification_failed")
        stages["verify"] = _stage("BLOCKED", writes_performed=True, code=code, detail=verified)
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="verify",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail=verified,
        )
    stages["verify"] = _stage("PASS", ok=True, writes_performed=True, detail=verified)

    ready = manager.current()
    if confirm is None:
        stages["activate"] = _stage(
            "PENDING_CONFIRMATION",
            ok=True,
            writes_performed=False,
            code="activation_confirmation_required",
            detail={"required": CONFIRM_ACTIVE, "generation": ready.generation},
        )
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=ready,
            stages=stages,
            status=ManifestState.V2_READY.value,
            ok=True,
            stage="activate",
            code="activation_confirmation_required",
            next_step=_next_step("ready"),
            writes_performed=True,
            activation_required=True,
            detail={"required": CONFIRM_ACTIVE, "generation": ready.generation},
        )

    try:
        governance_activation = _activate_governance_domain(root)
    except Exception as exc:
        code = _error_code(exc, "governance_ledger_activation_failed")
        stages["activate"] = _stage("BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)})
        return _envelope(
            workspace=root, data_home=resolved_data_home, apply=True,
            current=ready, stages=stages, status="BLOCKED", ok=False,
            stage="activate", code=code, next_step="repair the GovernanceV2 ledger before activation",
            writes_performed=True, activation_required=True, detail={"error": str(exc)},
        )

    try:
        active = manager.activate_v2(expected_generation=ready.generation)
    except Exception as exc:
        code = _error_code(exc, "activation_failed")
        stages["activate"] = _stage("BLOCKED", code=code, detail={"error": str(exc)})
        current = manager.current()
        return _envelope(
            workspace=root,
            data_home=resolved_data_home,
            apply=True,
            current=current,
            stages=stages,
            status="BLOCKED",
            ok=False,
            stage="activate",
            code=code,
            next_step=_next_step("resume"),
            writes_performed=True,
            activation_required=current.state is ManifestState.V2_READY,
            detail={"error": str(exc)},
        )

    try:
        memory_activation = _activate_memory_domain(root)
    except Exception as exc:
        code = _error_code(exc, "memory_domain_activation_failed")
        stages["activate"] = _stage(
            "BLOCKED", writes_performed=True, code=code, detail={"error": str(exc)}
        )
        return _envelope(
            workspace=root, data_home=resolved_data_home, apply=True,
            current=active, stages=stages, status="BLOCKED", ok=False,
            stage="activate", code=code, next_step="rerun memoryguard upgrade to repair the active memory domain",
            writes_performed=True, detail={"error": str(exc)},
        )

    cleanup = _cleanup_active_migration(
        root, active, apply=True, source_workspace=source_root,
    )
    stages["activate"] = _stage(
        "PASS", ok=True, writes_performed=True, code="activated",
        detail={**_manifest_summary(manager, active), "memory": memory_activation, "cleanup": cleanup},
    )
    return _envelope(
        workspace=root,
        data_home=resolved_data_home,
        apply=True,
        current=active,
        stages=stages,
        status=ManifestState.V2_ACTIVE.value,
        ok=True,
        stage="complete",
        code="activated",
        next_step=_next_step("active"),
        writes_performed=True,
        activation_required=False,
        detail={"memory": memory_activation, "cleanup": cleanup},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryguard upgrade",
        description="Verified V1-to-V2 migration. Bare `memoryguard upgrade` uses the canonical user data home.",
    )
    parser.add_argument("workspace_arg", nargs="?", help="advanced: explicit isolated workspace path")
    parser.add_argument("-w", "--workspace", default="", help="advanced: explicit isolated workspace path")
    parser.add_argument("--data-home", help="explicit V1 global data home")
    parser.add_argument("--preview", action="store_true", help="show the zero-write migration plan")
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--confirm",
        metavar="V2_ACTIVE",
        help="activate only when exactly V2_ACTIVE is supplied",
    )
    parser.add_argument("--migration-id")
    parser.add_argument("--expected-generation", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.workspace and args.workspace_arg:
        payload = _envelope(
            workspace=Path(args.workspace).expanduser().resolve(),
            data_home=(Path(args.data_home).expanduser().resolve() if args.data_home else resolve_data_home()),
            apply=bool(args.apply),
            current=type("UnknownState", (), {"state": "UNKNOWN", "generation": None, "migration_id": ""})(),
            stages={name: _stage() for name in _STAGE_NAMES},
            status="BLOCKED",
            ok=False,
            stage="preflight",
            code="workspace_specified_twice",
            next_step="specify workspace once, using either the positional argument or --workspace",
        )
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 2
    report = run_upgrade(
        args.workspace_arg or args.workspace or ".",
        data_home=args.data_home,
        apply=bool(args.apply),
        confirm=args.confirm,
        migration_id=args.migration_id,
        expected_generation=args.expected_generation,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
    return 0 if report.get("ok") is True else 2


__all__ = ["CONFIRM_ACTIVE", "SCHEMA", "build_parser", "main", "run_upgrade"]
