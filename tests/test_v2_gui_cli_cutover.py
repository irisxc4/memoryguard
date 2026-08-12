"""Phase 6 GUI/CLI cutover contract tests.

These fixtures deliberately avoid the real V1 store.  They assert the
observable one-route state machine and the transport boundaries that matter to
the GUI/CLI integration.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace
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
from memoryguard.runtime_v2.native_ports import (  # noqa: E402
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.cli import (  # noqa: E402
    _cli_workspace,
    _resolve_gui_workspace,
    build_parser,
    main as cli_main,
)
from memoryguard.mcp_server import _resolve_memory_workspace  # noqa: E402
from memoryguard.memory import MemoryAtomStore  # noqa: E402
from memoryguard.evidence.store import EvidenceStore  # noqa: E402
from memoryguard.governance_v2 import GovernanceV2  # noqa: E402
from memoryguard.runtime_v2.group_native import GroupControlService  # noqa: E402
from memoryguard.storage.layout import WorkspaceV2Layout  # noqa: E402
from memoryguard.storage.schema import initialize_all  # noqa: E402
from memoryguard.system.manifest import ManifestManager, ManifestState  # noqa: E402


def test_webview_file_picker_passes_filter_items_not_pipe_joined_string(tmp_path, monkeypatch):
    selected = tmp_path / "bundle.zip"
    selected.write_bytes(b"fixture")
    calls = []

    class Window:
        def create_file_dialog(self, *args, **kwargs):
            calls.append((args, kwargs))
            return [str(selected)]

    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(OPEN_DIALOG="open", FOLDER_DIALOG="folder"),
    )
    result = gui._pick_path_with_dialog(Window(), for_files=True)

    assert result["path"] == str(selected.resolve())
    assert calls and calls[0][0] == ("open",)
    filters = calls[0][1]["file_types"]
    assert isinstance(filters, tuple)
    assert filters == (
        "All files (*.*)",
        "Zip files (*.zip)",
        "JSON files (*.json)",
        "JSONL files (*.jsonl)",
    )


def test_agent_residual_queries_use_agent_route_without_source_binding_gate():
    assert "get_residual_cleanup" not in gui._GUI_SOURCE_READS
    assert "get_agent_data" not in gui._GUI_SOURCE_READS


def test_unbound_agent_residual_query_requires_trusted_process_only(tmp_path):
    """Residual inspection must work before an Agent has a memory binding."""
    from memoryguard.access_context import AccessContext

    class AgentService:
        def residual_cleanup(self, *, instance_id="", candidate_id=""):
            return {
                "ok": True,
                "status": "succeeded",
                "instance_id": instance_id,
                "candidate_id": candidate_id,
                "items": [],
            }

    port = NativeV2RuntimePort(tmp_path)
    port._agent_native_service = AgentService()
    access = AccessContext(
        trusted_agent_id="unbound-agent",
        is_admin=False,
        strict_binding=True,
        allow_anon=False,
        session_id="unbound-session",
        session_source="transport",
        session_trusted=True,
    )
    context = bind_native_transport_context(access, workspace_id=str(tmp_path))
    result = port.dispatch_gui(
        "get_residual_cleanup",
        ["unbound-agent"],
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )

    assert result["ok"] is True
    assert result["data"]["instance_id"] == "unbound-agent"


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


def test_bare_gui_uses_global_control_workspace_and_supports_workspace_flag(tmp_path, monkeypatch):
    project = tmp_path / "project"
    data_home = tmp_path / "global-home"
    (project / ".memoryguard").mkdir(parents=True)
    (data_home / ".memoryguard").mkdir(parents=True)
    monkeypatch.chdir(project)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    parser = build_parser()
    bare = parser.parse_args(["gui"])
    flagged = parser.parse_args(["gui", "--workspace", str(project)])

    assert _resolve_gui_workspace([]) == data_home.resolve()
    assert _cli_workspace(bare) == data_home.resolve()
    assert _cli_workspace(flagged) == project.resolve()


def test_bare_gui_does_not_switch_to_nearby_project_database(tmp_path, monkeypatch):
    root = tmp_path / "tools"
    child = root / "memoryguard"
    data_home = tmp_path / "global-home"
    (root / ".memoryguard").mkdir(parents=True)
    (child / ".memoryguard").mkdir(parents=True)
    (data_home / ".memoryguard").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    assert _resolve_gui_workspace([]) == data_home.resolve()


def _activate_v2_workspace(root: Path) -> None:
    layout = WorkspaceV2Layout(root)
    initialize_all(layout)
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(root)
    manager.transition(ManifestState.V2_BUILDING, migration_id="workspace-resolver-repro")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="resolver-source",
        target_digest="resolver-target",
        manifest_digest="resolver-manifest",
        digests={"validator_passed": True, "checkpoints": {"resolver": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def test_bare_control_cli_uses_global_data_home(tmp_path, monkeypatch):
    root = tmp_path / "tools"
    child = root / "memoryguard"
    data_home = tmp_path / "global-home"
    (root / ".memoryguard").mkdir(parents=True)
    child.mkdir()
    _activate_v2_workspace(child)
    _activate_v2_workspace(data_home)
    monkeypatch.chdir(root)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    args = build_parser().parse_args(["doctor"])

    assert _cli_workspace(args) == data_home.resolve()


def test_bare_mcp_skips_v1_parent_and_discovers_v2_child(tmp_path, monkeypatch):
    root = tmp_path / "tools"
    child = root / "memoryguard"
    (root / ".memoryguard").mkdir(parents=True)
    child.mkdir()
    _activate_v2_workspace(child)
    monkeypatch.chdir(root)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.delenv("MEMORYGUARD_CONTROL_SCOPE", raising=False)

    assert _resolve_memory_workspace({}) == child.resolve()


def test_bare_upgrade_uses_global_data_home_from_unrelated_directory(tmp_path, monkeypatch, capsys):
    root = tmp_path / "tools"
    child = root / "memoryguard"
    data_home = tmp_path / "global-home"
    (root / ".memoryguard").mkdir(parents=True)
    child.mkdir()
    _activate_v2_workspace(child)
    _activate_v2_workspace(data_home)
    monkeypatch.chdir(root)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))

    assert cli_main(["upgrade"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "already_active"
    assert Path(payload["workspace"]) == data_home.resolve()


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
