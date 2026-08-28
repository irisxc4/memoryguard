"""Provider repair keeps Codex MCP and lifecycle Hooks on one runtime snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import memoryguard.host_hooks as host_hooks
import memoryguard.provider_adapters as provider_adapters
from memoryguard.provider_adapters import (
    CodexAdapter,
    _repair_discovered_codex_homes,
    prepare_provider_mcp_launch,
)
from memoryguard import toml_compat as tomllib


AGENT_ID = "codex-program"
GROUP_ID = "shared-provider-runtime"


def _snapshot_builder(calls: list[tuple[Path, Path]]):
    """Return a deterministic fake builder that publishes only an interpreter."""

    def build(*, snapshot_root: Path, source_root: Path) -> str:
        calls.append((Path(snapshot_root), Path(source_root)))
        python = Path(snapshot_root) / "venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("snapshot interpreter", encoding="utf-8")
        return str(python)

    return build


def _snapshot_launch(
    source_root: Path,
    snapshot_root: Path,
    calls: list[tuple[Path, Path]],
) -> dict:
    return prepare_provider_mcp_launch(
        mutate=True,
        origin={"install_kind": "editable", "install_reason": "test", "editable": True},
        source_root=source_root,
        snapshot_root=snapshot_root,
        builder=_snapshot_builder(calls),
    )


def _managed_codex_handlers(config_home: Path) -> list[dict]:
    data = json.loads((config_home / "hooks.json").read_text(encoding="utf-8"))
    return [
        handler
        for groups in data["hooks"].values()
        for group in groups
        for handler in group.get("hooks", [])
        if host_hooks._is_our_handler(handler)
    ]


def _assert_codex_runtime(config_home: Path, runtime_python: str) -> None:
    """Assert parsed MCP and all seven Hook pairs use exactly one interpreter."""
    config_text = (config_home / "config.toml").read_text(encoding="utf-8")
    parsed = tomllib.loads(config_text)
    mcp = parsed["mcp_servers"]["memoryguard"]
    assert mcp["command"] == runtime_python
    assert mcp["args"] == ["-X", "utf8", "-m", "memoryguard.mcp_server"]

    handlers = _managed_codex_handlers(config_home)
    assert len(handlers) == 7

    # Keep the event-to-command assertion explicit.  It also checks quoting
    # for paths containing spaces on both POSIX and Windows command formats.
    data = json.loads((config_home / "hooks.json").read_text(encoding="utf-8"))
    expected_events = {
        name: event
        for event, name in host_hooks._EVENT_NAMES.items()
        if name in data["hooks"]
    }
    assert set(expected_events) == {
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "Stop",
    }
    workspace = Path(mcp["env"]["MEMORYGUARD_WORKSPACE"])
    for event_name, event in expected_events.items():
        event_handlers = [
            handler
            for handler in data["hooks"][event_name][0]["hooks"]
            if host_hooks._is_our_handler(handler)
        ]
        assert len(event_handlers) == 1
        handler = event_handlers[0]
        assert handler["command"] == host_hooks._command(
            "codex",
            event,
            workspace,
            AGENT_ID,
            GROUP_ID,
            windows=False,
            runtime_python=runtime_python,
        )
        assert handler["commandWindows"] == host_hooks._command(
            "codex",
            event,
            workspace,
            AGENT_ID,
            GROUP_ID,
            windows=True,
            runtime_python=runtime_python,
        )

    serialized = json.dumps(data, ensure_ascii=False)
    assert "python -X utf8" not in serialized
    assert "MEMORYGUARD_RUNTIME_PYTHON" not in serialized
    assert "test-secret-do-not-leak" not in serialized


def test_install_and_repair_share_snapshot_interpreter_and_rotate_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install, idempotent repair, and snapshot rotation never split runtimes."""
    source_root = tmp_path / "source checkout with spaces"
    source_root.mkdir()
    (source_root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    package_file = source_root / "memoryguard" / "runtime.py"
    package_file.parent.mkdir()
    package_file.write_text("VERSION = 'a'\n", encoding="utf-8")
    snapshot_root = tmp_path / "mcp runtime with spaces"
    data_home = tmp_path / "control plane"
    config_home = tmp_path / "Codex profile with spaces"
    config_home.mkdir(parents=True)
    calls: list[tuple[Path, Path]] = []

    monkeypatch.delenv("MEMORYGUARD_RUNTIME_PYTHON", raising=False)
    monkeypatch.setenv("MEMORYGUARD_TEST_SECRET", "test-secret-do-not-leak")
    monkeypatch.setattr(provider_adapters, "_require_provider_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_adapters.CodexAdapter,
        "_require_active_binding",
        lambda _self, *_args: "binding-id",
    )
    monkeypatch.setattr(host_hooks, "_validate_binding", lambda *_args, **_kwargs: None)

    launch_a = _snapshot_launch(source_root, snapshot_root, calls)
    assert launch_a["ok"] is True
    assert launch_a["snapshot"] is True
    runtime_a = str(launch_a["python"])
    assert Path(runtime_a).is_file()
    assert len(calls) == 1

    adapter = CodexAdapter(data_home, config_home=config_home)
    adapter._repair_data_home = data_home
    adapter._repair_codex_homes = ()
    monkeypatch.setattr(provider_adapters, "_prepare_provider_runtime", lambda: launch_a)
    installed = adapter.install(
        data_home,
        share_group_id=GROUP_ID,
        agent_instance_id=AGENT_ID,
        global_scope=True,
    )
    assert installed["configured"] is True
    _assert_codex_runtime(config_home, runtime_a)
    before_repair_config = (config_home / "config.toml").read_bytes()
    before_repair_hooks = (config_home / "hooks.json").read_bytes()

    repaired = _repair_discovered_codex_homes(
        data_home,
        agent_instance_id=AGENT_ID,
        share_group_id=GROUP_ID,
        homes=[config_home],
        runtime_python=runtime_a,
    )
    assert repaired["warnings"] == []
    assert (config_home / "config.toml").read_bytes() == before_repair_config
    assert (config_home / "hooks.json").read_bytes() == before_repair_hooks
    _assert_codex_runtime(config_home, runtime_a)

    # The source content key changes, so the builder publishes a new snapshot.
    package_file.write_text("VERSION = 'b'\n", encoding="utf-8")
    launch_b = _snapshot_launch(source_root, snapshot_root, calls)
    assert launch_b["ok"] is True
    assert launch_b["snapshot"] is True
    runtime_b = str(launch_b["python"])
    assert runtime_b != runtime_a
    assert len(calls) == 2

    rotated = _repair_discovered_codex_homes(
        data_home,
        agent_instance_id=AGENT_ID,
        share_group_id=GROUP_ID,
        homes=[config_home],
        runtime_python=runtime_b,
    )
    assert rotated["warnings"] == []
    _assert_codex_runtime(config_home, runtime_b)
    config_after_rotation = (config_home / "config.toml").read_text(encoding="utf-8")
    hooks_after_rotation = (config_home / "hooks.json").read_text(encoding="utf-8")
    assert runtime_a not in config_after_rotation
    assert runtime_a not in hooks_after_rotation

    # Repeating repair on the new key remains byte-idempotent.
    before_second_rotation = (
        (config_home / "config.toml").read_bytes(),
        (config_home / "hooks.json").read_bytes(),
    )
    repeated = _repair_discovered_codex_homes(
        data_home,
        agent_instance_id=AGENT_ID,
        share_group_id=GROUP_ID,
        homes=[config_home],
        runtime_python=runtime_b,
    )
    assert repeated["warnings"] == []
    assert (
        (config_home / "config.toml").read_bytes(),
        (config_home / "hooks.json").read_bytes(),
    ) == before_second_rotation
    assert len(calls) == 2


def test_noneditable_repair_ignores_stale_snapshot_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wheel repair must rotate both MCP and Hooks off an old snapshot."""
    import sys

    data_home = tmp_path / "control plane"
    config_home = tmp_path / "Codex profile"
    config_home.mkdir(parents=True)
    stale = tmp_path / "old mcp snapshot" / "python.exe"
    stale.parent.mkdir(parents=True)
    stale.write_text("agent-memguard 0.7.3", encoding="utf-8")

    monkeypatch.setenv("MEMORYGUARD_RUNTIME_PYTHON", str(stale))
    monkeypatch.setattr(
        provider_adapters,
        "inspect_distribution_origin",
        lambda: {
            "install_kind": "installed",
            "install_reason": "distribution_installed",
            "editable": False,
        },
    )
    monkeypatch.setattr(provider_adapters, "_require_provider_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        provider_adapters.CodexAdapter,
        "_require_active_binding",
        lambda _self, *_a: "binding-id",
    )
    monkeypatch.setattr(host_hooks, "_validate_binding", lambda *_a, **_k: None)

    adapter = CodexAdapter(data_home, config_home=config_home)
    adapter._repair_data_home = data_home
    adapter._repair_codex_homes = ()
    installed = adapter.install(
        data_home,
        share_group_id=GROUP_ID,
        agent_instance_id=AGENT_ID,
        global_scope=True,
    )

    assert installed["configured"] is True
    _assert_codex_runtime(config_home, sys.executable)
    config_text = (config_home / "config.toml").read_text(encoding="utf-8")
    hooks_text = (config_home / "hooks.json").read_text(encoding="utf-8")
    assert str(stale) not in config_text
    assert str(stale) not in hooks_text
