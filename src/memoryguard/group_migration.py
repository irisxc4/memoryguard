# -*- coding: utf-8 -*-
"""Shared-memory group migration and legacy-group discovery (Part C).

When the control plane moved from a project ``.memoryguard`` to the AppData
control directory, the shared-memory *records* did not move with it: the real
records stayed in the old workspace's ``shared-memory/<gid>`` directory while
hooks/bindings pointed at a newly and silently created empty group in the new
workspace.  This module is the migration path between the two:

* ``find_nonempty_shared_groups`` / ``discover_legacy_group`` locate legacy
  groups (reusing ``iter_legacy_groups`` from rule_merge_store).
* ``copy_group_records`` copies one legacy group's records into a target
  group idempotently.  Each copy appends a ``migrated-from:<source_gid>``
  provenance entry; a re-run merges into the existing row (the store's own
  dedup) instead of inserting a duplicate.

The no-silent-empty-group guards live in ``SharedMemoryStore.__init__``
(advisory warning) and ``AgentBindingStore.bind_agents_to_group``
(fail-closed) and are tested in ``tests/test_group_migration.py``.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema_v3 import Provenance, SharedMemoryRecord
from .shared_memory_store import SharedMemoryStore


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _has_migrated_from(record: SharedMemoryRecord, source_gid: str) -> bool:
    """True when a target record already carries our migration provenance.

    ``dedup_domain`` is *not* a stable migration marker: ``_init_db`` runs
    ``_migrate_rule_assignments`` on every reopen, which recomputes and
    normalizes dedup_domain to the canonical value.  The provenance entry we
    append is never rewritten, so it is the reliable re-run signal.
    """
    marker = f"migrated-from:{source_gid}"
    for item in record.provenance:
        if getattr(item, "source_object_id", "") == marker:
            return True
    return False


def _status_value(record: SharedMemoryRecord) -> str:
    value = getattr(record.status, "value", None)
    return value if isinstance(value, str) else str(record.status)


def find_nonempty_shared_groups(workspace: str | Path) -> list[dict[str, Any]]:
    """List every shared-memory group that contains at least one record.

    Enumerates ``.memoryguard/shared-memory/<gid>/memory.db`` via
    ``rule_merge_store.iter_legacy_groups``, then opens each group read-only
    and counts its records.  A group whose database cannot be opened is
    reported with an ``error`` instead of aborting the whole sweep.
    """
    from .rule_merge_store import iter_legacy_groups

    results: list[dict[str, Any]] = []
    for group_id, db_path in iter_legacy_groups(workspace):
        if not db_path.exists():
            continue
        try:
            store = SharedMemoryStore(workspace, group_id, read_only=True, must_exist=True)
            records = store.list_records()
        except Exception as exc:  # unreadable legacy DB must not break the sweep
            results.append({
                "group_id": group_id,
                "db_path": str(db_path),
                "records": 0,
                "active": 0,
                "error": str(exc),
            })
            continue
        results.append({
            "group_id": group_id,
            "db_path": str(db_path),
            "records": len(records),
            "active": sum(1 for r in records if _status_value(r) == "active"),
        })
    results.sort(key=lambda item: (item.get("records", 0), item.get("error", "") == ""), reverse=True)
    return results


def discover_legacy_group(workspace: str | Path) -> dict[str, Any] | None:
    """Return the largest readable non-empty legacy group, if any."""
    for item in find_nonempty_shared_groups(workspace):
        if item.get("records", 0) > 0 and not item.get("error"):
            return item
    return None


def copy_group_records(
    source_ws: str | Path,
    source_gid: str,
    target_ws: str | Path,
    target_gid: str,
    *,
    dry_run: bool = False,
    archive_source: bool = False,
    allow_new_target: bool = False,
) -> dict[str, Any]:
    """Copy every record from one shared-memory group into another.

    Idempotent: the store's own dedup (canonical_hash + dedup_domain) merges a
    re-run into the previously migrated row, and each copy appends a
    ``migrated-from:<source_gid>`` provenance entry that survives reopen, so a
    re-run is reported as ``updated`` rather than duplicated.  ``always``
    records carry their audience assignments across (group-targeted audiences
    are re-pointed at the target group).  With ``dry_run=True`` nothing is
    written; the returned summary lists the source records and the target
    occupancy.
    """
    source_ws = Path(source_ws).resolve()
    target_ws = Path(target_ws).resolve()
    if source_gid == target_gid and source_ws == target_ws:
        raise ValueError("source and target group are the same")

    src = SharedMemoryStore(source_ws, source_gid, read_only=True, must_exist=True)
    records = src.list_records()
    tgt = SharedMemoryStore(target_ws, target_gid)
    existing = tgt.list_records()
    existing_by_id = {r.memory_id: r for r in existing}

    def classify(rec) -> str:
        prev = existing_by_id.get(rec.memory_id)
        if prev is None:
            return "copied"
        if _has_migrated_from(prev, source_gid):
            return "updated"  # re-run of this migration: provenance merge
        return "replaced"  # target holds the id under a different origin

    plan: dict[str, Any] = {
        "source": {
            "workspace": str(source_ws),
            "group_id": source_gid,
            "records": len(records),
        },
        "target": {
            "workspace": str(target_ws),
            "group_id": target_gid,
            "existing_records": len(existing),
            "existing_active": sum(1 for r in existing if _status_value(r) == "active"),
        },
        "dry_run": dry_run,
        "copied": 0,
        "updated": 0,
        "replaced": 0,
        "failed": [],
        "assignments_migrated": 0,
        "collisions": [
            rec.memory_id for rec in records
            if classify(rec) == "replaced"
        ],
        "archived_to": "",
    }
    if dry_run:
        for rec in records:
            plan[classify(rec)] += 1
        return plan

    for rec in records:
        try:
            record = SharedMemoryRecord.from_dict(rec.to_dict())
            record.provenance = list(record.provenance) + [
                Provenance(
                    source_object_id=f"migrated-from:{source_gid}",
                    locator=rec.memory_id,
                    excerpt_hash="",
                    source_revision="",
                )
            ]
            assignments: list[dict[str, Any]] = []
            if record.injection_policy == "always":
                for item in src.list_rule_assignments(rec.memory_id):
                    d = item.to_dict()
                    if d.get("target_type") == "group":
                        d["target_id"] = target_gid
                    assignments.append(d)
            tgt.append_record(record, assignments=assignments)
            if assignments:
                plan["assignments_migrated"] += 1
            plan[classify(rec)] += 1
        except Exception as exc:
            plan["failed"].append({"memory_id": rec.memory_id, "error": str(exc)})

    if archive_source and not plan["failed"]:
        sm_dir = source_ws / ".memoryguard" / "shared-memory" / source_gid
        if sm_dir.is_dir():
            archive_root = source_ws / ".memoryguard" / "shared-memory-archived"
            archive_root.mkdir(parents=True, exist_ok=True)
            dest = archive_root / f"{source_gid}-{_now_stamp()}"
            shutil.move(str(sm_dir), str(dest))
            plan["archived_to"] = str(dest)
    return plan
