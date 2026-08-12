"""Phase 6 GUI/CLI cutover contract tests.

These fixtures deliberately avoid the real V1 store.  They assert the
observable one-route state machine and the transport boundaries that matter to
the GUI/CLI integration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memoryguard import gui  # noqa: E402
from memoryguard.cutover_v2.facade import (  # noqa: E402
    V2RuntimeFacade as NativeV2RuntimeFacade,
    _cli_is_mutation,
)
from memoryguard.cutover_v2.surfaces import CLI_COMMAND_NAMES  # noqa: E402
from memoryguard.gui import SafeBridgeApi, _dispatch_gui_api_call, _redact_gui_paths  # noqa: E402
from memoryguard.cli import (  # noqa: E402
    _cli_workspace,
    _resolve_gui_workspace,
    build_parser,
)


class Manifest:
    def __init__(self, state: str) -> None:
        self.state = state
        self.generation = 1
        self.calls = 0

    def current(self):
        self.calls += 1
        return {"state": self.state, "generation": self.generation}


class Port:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls: list[tuple] = []

    def dispatch(self, surface, name, args, **kwargs):
        self.calls.append((surface, name, args, kwargs))
        return {"ok": True, "value": self.value}


class FacadeDouble:
    """Minimal native facade double; class name exercises the native shim."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple] = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {"state": self.state, "generation": 1}

    def dispatch_gui(self, name, args=None, *, mutation=None, context=None):
        self.calls.append(("gui", name, args, mutation, context))
        return {"ok": True, "path": "v2", "context": context}

    def dispatch_cli(self, name, args=None, *, mutation=None):
        self.calls.append(("cli", name, args, mutation, None))
        return {"ok": True, "path": "v2"}


@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING"])
def test_non_v2_gui_states_require_upgrade_without_constructing_legacy(
    tmp_path, state, monkeypatch,
):
    class ExplodingFallbackApi:
        def __init__(self, *args, **kwargs):
            raise AssertionError("GUI must not construct a fallback API")

    monkeypatch.setattr(gui, "GovernanceApi", ExplodingFallbackApi)
    v2 = FacadeDouble(state)
    bridge = SafeBridgeApi(str(tmp_path), _v2_port=v2)

    result = bridge.call_readonly("get_audit", [])

    assert result["ok"] is False
    assert result["code"] == "v2_upgrade_required"
    assert result["state"] == state
    assert result["surface"] == "GUI"
    assert result["path"] == "v2"
    assert result["status"] == "blocked"
    assert result["next_step"] == "Run `memoryguard upgrade` before retrying."
    assert v2.calls == []
    assert v2.status_calls == 1


def test_gui_source_uses_the_v2_bridge_dispatch_contract():
    source = (ROOT / "src" / "memoryguard" / "gui.py").read_text(encoding="utf-8")
    assert "class SafeBridgeApi" in source
    assert "def _dispatch_v2" in source
    assert "def dispatch_api" in source


def test_v2_ready_is_read_only_and_never_dispatches_mutations(tmp_path):
    manifest = Manifest("V2_READY")
    v2 = Port("v2")
    facade = NativeV2RuntimeFacade(
        manifest=manifest, v2=v2, workspace=str(tmp_path),
    )
    assert facade.dispatch_gui("get_audit", [], mutation=False)["path"] == "v2"
    denied = facade.dispatch_gui("lock_memory", [], mutation=True)
    assert denied["code"] == "v2_not_active"
    assert len(v2.calls) == 1


def test_unknown_state_fails_closed(tmp_path):
    class Unknown:
        def status(self, workspace):
            return {"state": "FUTURE_STATE"}

        def dispatch_gui(self, *args, **kwargs):
            raise AssertionError("unknown state must not dispatch")

    v2 = Port("v2")
    facade = NativeV2RuntimeFacade(
        manifest=Unknown(), v2=v2, workspace=str(tmp_path),
    )
    result = facade.dispatch_gui(
        "get_audit", [], mutation=False,
    )
    assert result["code"] == "v2_manifest_state_unavailable"
    assert not v2.calls


def test_safe_bridge_passes_trusted_context_and_never_uses_actor(tmp_path):
    facade = FacadeDouble("V2_ACTIVE")
    from memoryguard.access_context import AccessContext

    context = AccessContext(
        trusted_agent_id="bridge-agent",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="bridge-session",
        session_source="transport",
        session_trusted=True,
    )
    bridge = SafeBridgeApi(
        str(tmp_path), direct_mutations=True, _v2_port=facade,
        _trusted_access_context=context,
    )
    # Replacing the preview/actor arguments must not alter transport context.
    result = bridge.request_mutation("lock_memory", [{"actor": "attacker", "preview": True}])
    assert result["path"] == "v2"
    assert facade.calls[0][4]["entrypoint"] == "gui"
    assert facade.calls[0][4]["trusted_agent_id"] == "bridge-agent"


