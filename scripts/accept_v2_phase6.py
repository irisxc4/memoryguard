#!/usr/bin/env python3
"""Machine-readable Phase 6 GUI/CLI cutover acceptance.

The script exercises only isolated in-memory ports.  It never promotes the
real workspace and only reads its existing manifest before/after the fixture
run.  stdout is always one compact JSON document for CI/host automation.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard.cli import build_parser  # noqa: E402
from memoryguard.compat_v2 import CLI_COMMAND_NAMES, make_cutover_adapter  # noqa: E402
from memoryguard.cutover_v2 import V2RuntimeFacade  # noqa: E402
from memoryguard.system.manifest import ManifestManager  # noqa: E402


class FixtureManifest:
    def __init__(self, state: str) -> None:
        self.state = state
        self.generation = 1
        self.calls = 0

    def current(self) -> dict[str, Any]:
        self.calls += 1
        return {"state": self.state, "generation": self.generation}


class FixturePort:
    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[tuple[Any, ...]] = []

    def dispatch(self, surface: str, name: str, args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((surface, name, args, kwargs))
        return {"ok": True, "fixture": self.label}


def _dispatch_state(state: str, root: Path) -> dict[str, Any]:
    manifest = FixtureManifest(state)
    legacy, v2 = FixturePort("legacy"), FixturePort("v2")
    facade = V2RuntimeFacade(manifest=manifest, legacy=legacy, v2=v2, workspace=str(root))
    legacy_adapter = make_cutover_adapter(root, legacy_port=legacy, v2_port=facade)

    before = manifest.calls
    gui_read = legacy_adapter.dispatch_gui("get_audit", [], mutation=False)
    gui_mutation = legacy_adapter.dispatch_gui("lock_memory", [], mutation=True)

    # ``groups migrate`` is a dry-run; explicit --apply is a mutation.  Keep
    # Namespace fields intact to detect accidental argument collapsing.
    import argparse

    cli_read_args = argparse.Namespace(action="migrate", apply=False, workspace=str(root), func=None)
    cli_write_args = argparse.Namespace(action="migrate", apply=True, workspace=str(root), func=None)
    cli_read = legacy_adapter.dispatch_cli("groups", cli_read_args)
    cli_write = legacy_adapter.dispatch_cli("groups", cli_write_args)
    calls = manifest.calls - before
    expected_calls = 4
    expected_path = {
        "V1_ACTIVE": "legacy",
        "V2_BUILDING": "legacy",
        "V2_READY": "v2",
        "V2_ACTIVE": "v2",
    }.get(state)
    expected_legacy_calls = 4 if expected_path == "legacy" else 0
    expected_v2_calls = 2 if state == "V2_READY" else (4 if state == "V2_ACTIVE" else 0)
    return {
        "manifest_reads": calls,
        "manifest_reads_exact": calls == expected_calls,
        "gui_read_path": gui_read.get("path"),
        "gui_mutation_code": gui_mutation.get("code"),
        "cli_read_path": cli_read.get("path"),
        "cli_write_code": cli_write.get("code"),
        "expected_path": expected_path,
        "legacy_calls": len(legacy.calls),
        "v2_calls": len(v2.calls),
        "legacy_single_route": len(legacy.calls) == expected_legacy_calls,
        "v2_single_route": len(v2.calls) == expected_v2_calls,
        "ready_mutation_blocked": state != "V2_READY" or gui_mutation.get("code") == "v2_not_active",
        "fixture_ok": (
            calls == expected_calls
            and gui_read.get("path") == expected_path
            and cli_read.get("path") == expected_path
            and (state != "V2_READY" or cli_write.get("code") == "v2_not_active")
            and (state not in {"V1_ACTIVE", "V2_BUILDING"} or cli_write.get("path") == "legacy")
        ),
    }


def _unknown_fixture(root: Path) -> dict[str, Any]:
    manifest = FixtureManifest("FUTURE_STATE")
    legacy, v2 = FixturePort("legacy"), FixturePort("v2")
    facade = V2RuntimeFacade(manifest=manifest, legacy=legacy, v2=v2, workspace=str(root))
    adapter = make_cutover_adapter(root, legacy_port=legacy, v2_port=facade)
    result = adapter.dispatch_gui("get_audit", [], mutation=False)
    return {
        "code": result.get("code"),
        "legacy_calls": len(legacy.calls),
        "v2_calls": len(v2.calls),
        "fail_closed": result.get("code") == "v2_manifest_state_unavailable" and not legacy.calls and not v2.calls,
    }


def _corrupt_fixture(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / ".memoryguard" / "system" / "manifest.db"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(b"not-a-sqlite-manifest")

    class Legacy:
        def __init__(self):
            self.calls = 0

        def dispatch(self, *args, **kwargs):
            self.calls += 1
            return {"ok": True}

    legacy = Legacy()
    result = make_cutover_adapter(root, legacy_port=legacy).dispatch_gui("get_audit", [])
    return {
        "code": result.get("code"),
        "legacy_calls": legacy.calls,
        "fail_closed": result.get("code") == "v2_manifest_state_unavailable" and legacy.calls == 0,
    }


def _bridge_lazy_fixture(root: Path) -> dict[str, Any]:
    """Exercise the real SafeBridge sandbox gate and lazy legacy seam."""
    from memoryguard import gui
    from memoryguard.access_context import AccessContext

    class Facade:
        def __init__(self, state: str) -> None:
            self.state = state
            self.calls: list[tuple[Any, ...]] = []

        def status(self, workspace: str = "") -> dict[str, Any]:
            return {"state": self.state, "generation": 1}

        def dispatch_gui(self, name: str, args: Any = None, *, mutation: bool = False, context: Any = None):
            self.calls.append((name, args, mutation, context))
            return {"ok": True, "path": "v2"}

    from memoryguard.security import RequestQueue

    original_api = gui.GovernanceApi
    original_notify = RequestQueue._notify_desktop
    count = {"legacy": 0}

    class CountingApi(original_api):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            count["legacy"] += 1
            super().__init__(*args, **kwargs)

    gui.GovernanceApi = CountingApi
    RequestQueue._notify_desktop = lambda self, request_id: None
    previous = os.environ.get("MEMORYGUARD_SANDBOX")
    os.environ["MEMORYGUARD_SANDBOX"] = "1"
    try:
        root.mkdir(parents=True, exist_ok=True)
        legacy_state = Facade("V1_ACTIVE")
        bridge = gui.SafeBridgeApi(str(root), direct_mutations=False, _v2_port=legacy_state)
        deferred = bridge.request_mutation("lock_memory", [])
        active_state = Facade("V2_ACTIVE")
        v2_bridge = gui.SafeBridgeApi(
            str(root),
            direct_mutations=False,
            _v2_port=active_state,
            _trusted_access_context=AccessContext(
                trusted_agent_id="phase6-agent",
                is_admin=True,
                strict_binding=True,
                allow_anon=False,
                session_id="phase6-session",
                session_source="fixture",
                session_trusted=True,
            ),
        )
        v2 = v2_bridge.request_mutation("lock_memory", [{"actor": "attacker"}])
        return {
            "sandbox_deferred": bool(deferred.get("deferred")) and deferred.get("ok") is True,
            "sandbox_legacy_instances": count["legacy"],
            "v2_path": v2.get("path"),
            "v2_legacy_instances": count["legacy"],
            "trusted_context_forwarded": bool(
                active_state.calls
                and isinstance(active_state.calls[0][3], dict)
                and active_state.calls[0][3].get("trusted_agent_id") == "phase6-agent"
            ),
            "workspace_files": sorted(path.name for path in root.glob(".memoryguard/request-queue.json")),
        }
    finally:
        gui.GovernanceApi = original_api
        RequestQueue._notify_desktop = original_notify
        if previous is None:
            os.environ.pop("MEMORYGUARD_SANDBOX", None)
        else:
            os.environ["MEMORYGUARD_SANDBOX"] = previous


def _http_mutation_fixture(root: Path) -> dict[str, Any]:
    """Run one localhost HTTP mutation through SafeBridge, then shut down."""
    from memoryguard import gui
    from memoryguard.security import RequestQueue

    import re

    original_server = gui.http.server.ThreadingHTTPServer
    original_notify = RequestQueue._notify_desktop
    holder: dict[str, Any] = {}

    class CapturingServer(original_server):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            holder["server"] = self

    gui.http.server.ThreadingHTTPServer = CapturingServer
    RequestQueue._notify_desktop = lambda self, request_id: None
    previous = os.environ.get("MEMORYGUARD_SANDBOX")
    os.environ["MEMORYGUARD_SANDBOX"] = "1"
    def _run_server() -> None:
        # Keep acceptance stdout as exactly one JSON document.
        with contextlib.redirect_stdout(io.StringIO()):
            gui.open_localhost_window(str(root), auto_open=False)

    thread = threading.Thread(target=_run_server, daemon=True)
    try:
        thread.start()
        deadline = time.time() + 10
        while "server" not in holder and time.time() < deadline:
            time.sleep(0.01)
        server = holder.get("server")
        if server is None:
            return {"http_ok": False, "reason": "server_not_started"}
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib_request.urlopen(f"{base}/", timeout=5) as response:
            page = response.read().decode("utf-8")
        match = re.search(r"window.__MG_SESSION__=\"([^\"]+)\"", page)
        if not match:
            return {"http_ok": False, "reason": "session_not_found"}
        req = urllib_request.Request(
            f"{base}/api/submit_request",
            data=json.dumps(["lock_memory", []]).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Session-Token": match.group(1)},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"http_ok": payload.get("ok") is True and payload.get("deferred") is True, "http_payload": payload}
    except Exception as exc:
        return {"http_ok": False, "reason": str(exc)}
    finally:
        gui.http.server.ThreadingHTTPServer = original_server
        RequestQueue._notify_desktop = original_notify
        if previous is None:
            os.environ.pop("MEMORYGUARD_SANDBOX", None)
        else:
            os.environ["MEMORYGUARD_SANDBOX"] = previous
        server = holder.get("server")
        if server is not None:
            server.shutdown()
            server.server_close()
        thread.join(timeout=10)


def _real_manifest_snapshot(workspace: Path) -> tuple[str, int] | None:
    try:
        record = ManifestManager(workspace).current()
        return (record.state.value, int(record.generation))
    except Exception:
        return None


def _optional_surfaces() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("memoryguard.mcp_server", "memoryguard.host_hooks"):
        result[name] = importlib.util.find_spec(name) is not None
    return result


def build_report(workspace: Path) -> dict[str, Any]:
    real_before = _real_manifest_snapshot(workspace)
    cli_choices = set(build_parser()._subparsers._group_actions[0].choices)
    # The compatibility snapshot is the independent contract.  Exact set
    # equality detects additions/removals without a second hand-maintained
    # numeric constant drifting whenever an approved command is introduced.
    names_ok = cli_choices == set(CLI_COMMAND_NAMES)
    with tempfile.TemporaryDirectory(prefix="memoryguard-v2-phase6-") as fixture_dir:
        fixture_root = Path(fixture_dir)
        states = {state: _dispatch_state(state, fixture_root / state) for state in (
            "V1_ACTIVE", "V2_BUILDING", "V2_READY", "V2_ACTIVE",
        )}
        unknown = _unknown_fixture(fixture_root / "unknown")
        corrupt = _corrupt_fixture(fixture_root / "corrupt")
        bridge = _bridge_lazy_fixture(fixture_root / "bridge")
        http = _http_mutation_fixture(fixture_root / "http")
    real_after = _real_manifest_snapshot(workspace)
    unchanged = real_before == real_after
    checks = {
        "state_matrix": all(item["fixture_ok"] for item in states.values()),
        "manifest_read_once_per_call": all(item["manifest_reads_exact"] for item in states.values()),
        "single_route": all(item["legacy_single_route"] and item["v2_single_route"] for item in states.values()),
        "unknown_fail_closed": unknown["fail_closed"],
        "corrupt_manifest_fail_closed": corrupt["fail_closed"],
        "cli_names_snapshot": names_ok,
        "safe_bridge_sandbox": bridge.get("sandbox_deferred") is True,
        "safe_bridge_v2_lazy": bridge.get("v2_path") == "v2" and bridge.get("v2_legacy_instances") == 0,
        "http_mutation_through_bridge": http.get("http_ok") is True,
        "real_workspace_unchanged": unchanged,
    }
    return {
        "contract": "memoryguard-v2-phase6-gui-cli",
        "phase": 6,
        "ok": all(checks.values()),
        "checks": checks,
        "states": states,
        "unknown": unknown,
        "corrupt": corrupt,
        "bridge": bridge,
        "http": http,
        "cli_command_count": len(cli_choices),
        "optional_surfaces": _optional_surfaces(),
        "real_manifest_before": real_before,
        "real_manifest_after": real_after,
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
