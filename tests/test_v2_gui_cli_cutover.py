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

from memoryguard.compat_v2 import (  # noqa: E402
    CLI_COMMAND_NAMES,
    make_cutover_adapter,
)
from memoryguard.gui import SafeBridgeApi, _redact_gui_paths  # noqa: E402
from memoryguard.cli import build_parser  # noqa: E402


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


class V2RuntimeFacade:
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


class Legacy:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def dispatch(self, surface, name, args):
        self.calls.append((surface, name, args))
        return {"ok": True, "path": "legacy"}


@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING"])
def test_legacy_states_have_one_legacy_route(tmp_path, state):
    manifest = Manifest(state)
    legacy = Legacy()
    # Generic fixture uses the stable adapter with an explicit V2 port whose
    # status is read exactly once per dispatch.
    class V2:
        def __init__(self):
            self.status_calls = 0
            self.calls = []

        def status(self, workspace):
            self.status_calls += 1
            return {"state": state}

        def dispatch_gui(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"ok": True}

    v2 = V2()
    adapter = make_cutover_adapter(tmp_path, legacy_port=legacy, v2_port=v2)
    result = adapter.dispatch_gui("get_audit", [], mutation=False)
    assert result["path"] == "legacy"
    assert len(legacy.calls) == 1
    assert not v2.calls
    assert v2.status_calls == 1


def test_ready_read_only_v2_and_mutation_has_no_fallback(tmp_path):
    class V2:
        def __init__(self):
            self.calls = []

        def status(self, workspace):
            return {"state": "V2_READY"}

        def dispatch_gui(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return {"ok": True}

    legacy = Legacy()
    v2 = V2()
    adapter = make_cutover_adapter(tmp_path, legacy_port=legacy, v2_port=v2)
    assert adapter.dispatch_gui("get_audit", [], mutation=False)["path"] == "v2"
    denied = adapter.dispatch_gui("lock_memory", [], mutation=True)
    assert denied["code"] == "v2_not_active"
    assert len(v2.calls) == 1
    assert not legacy.calls


def test_unknown_state_fails_closed(tmp_path):
    class Unknown:
        def status(self, workspace):
            return {"state": "FUTURE_STATE"}

        def dispatch_gui(self, *args, **kwargs):
            raise AssertionError("unknown state must not dispatch")

    result = make_cutover_adapter(tmp_path, legacy_port=Legacy(), v2_port=Unknown()).dispatch_gui(
        "get_audit", [], mutation=False,
    )
    assert result["code"] == "v2_manifest_state_unavailable"


def test_safe_bridge_passes_trusted_context_and_never_uses_actor(tmp_path):
    facade = V2RuntimeFacade("V2_ACTIVE")
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
    from memoryguard.cutover_v2.facade import V2RuntimeFacade

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
    facade = V2RuntimeFacade(manifest=manifest, legacy=Port(), v2=v2, workspace=str(tmp_path))
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

    facade = V2RuntimeFacade("V2_ACTIVE")
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


def test_safe_bridge_real_native_read_uses_binding_scope_and_strips_payload_identity(tmp_path):
    from memoryguard.access_context import AccessContext
    from memoryguard.agent_binding import AgentBindingStore, personal_group_id
    from memoryguard.cutover_v2.facade import V2RuntimeFacade
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_test_services

    group_id = personal_group_id("bridge-agent")
    AgentBindingStore(tmp_path).bind_agent("bridge-agent", group_id)

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
    facade = V2RuntimeFacade(manifest=Manifest(), v2=native, workspace=str(tmp_path))
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

    legacy = Legacy()
    class V2:
        def status(self, workspace):
            return {"state": "V1_ACTIVE"}

    ns = argparse.Namespace(action="migrate", apply=False, workspace=str(tmp_path), func=None)
    adapter = make_cutover_adapter(tmp_path, legacy_port=legacy, v2_port=V2())
    result = adapter.dispatch_cli("groups", ns)
    assert result["path"] == "legacy"
    assert legacy.calls[0][2]["action"] == "migrate"
    assert legacy.calls[0][2]["apply"] is False


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
    from memoryguard.compat_v2 import LegacyV2Adapter

    assert LegacyV2Adapter._cli_is_mutation(command, argparse.Namespace(**payload)) is expected


def test_gui_legacy_source_output_redacts_paths_and_requires_binding(tmp_path):
    bridge = SafeBridgeApi(str(tmp_path))
    denied = bridge.call_readonly("list_sources", [])
    assert denied["code"] == "active_binding_required"

    class Adapter:
        def dispatch_gui(self, method, args=None, *, mutation=False):
            return {
                "status": "not_ready", "path": "legacy", "ok": False,
                "legacy": {
                    "sources": [{
                        "root_id": "root-1", "type": "directory", "scope": "project",
                        "path": str(tmp_path / "private" / "secret.md"),
                    }],
                },
            }

    bridge._source_scope = lambda: ("bound-group", "")
    bridge._cutover = lambda: Adapter()
    listed = bridge.call_readonly("list_sources", [])
    rendered = str(listed)
    assert str(tmp_path) not in rendered
    assert "source:" in rendered


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
