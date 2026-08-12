#!/usr/bin/env python3
"""Safely build MemoryGuard V2 and stop at V2_READY.

Default mode is zero-write.  ``--apply`` is required to build the frozen V2
shadow and transition to V2_READY.  This command never activates V2_ACTIVE.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.migration.ready_prepare import READY_SCHEMA, prepare_v2_ready  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--data-home", type=Path, help="canonical user data home; defaults to MemoryGuard data home")
    parser.add_argument("--migration-id")
    parser.add_argument("--expected-generation", type=int)
    parser.add_argument("--apply", action="store_true", help="build V2 shadow and stop at V2_READY")
    args = parser.parse_args(argv)
    try:
        report = prepare_v2_ready(
            args.workspace,
            apply=bool(args.apply),
            data_home=args.data_home,
            migration_id=args.migration_id,
            expected_generation=args.expected_generation,
        )
    except Exception as exc:  # noqa: BLE001 - bounded machine envelope
        report = {
            "schema": READY_SCHEMA,
            "status": "BLOCKED",
            "ok": False,
            "ready": False,
            "manifest_state": "",
            "v2_active": False,
            "activation_required": False,
            "stage": "exception",
            "error": type(exc).__name__,
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not args.apply:
        return 0 if report.get("ok") else 1
    return 0 if report.get("status") == "V2_READY" and report.get("v2_active") is False else 2


if __name__ == "__main__":
    raise SystemExit(main())
