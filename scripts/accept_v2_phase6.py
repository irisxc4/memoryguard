#!/usr/bin/env python3
"""Machine-readable V2-only Phase 6 GUI/CLI acceptance.

The retired dual-route acceptance is replaced by a small in-memory contract
check.  It verifies that pre-V2 states require upgrade, READY is read-only,
ACTIVE uses the native route, and unknown state fails closed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.cli import build_parser  # noqa: E402
from memoryguard.cutover_v2.facade import V2RuntimeFacade  # noqa: E402
from memoryguard.cutover_v2.surfaces import (  # noqa: E402
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    MCP_TOOL_NAMES,
)


class FixtureManifest:
    def __init__(self, state: str) -> None:
        self.state = state
        self.generation = 1
        self.calls = 0

    def current(self) -> dict[str, Any]:
        self.calls += 1
        return {"state": self.state, "generation": self.generation}


class FixturePort:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def dispatch(self, surface: str, name: str, args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((surface, name, args, kwargs))
        return {"ok": True, "fixture": "native"}


def _dispatch_state(state: str) -> dict[str, Any]:
    manifest = FixtureManifest(state)
    native = FixturePort()
    facade = V2RuntimeFacade(manifest=manifest, v2=native, workspace=str(ROOT))

    read = facade.dispatch_gui("get_audit", [])
    mutation = facade.dispatch_gui("lock_memory", [], mutation=True)
    expected = {
        "V1_ACTIVE": ("v2_upgrade_required", "v2_upgrade_required", 0),
        "V2_BUILDING": ("v2_upgrade_required", "v2_upgrade_required", 0),
        "V2_READY": ("", "v2_not_active", 1),
        "V2_ACTIVE": ("", "", 2),
    }.get(state)
    if expected is None:
        return {"state": state, "ok": False, "reason": "unexpected_fixture_state"}
    read_code, mutation_code, expected_calls = expected
    read_ok = read.get("code", "") == read_code if read_code else read.get("path") == "v2"
    mutation_ok = (
        mutation.get("code", "") == mutation_code
        if mutation_code
        else mutation.get("path") == "v2"
    )
    return {
        "state": state,
        "manifest_reads": manifest.calls,
        "native_calls": len(native.calls),
        "read_code": read.get("code", ""),
        "mutation_code": mutation.get("code", ""),
        "read_path": read.get("path"),
        "mutation_path": mutation.get("path"),
        "ok": (
            read_ok
            and mutation_ok
            and manifest.calls == 2
            and len(native.calls) == expected_calls
        ),
    }


def _unknown_state() -> dict[str, Any]:
    manifest = FixtureManifest("FUTURE_STATE")
    native = FixturePort()
    facade = V2RuntimeFacade(manifest=manifest, v2=native, workspace=str(ROOT))
    result = facade.dispatch_gui("get_audit", [])
    return {
        "code": result.get("code"),
        "manifest_reads": manifest.calls,
        "native_calls": len(native.calls),
        "fail_closed": (
            result.get("code") == "v2_manifest_state_unavailable"
            and manifest.calls == 1
            and not native.calls
        ),
    }


def build_report(workspace: Path) -> dict[str, Any]:
    del workspace
    cli_choices = set(build_parser()._subparsers._group_actions[0].choices)
    states = {
        state: _dispatch_state(state)
        for state in ("V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE")
    }
    unknown = _unknown_state()
    checks = {
        "v2_state_matrix": all(item["ok"] for item in states.values()),
        "unknown_fail_closed": unknown["fail_closed"],
        "cli_names_snapshot": cli_choices == set(CLI_COMMAND_NAMES),
        "canonical_surfaces_present": all(
            (MCP_TOOL_NAMES, GUI_METHOD_NAMES, CLI_COMMAND_NAMES)
        ),
    }
    return {
        "contract": "memoryguard-v2-phase6-gui-cli-v2-only",
        "phase": 6,
        "ok": all(checks.values()),
        "checks": checks,
        "states": states,
        "unknown": unknown,
        "surface_counts": {
            "mcp": len(MCP_TOOL_NAMES),
            "gui": len(GUI_METHOD_NAMES),
            "cli": len(CLI_COMMAND_NAMES),
        },
        "cli_command_count": len(cli_choices),
        "failures": [name for name, passed in checks.items() if not passed],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true", help="accepted for automation; output is always JSON")
    args = parser.parse_args(argv)
    report = build_report(args.workspace.expanduser().resolve())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
