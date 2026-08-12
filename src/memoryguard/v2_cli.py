"""Explicit MemoryGuard V2 cutover CLI installed with the Python package.

The ordinary ``memoryguard`` command follows the active manifest.  This small
operator command exists only for the one-time V1 -> V2 cutover lifecycle:
status, safe frozen-source preparation, and explicit activation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .migration.gui_control import inspect_legacy_gui_control, migrate_legacy_gui_control
from .migration.ready_prepare import prepare_v2_ready
from .system.manifest import ManifestManager, ManifestState


SCHEMA = "memoryguard-v2-operator-cli-1"
_CONFIRM_ACTIVE = "V2_ACTIVE"


def _public_manifest(manager: ManifestManager) -> dict[str, Any]:
    current = manager.current(immutable=True)
    return {
        "state": current.state.value,
        "generation": current.generation,
        "migration_id": current.migration_id,
        "v2_ready": current.state in {ManifestState.V2_READY, ManifestState.V2_ACTIVE},
        "v2_active": current.state is ManifestState.V2_ACTIVE,
        "has_source_digest": bool(current.source_digest),
        "has_target_digest": bool(current.target_digest),
        "has_manifest_digest": bool(current.manifest_digest),
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _error(command: str, code: str, *, state: str = "", detail: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "command": command,
        "status": "BLOCKED",
        "ok": False,
        "code": code,
    }
    if state:
        payload["state"] = state
    if detail:
        payload["detail"] = detail
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memoryguard-v2",
        description="Explicit MemoryGuard V2 migration and activation operator CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="read the V2 cutover manifest without writing")
    status.add_argument("-w", "--workspace", default=".")

    prepare = sub.add_parser("prepare", help="build a frozen V2 shadow and stop at V2_READY")
    prepare.add_argument("-w", "--workspace", default=".")
    prepare.add_argument("--data-home")
    prepare.add_argument("--migration-id")
    prepare.add_argument("--expected-generation", type=int)
    prepare.add_argument("--apply", action="store_true", help="perform the shadow build; default is zero-write")

    control = sub.add_parser(
        "migrate-gui-control",
        help="migrate legacy AgentBinding metadata into the V2 system control plane",
    )
    control.add_argument("-w", "--workspace", default=".")
    control.add_argument(
        "--apply",
        action="store_true",
        help="write the digest-bound V2 control migration; default is zero-write preview",
    )

    activate = sub.add_parser("activate", help="transition V2_READY to V2_ACTIVE after a fresh drift check")
    activate.add_argument("-w", "--workspace", default=".")
    activate.add_argument(
        "--confirm",
        required=True,
        metavar="V2_ACTIVE",
        help="must be exactly V2_ACTIVE; activation is never implicit",
    )
    activate.add_argument("--expected-generation", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve()

    try:
        manager = ManifestManager(workspace)
        if args.command == "status":
            _emit({"schema": SCHEMA, "command": "status", "status": "OK", "ok": True, **_public_manifest(manager)})
            return 0

        if args.command == "migrate-gui-control":
            report = (
                migrate_legacy_gui_control(workspace)
                if bool(args.apply)
                else inspect_legacy_gui_control(workspace)
            )
            _emit({"schema": SCHEMA, "command": "migrate-gui-control", **report})
            return 0 if report.get("ok") else 2

        if args.command == "prepare":
            report = prepare_v2_ready(
                workspace,
                apply=bool(args.apply),
                data_home=(None if not args.data_home else Path(args.data_home).expanduser().resolve()),
                migration_id=args.migration_id,
                expected_generation=args.expected_generation,
            )
            _emit({"schema": SCHEMA, "command": "prepare", **report})
            if not args.apply:
                return 0 if report.get("ok") else 1
            return 0 if report.get("status") == "V2_READY" and report.get("v2_active") is False else 2

        current = manager.current()
        if args.confirm != _CONFIRM_ACTIVE:
            _emit(_error("activate", "activation_confirmation_mismatch", state=current.state.value))
            return 2
        if current.state is ManifestState.V2_ACTIVE:
            _emit({"schema": SCHEMA, "command": "activate", "status": "V2_ACTIVE", "ok": True, **_public_manifest(manager)})
            return 0
        if current.state is not ManifestState.V2_READY:
            _emit(_error("activate", "activation_requires_v2_ready", state=current.state.value))
            return 2
        if args.expected_generation is not None and current.generation != args.expected_generation:
            _emit(_error("activate", "manifest_generation_conflict", state=current.state.value))
            return 2

        active = manager.activate_v2(expected_generation=current.generation)
        _emit({
            "schema": SCHEMA,
            "command": "activate",
            "status": active.state.value,
            "ok": active.state is ManifestState.V2_ACTIVE,
            "state": active.state.value,
            "generation": active.generation,
            "migration_id": active.migration_id,
            "v2_active": active.state is ManifestState.V2_ACTIVE,
        })
        return 0 if active.state is ManifestState.V2_ACTIVE else 2
    except Exception as exc:  # stable operator envelope; no traceback by default
        _emit(_error(str(getattr(args, "command", "") or "unknown"), type(exc).__name__, detail=str(exc)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
