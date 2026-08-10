#!/usr/bin/env python3
"""Plan or safely build one workspace's V2 shadow databases.

Without ``--apply`` this command is strictly read-only.  ``--apply`` is the
explicit opt-in for backups, governance lock acquisition, manifest BUILDING
checkpointing, and the existing Phase 2 coordinator.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.migration.workspace_prepare import (  # noqa: E402
    SCHEMA,
    WorkspacePrepareError,
    prepare_v2_workspace,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data-home", type=Path)
    parser.add_argument("--migration-id")
    parser.add_argument("--expected-generation", type=int)
    parser.add_argument("--fail-at", help="fault-injection step for fixture tests")
    parser.add_argument("--apply", action="store_true", help="write backups, manifest checkpoints, and V2 DBs")
    args = parser.parse_args(argv)
    try:
        report = prepare_v2_workspace(
            args.workspace,
            apply=bool(args.apply),
            data_home=args.data_home,
            migration_id=args.migration_id,
            expected_generation=args.expected_generation,
            fail_at=args.fail_at,
        )
    except Exception as exc:  # noqa: BLE001 - fixed machine-readable envelope
        report = {
            "schema": SCHEMA,
            "schema_version": 1,
            "status": "FAILED",
            "ok": False,
            "plan": {"mode": "apply" if args.apply else "dry_run", "writes_performed": False},
            "backups": [],
            "source_hashes": {},
            "domains": {},
            "checkpoints": {},
            "validator": {},
            "readiness_eligible": False,
            "failures": [{"kind": type(exc).__name__, "message": str(exc)}],
            "gates": {"dry_run_zero_write": not args.apply, "readiness_eligible_false": True},
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not args.apply:
        return 0
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
