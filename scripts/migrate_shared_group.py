# -*- coding: utf-8 -*-
"""Migrate one V1 shared-memory group into an existing V2 target.

The source is opened only through :class:`V1GroupReader` and the write path is
the formal :class:`V1MemoryMigrator` into ``MemoryAtomStore``/``EvidenceStore``.
Dry-run is the default; ``--apply`` performs the shadow migration, drains the
formal evidence projector, and exposes the target atoms as ``ready``.  This
script never activates a manifest and never constructs a V1 store.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.data_home import resolve_data_home  # noqa: E402
from memoryguard.evidence import EvidenceStore  # noqa: E402
from memoryguard.memory import MemoryAtomStore, MemoryReadScope  # noqa: E402
from memoryguard.migration import V1GroupReader, V1MemoryMigrator  # noqa: E402
from memoryguard.system.manifest import ManifestManager  # noqa: E402


# Defaults mirror the known incident but remain overrideable for a real
# migration rehearsal.  The expectation gates are intentionally fail-closed.
DEFAULT_SOURCE_WS = str(ROOT)
DEFAULT_SOURCE_GID = "shared-6767d0c38b9cc5f1"
DEFAULT_TARGET_GID = "shared-9b8b5d020a74b2fd"
DEFAULT_EXPECT_SOURCE = 127
DEFAULT_EXPECT_ACTIVE = 64


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _target_scope(target_ws: Path, target_gid: str) -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(target_ws.resolve()),
        share_group_id=str(target_gid),
        admin=True,
    )


def _target_atoms(target_ws: Path, target_gid: str) -> list[Any]:
    return MemoryAtomStore(target_ws).list_atoms(
        scope=_target_scope(target_ws, target_gid),
        include_building=True,
    )


def _source_mapping_index(memory: MemoryAtomStore, source_gid: str) -> dict[str, str]:
    prefix = f"{source_gid}/"
    return {
        str(item["source_record_id"]): str(item["atom_id"])
        for item in memory.list_source_mappings()
        if str(item.get("source_domain") or "") == "shared_memory"
        and str(item.get("source_ref") or "").startswith(prefix)
        and str(item.get("source_record_id") or "")
        and str(item.get("atom_id") or "")
    }


def _archive_source(source_ws: Path, source_gid: str) -> str:
    source_dir = source_ws / ".memoryguard" / "shared-memory" / source_gid
    if not source_dir.is_dir():
        return ""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = source_ws / ".memoryguard" / "shared-memory-archived"
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"{source_gid}-{stamp}"
    shutil.move(str(source_dir), str(destination))
    return str(destination)


def _manifest_state(target_ws: Path) -> str:
    state = ManifestManager(target_ws).current().state
    return str(getattr(state, "value", state) or "").strip().upper()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate one V1 shared-memory group into a V2 target."
    )
    parser.add_argument(
        "--source-workspace", dest="source_ws", default=DEFAULT_SOURCE_WS,
        help=f"workspace holding the V1 group (default {DEFAULT_SOURCE_WS})",
    )
    parser.add_argument(
        "--source-db", default="",
        help="optional exact V1 memory.db path; otherwise use the standard group layout",
    )
    parser.add_argument(
        "--from", dest="source_gid", default=DEFAULT_SOURCE_GID,
        help=f"V1 source group id (default {DEFAULT_SOURCE_GID})",
    )
    parser.add_argument(
        "--workspace", dest="target_ws", default="",
        help="existing V2 target workspace; default = resolve_data_home()",
    )
    parser.add_argument(
        "--to", dest="target_gid", default=DEFAULT_TARGET_GID,
        help=f"V2 target group id (default {DEFAULT_TARGET_GID})",
    )
    parser.add_argument(
        "--expect-source", type=int, default=DEFAULT_EXPECT_SOURCE,
        help=f"expected V1 source record count (default {DEFAULT_EXPECT_SOURCE})",
    )
    parser.add_argument(
        "--expect-active", type=int, default=DEFAULT_EXPECT_ACTIVE,
        help=f"expected V1 active count (default {DEFAULT_EXPECT_ACTIVE})",
    )
    parser.add_argument("--apply", action="store_true", help="write; default is preview only")
    parser.add_argument(
        "--archive-source", action="store_true",
        help="after verified apply, move the exact V1 group directory to the archive",
    )
    args = parser.parse_args(argv)

    source_ws = Path(args.source_ws).expanduser().resolve()
    target_ws = (
        Path(args.target_ws).expanduser().resolve()
        if args.target_ws
        else resolve_data_home()
    )
    source_db = Path(args.source_db).expanduser().resolve() if args.source_db else None

    print(f"source: {source_ws} / {args.source_gid}")
    print(f"target: {target_ws} / {args.target_gid}")

    # Explicit V1 migration preflight: this is the only place the script
    # looks at the retired group layout.
    reader = V1GroupReader(
        source_ws,
        args.source_gid,
        source_db,
        immutable=True,
    )
    inventory = reader.inventory()
    if not inventory.ok:
        print(f"ABORT: V1 source preflight failed: {inventory.error}")
        return 2
    print(f"source records={inventory.records} active={inventory.active}")
    if inventory.records < 1 or inventory.records != args.expect_source:
        print(
            f"ABORT: source records={inventory.records}, "
            f"expected {args.expect_source}"
        )
        return 2
    if inventory.active != args.expect_active:
        print(
            f"ABORT: source active={inventory.active}, "
            f"expected {args.expect_active}"
        )
        return 2

    try:
        state = _manifest_state(target_ws)
    except Exception as exc:
        print(f"ABORT: V2 target manifest unavailable: {type(exc).__name__}")
        return 2
    if state not in {"V2_BUILDING", "V2_READY", "V2_ACTIVE"}:
        print(f"ABORT: V2 target manifest state is {state or 'UNKNOWN'}")
        return 2

    try:
        memory = MemoryAtomStore(target_ws)
        existing = _target_atoms(target_ws, args.target_gid)
    except Exception as exc:
        print(f"ABORT: V2 target storage unavailable: {type(exc).__name__}")
        return 2
    existing_active = sum(1 for atom in existing if str(atom.status) == "active")
    mapping_index = _source_mapping_index(memory, args.source_gid)
    source_ids = {str(row.get("memory_id") or row.get("id") or "") for row in reader.rows()}
    source_ids.discard("")
    if source_ids and source_ids <= set(mapping_index):
        print("already_migrated: V2 source mappings cover every V1 record")
        return 0
    if existing_active or existing:
        print(
            "ABORT: target group is not empty; refusing to migrate on top of "
            "unmapped V2 atoms"
        )
        return 2

    migrator = V1MemoryMigrator(
        source_ws,
        target=target_ws,
        groups={args.source_gid: reader.db_path},
        group_targets={args.source_gid: args.target_gid},
        include_managed=False,
        immutable_sources=True,
    )
    preview = migrator.preview()
    print("preview=" + _json(preview.to_dict()))
    if not preview.ok:
        print("ABORT: migration preview failed")
        return 2
    if not args.apply:
        print("preview complete; rerun with --apply to write")
        return 0

    result = migrator.migrate(promote=False)
    print("migration=" + _json(result.to_dict()))
    if not result.ok:
        print("ABORT: V2 migration failed")
        return 1

    evidence = EvidenceStore(target_ws)
    projection = memory.project_evidence(evidence)
    print("evidence_projection=" + _json(projection))
    if projection.get("failed") or projection.get("pending"):
        print("ABORT: V2 evidence outbox did not drain")
        return 1
    validation = memory.validate(evidence, include_building=True)
    print("validation=" + _json(validation.to_dict()))
    if not validation.ok:
        print("ABORT: V2 migration validation failed")
        return 1
    memory.set_visibility("ready")

    final_atoms = _target_atoms(target_ws, args.target_gid)
    final_mapping = _source_mapping_index(memory, args.source_gid)
    missing = sorted(source_ids - set(final_mapping))
    final_active = sum(1 for atom in final_atoms if str(atom.status) == "active")
    print(
        f"after: target atoms={len(final_atoms)} active={final_active} "
        f"mapped_source_records={len(final_mapping)} missing={len(missing)}"
    )
    if missing or final_active != args.expect_active:
        print(f"ABORT: V2 target verification failed; missing={missing[:10]}")
        return 1
    if args.archive_source:
        archived = _archive_source(source_ws, args.source_gid)
        if archived:
            print(f"archived_source={archived}")
    print("migration verified OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
