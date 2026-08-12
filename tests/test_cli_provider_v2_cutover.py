from __future__ import annotations

import json

import pytest

from memoryguard.cli import main as cli_main
from memoryguard.provider_adapters import ClaudeAdapter


class _Manifest:
    def __init__(self, state: str) -> None:
        self.state = state
        self.generation = 7

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _set_manifest(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    monkeypatch.setattr(
        "memoryguard.system.manifest.ManifestManager",
        lambda _workspace: _Manifest(state),
    )


@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING"])
def test_cli_requires_public_upgrade_before_any_native_command(
    tmp_path, monkeypatch, capsys, state,
):
    _set_manifest(monkeypatch, state)

    assert cli_main(["doctor", "-w", str(tmp_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "v2_upgrade_required"
    assert payload["state"] == state


def test_cli_unknown_manifest_state_is_fail_closed(tmp_path, monkeypatch, capsys):
    _set_manifest(monkeypatch, "FUTURE_STATE")

    assert cli_main(["doctor", "-w", str(tmp_path)]) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "v2_manifest_state_unavailable"


def test_v2_cli_does_not_construct_legacy_runtime(monkeypatch, tmp_path, capsys):
    _set_manifest(monkeypatch, "V2_ACTIVE")
    from memoryguard.cli import cmd_source
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
    import inspect

    source_command = inspect.getsource(cmd_source)
    assert "SourceControlService" in source_command
    assert "source_registry" not in source_command

    native_cli_calls: list[str] = []
    native_dispatch_cli = NativeV2RuntimePort.dispatch_cli

    def trace_native_cli(self, name, args=None, **kwargs):
        native_cli_calls.append(str(name))
        return native_dispatch_cli(self, name, args, **kwargs)

    # The V2 native CLI dispatch is the executable contract.  Patch that
    # contract point so an accidental compatibility runtime cannot satisfy
    # this test without being observed; no retired module is imported.
    monkeypatch.setattr(NativeV2RuntimePort, "dispatch_cli", trace_native_cli)

    assert cli_main(["doctor", "-w", str(tmp_path)]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ok"] is True

    assert cli_main(["source", "list", "-w", str(tmp_path)]) == 0
    source = json.loads(capsys.readouterr().out)
    assert source["ok"] is True
    assert native_cli_calls == ["doctor", "source"]


@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING"])
def test_provider_binding_read_reports_stable_upgrade_code(tmp_path, monkeypatch, state):
    _set_manifest(monkeypatch, state)

    with pytest.raises(ValueError, match="^v2_upgrade_required$"):
        ClaudeAdapter(tmp_path).status()


def test_provider_binding_unknown_manifest_reports_stable_code(tmp_path, monkeypatch):
    _set_manifest(monkeypatch, "FUTURE_STATE")

    with pytest.raises(ValueError, match="^v2_manifest_state_unavailable$"):
        ClaudeAdapter(tmp_path).status()


def test_provider_binding_write_waits_for_v2_active(tmp_path, monkeypatch):
    _set_manifest(monkeypatch, "V2_READY")

    with pytest.raises(ValueError, match="^v2_not_active$"):
        ClaudeAdapter(tmp_path).install(
            tmp_path,
            share_group_id="group-a",
            agent_instance_id="agent-a",
        )
