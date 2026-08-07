# -*- coding: utf-8 -*-
"""One-shot migration of a legacy shared-memory group into the AppData group.

The control plane moved from a project ``.memoryguard`` to the AppData control
directory, but the shared-memory records did not move with it: the real 127
records / 64 active stayed in the project workspace group
``shared-6767d0c38b9cc5f1`` while hooks/bindings pointed at a freshly and
silently created near-empty group ``shared-9b8b5d020a74b2fd`` in the AppData
workspace.  This script copies the legacy group's records into the target
group idempotently (reusing ``copy_group_records``), verifies the outcome, and
optionally archives the source group directory.

Idempotent: a re-run detects the ``migrated-from:<source_gid>`` provenance
marker on the target records and reports ``already_migrated`` instead of
writing again.  Dry-run by default; pass ``--apply`` to write.

Usage (defaults mirror the 2026-08 incident):
    python scripts/migrate_shared_group.py                # dry-run
    python scripts/migrate_shared_group.py --apply        # migrate
    python scripts/migrate_shared_group.py --apply --archive-source
    # overrides:
    python scripts/migrate_shared_group.py --apply \
        --source-workspace <proj> --from shared-xxx \
        --workspace <data_home> --to shared-yyy \
        [--expect-source 127] [--expect-active 64]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from memoryguard.data_home import resolve_data_home  # noqa: E402
from memoryguard.group_migration import copy_group_records, _has_migrated_from  # noqa: E402
from memoryguard.shared_memory_store import SharedMemoryStore  # noqa: E402

# Defaults for the 2026-08 incident (overridable via argv).
DEFAULT_SOURCE_WS = str(ROOT)
DEFAULT_SOURCE_GID = "shared-6767d0c38b9cc5f1"
DEFAULT_TARGET_GID = "shared-9b8b5d020a74b2fd"
DEFAULT_EXPECT_SOURCE = 127
DEFAULT_EXPECT_ACTIVE = 64


def _status(store) -> int:
    from memoryguard.group_migration import _status_value
    return sum(1 for r in store.list_records() if _status_value(r) == "active")


def _already_migrated(target_ws: Path, target_gid: str, source_gid: str) -> bool:
    try:
        store = SharedMemoryStore(target_ws, target_gid, read_only=True, must_exist=True)
    except Exception:
        return False
    return any(_has_migrated_from(r, source_gid) for r in store.list_records())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate a legacy shared-memory group into the AppData group.")
    parser.add_argument("--source-workspace", dest="source_ws", default=DEFAULT_SOURCE_WS,
                        help=f"workspace holding the legacy group (default {DEFAULT_SOURCE_WS})")
    parser.add_argument("--from", dest="source_gid", default=DEFAULT_SOURCE_GID,
                        help=f"legacy group id (default {DEFAULT_SOURCE_GID})")
    parser.add_argument("--workspace", dest="target_ws", default=None,
                        help="target (AppData) workspace; default = resolve_data_home()")
    parser.add_argument("--to", dest="target_gid", default=DEFAULT_TARGET_GID,
                        help=f"target group id (default {DEFAULT_TARGET_GID})")
    parser.add_argument("--expect-source", type=int, default=DEFAULT_EXPECT_SOURCE,
                        help=f"expected source record count (default {DEFAULT_EXPECT_SOURCE})")
    parser.add_argument("--expect-active", type=int, default=DEFAULT_EXPECT_ACTIVE,
                        help=f"expected source active count (default {DEFAULT_EXPECT_ACTIVE})")
    parser.add_argument("--apply", action="store_true", help="write; default is dry-run")
    parser.add_argument("--archive-source", action="store_true",
                        help="move the source group dir under .memoryguard/shared-memory-archived/ after a clean copy")
    args = parser.parse_args(argv)

    source_ws = Path(args.source_ws).resolve()
    target_ws = Path(args.target_ws).resolve() if args.target_ws else resolve_data_home()

    print(f"source:  {source_ws} / {args.source_gid}")
    print(f"target:  {target_ws} / {args.target_gid}")

    # ---- expectation gates -------------------------------------------------
    try:
        src = SharedMemoryStore(source_ws, args.source_gid, read_only=True, must_exist=True)
    except Exception as exc:
        print(f"ABORT: cannot open source group: {exc}")
        return 2
    src_total = len(src.list_records())
    src_active = _status(src)
    print(f"source records={src_total} active={src_active}")
    if src_total != args.expect_source or src_total < 1:
        print(f"ABORT: source has {src_total} records, expected {args.expect_source}")
        return 2

    # ---- re-run detection ---------------------------------------------------
    # Must precede the target-active gate: after a successful migration the
    # target legitimately holds active records, and a re-run is a no-op.
    if _already_migrated(target_ws, args.target_gid, args.source_gid):
        print("already_migrated: target records carry migrated-from provenance; nothing to do")
        return 0

    try:
        tgt = SharedMemoryStore(target_ws, args.target_gid, read_only=True, must_exist=True)
    except Exception as exc:
        print(f"ABORT: cannot open target group: {exc}")
        return 2
    tgt_total = len(tgt.list_records())
    tgt_active = _status(tgt)
    print(f"target records={tgt_total} active={tgt_active}")
    if tgt_active != 0 and tgt_total > 1:
        print("ABORT: target already holds active records; refusing to migrate on top of it")
        return 2

    # ---- dry-run / apply ----------------------------------------------------
    result = copy_group_records(
        source_ws, args.source_gid, target_ws, args.target_gid,
        dry_run=not args.apply, archive_source=args.archive_source,
    )
    print(f"dry_run={not args.apply}")
    print(f"copied={result['copied']} updated={result['updated']} replaced={result['replaced']} "
          f"assignments={result['assignments_migrated']} failed={len(result['failed'])}")
    if result["collisions"]:
        print("collisions:", result["collisions"])
    if result["failed"]:
        for item in result["failed"]:
            print("  FAILED:", item["memory_id"], item["error"])
        if args.apply:
            print("migration completed with failures")
            return 1
    if result["archived_to"]:
        print("archived source to:", result["archived_to"])

    if not args.apply:
        print("\ndry-run complete. Re-run with --apply to write.")
        return 0

    # ---- verify --------------------------------------------------------------
    final = SharedMemoryStore(target_ws, args.target_gid, read_only=True, must_exist=True)
    final_total = len(final.list_records())
    final_active = _status(final)
    ids = {r.memory_id for r in final.list_records()}
    src_ids = {r.memory_id for r in src.list_records()}
    missing = sorted(src_ids - ids)
    print(f"after: target records={final_total} active={final_active} missing_source_ids={len(missing)}")
    if missing:
        print("ABORT: not all source records present in target:", missing[:10])
        return 1
    if final_active != args.expect_active:
        print(f"ABORT: target active={final_active}, expected {args.expect_active}")
        return 1
    print("migration verified OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