def test_safe_bridge_non_rule_mutation_context_reaches_v2_port(tmp_path):
    from memoryguard.access_context import AccessContext

    class Manifest:
        def __init__(self):
            self.calls = 0

        def current(self):
            self.calls += 1
            return {"state": "V2_ACTIVE", "generation": 1}

    class Port:
        def __init__(self):
            self.calls = []

        def dispatch(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"ok": True}

    manifest, v2 = Manifest(), Port()
    facade = NativeV2RuntimeFacade(manifest=manifest, v2=v2, workspace=str(tmp_path))
    context = AccessContext(
        trusted_agent_id="bridge-agent", is_admin=True, strict_binding=True,
        allow_anon=False, session_id="bridge-session",
        session_source="transport", session_trusted=True,
    )
    bridge = SafeBridgeApi(
        str(tmp_path), direct_mutations=True, _v2_port=facade,
        _trusted_access_context=context,
    )
    result = bridge.request_mutation("lock_memory", [{"actor": "preview-only"}])
    assert result["path"] == "v2"
    assert manifest.calls == 1
    assert v2.calls and v2.calls[0][1]["context"]["trusted_agent_id"] == "bridge-agent"


def test_safe_bridge_readonly_carries_bound_context_without_payload_identity(tmp_path):
    from memoryguard.access_context import AccessContext

    facade = FacadeDouble("V2_ACTIVE")
    context = AccessContext(
        trusted_agent_id="bridge-agent", is_admin=False, strict_binding=True,
        allow_anon=False, session_id="bridge-session",
        session_source="transport", session_trusted=True,
    )
    bridge = SafeBridgeApi(
        str(tmp_path), direct_mutations=True, _v2_port=facade,
        _trusted_access_context=context,
    )
    result = bridge.call_readonly("get_memory", [{"agent_instance_id": "attacker"}])
    assert result["path"] == "v2"
    trusted = facade.calls[0][4]
    assert trusted["trusted_agent_id"] == "bridge-agent"
    assert trusted["__native_transport_capability"] is not None


@pytest.mark.parametrize(
    ("method", "args", "mutation"),
    [
        ("get_memory", ["memory-1"], False),
        ("lock_memory", ["memory-1"], True),
        ("knowledge_add", ["C:/fixture/source", "Fixture"], True),
    ],
)
def test_pywebview_and_localhost_share_exact_business_dispatch(tmp_path, method, args, mutation):
    """Both transports call the same server-side classifier and envelope path."""
    from memoryguard.access_context import AccessContext

    facade = FacadeDouble("V2_ACTIVE")
    bridge = SafeBridgeApi(
        str(tmp_path),
        direct_mutations=True,
        _v2_port=facade,
        _trusted_access_context=AccessContext(
            trusted_agent_id="bridge-agent",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="bridge-session",
            session_source="transport",
            session_trusted=True,
        ),
    )
    webview = bridge.dispatch_api(method, args)
    localhost = _dispatch_gui_api_call(bridge, method, args)
    assert webview == localhost
    assert facade.calls
    assert all(call[3] is mutation for call in facade.calls[-2:])


def test_safe_bridge_real_native_read_uses_binding_scope_and_strips_payload_identity(tmp_path):
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.group_native import GroupControlService, personal_group_id
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_test_services

    group_id = personal_group_id("bridge-agent")
    GroupControlService(tmp_path, write=True).bind_agent("bridge-agent", group_id)

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    calls = []
    native = NativeV2RuntimePort(
        tmp_path,
        state_provider=Manifest(),
        services=bind_native_test_services({
            "get_memory": lambda payload, **kwargs: calls.append((payload, kwargs)) or {"record": True},
        }),
    )
    facade = NativeV2RuntimeFacade(manifest=Manifest(), v2=native, workspace=str(tmp_path))
    bridge = SafeBridgeApi(
        str(tmp_path), _v2_port=facade,
        _trusted_access_context=AccessContext(
            trusted_agent_id="bridge-agent", is_admin=False,
            strict_binding=True, allow_anon=False,
            session_id="bridge-session", session_source="transport",
            session_trusted=True,
        ),
    )
    result = bridge.call_readonly("get_memory", [{"memory_id": "m1"}])
    assert result["ok"] is True, result
    assert calls and calls[0][0] == {"memory_id": "m1"}, (result, calls)
    assert calls[0][1]["context"]["agent_instance_id"] == "bridge-agent"
    assert calls[0][1]["context"]["share_group_id"] == group_id
    spoofed = bridge.call_readonly(
        "get_memory", [{"memory_id": "m1", "agent_instance_id": "attacker"}],
    )
    assert spoofed["code"] == "context_identity_spoof"


def test_cli_snapshot_matches_all_commands_and_namespace_subactions_survive(tmp_path):
    parser = build_parser()
    choices = set(parser._subparsers._group_actions[0].choices)
    assert choices == set(CLI_COMMAND_NAMES)

    ns = argparse.Namespace(action="migrate", apply=False, workspace=str(tmp_path), func=None)
    v2 = Port("v2")
    facade = NativeV2RuntimeFacade(
        manifest=Manifest("V2_ACTIVE"), v2=v2, workspace=str(tmp_path),
    )
    result = facade.dispatch_cli("groups", ns)
    assert result["path"] == "v2"
    assert v2.calls[0][2].action == "migrate"
    assert v2.calls[0][2].apply is False


def test_bare_gui_uses_current_project_workspace_and_supports_workspace_flag(tmp_path, monkeypatch):
    (tmp_path / ".memoryguard").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.delenv("MEMORYGUARD_HOME", raising=False)

    parser = build_parser()
    bare = parser.parse_args(["gui"])
    flagged = parser.parse_args(["gui", "--workspace", str(tmp_path)])

    assert _resolve_gui_workspace([]) == tmp_path.resolve()
    assert _cli_workspace(bare) == tmp_path.resolve()
    assert _cli_workspace(flagged) == tmp_path.resolve()


def test_bare_gui_skips_v1_parent_and_discovers_v2_child(tmp_path, monkeypatch):
    from memoryguard import cli

    root = tmp_path / "tools"
    child = root / "memoryguard"
    (root / ".memoryguard").mkdir(parents=True)
    (child / ".memoryguard").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.delenv("MEMORYGUARD_HOME", raising=False)

    states = {
        root.resolve(): ("V1_ACTIVE", 0, object()),
        child.resolve(): ("V2_ACTIVE", 11, object()),
    }
    monkeypatch.setattr(cli, "_cli_manifest_snapshot", lambda path: states.get(
        Path(path).resolve(), ("UNKNOWN", None, None),
    ))

    assert _resolve_gui_workspace([]) == child.resolve()


@pytest.mark.parametrize(
    ("command", "payload", "expected"),
    [
        ("groups", {"action": "migrate", "apply": False}, False),
        ("groups", {"action": "migrate", "apply": True}, True),
        ("source", {"action": "list"}, False),
        ("source", {"action": "add"}, True),
        ("import", {"action": "preview"}, False),
        ("import", {"action": "create"}, True),
        ("hooks", {"action": "status"}, False),
        ("hooks", {"action": "install"}, True),
        ("provider", {"action": "repair"}, True),
        ("gc", {"apply": False}, False),
        ("gc", {"apply": True}, True),
        ("storage", {"action": "audit"}, False),
        ("storage", {"action": "report"}, False),
        ("storage", {"action": "compact", "apply": False}, False),
        ("storage", {"action": "compact", "apply": True}, True),
        ("storage", {"action": "sweep", "apply": False}, True),
        ("storage", {"action": "lease-acquire"}, True),
        ("desktop", {}, True),
    ],
)
def test_cli_mutation_classifier_preserves_subaction(command, payload, expected):
    assert _cli_is_mutation(command, argparse.Namespace(**payload)) is expected


def test_gui_recursive_path_redactor_is_stable_and_preserves_bytes(tmp_path):
    secret_path = str(tmp_path / "private" / "secret.md")
    blob = b"\x00/private-bytes"
    payload = {
        "nested": [{
            "root_path": secret_path,
            "relative_path": "private/secret.md",
            "opaque": blob,
            "path": blob,
        }],
        "bytes": blob,
    }
    safe = _redact_gui_paths(payload, "bound-group")
    assert safe["bytes"] is blob
    assert safe["nested"][0]["path"] is blob
    item = safe["nested"][0]
    assert set(item["root_path"]) == {"ref", "hash", "summary"}
    assert len(item["root_path"]["hash"]) == 64
    assert secret_path not in json.dumps(safe, ensure_ascii=False, default=repr)
    assert "private/secret.md" not in json.dumps(safe, ensure_ascii=False, default=repr)
    assert _redact_gui_paths(payload, "bound-group") == safe
