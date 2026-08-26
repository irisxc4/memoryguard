"""Bare CLI control-home recovery from verified installed Codex profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memoryguard import cli
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2(workspace: Path) -> None:
    manager = ManifestManager(workspace)
    initialize_all(WorkspaceV2Layout(workspace))
    GovernanceV2(
        workspace,
        memory_store=MemoryAtomStore(workspace),
        evidence_store=EvidenceStore(workspace),
    )
    manager.transition(ManifestState.V2_BUILDING, migration_id="stable-control-home")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="stable-control-home-source",
        target_digest="stable-control-home-target",
        manifest_digest="stable-control-home-manifest",
        digests={"validator_passed": True, "checkpoints": {"stable_control_home": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _bind(workspace: Path, agent_id: str, group_id: str = "stable-group") -> None:
    GroupControlService(workspace, write=True).bind_agent(
        agent_id,
        group_id,
        idempotency_key=f"stable-control-home:{agent_id}:{group_id}",
    )


def _profile_config(profile_home: Path, *, agent_id: str, control_home: Path | str,
                    owned: bool = True) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    module = "memoryguard.mcp_server" if owned else "other.module"
    config = {
        "mcp_servers": {
            "memoryguard": {
                "command": "python",
                "args": ["-m", module],
                "env": {
                    "MEMORYGUARD_AGENT_ID": agent_id,
                    "MEMORYGUARD_HOME": str(control_home),
                },
            },
        },
    }
    # TOML is intentionally small and fixture-owned; JSON string escaping is
    # valid TOML basic-string escaping for all generated path values here.
    server = config["mcp_servers"]["memoryguard"]
    (profile_home / "config.toml").write_text(
        "[mcp_servers.memoryguard]\n"
        f"command = {json.dumps(server['command'])}\n"
        f"args = {json.dumps(server['args'])}\n"
        "env = { "
        f"MEMORYGUARD_AGENT_ID = {json.dumps(agent_id)}, "
        f"MEMORYGUARD_HOME = {json.dumps(str(control_home))} "
        "}\n",
        encoding="utf-8",
    )


def _bare_system32_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    default_home = tmp_path / "localappdata" / "MemoryGuard"
    default_home.mkdir(parents=True)
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    monkeypatch.chdir(system32)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    fake_home = tmp_path / "user-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.delenv("MEMORYGUARD_HOME", raising=False)
    monkeypatch.delenv("MEMORYGUARD_WORKSPACE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_DATA", raising=False)
    monkeypatch.delenv("CODEXROUTER_HOME", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)
    return default_home


def _router_profile(root: Path, account: str) -> Path:
    return root / "profiles" / account / "codex-home"


def test_bare_gui_preflight_recovers_one_verified_router_control_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_home = _bare_system32_environment(monkeypatch, tmp_path)
    control_home = tmp_path / "verified-v2"
    _activate_v2(control_home)
    _bind(control_home, "codex-stable")
    router = tmp_path / "localappdata" / "CodexRouter"
    _profile_config(_router_profile(router, "acct-a"), agent_id="codex-stable", control_home=control_home)
    _profile_config(_router_profile(router, "acct-b"), agent_id="codex-stable", control_home=control_home)

    seen: dict[str, Path] = {}

    def preflight(self, name, args, **kwargs):
        del name, args, kwargs
        seen["workspace"] = self.workspace
        return {"ok": False, "status": "blocked", "code": "preflight_only"}

    monkeypatch.setattr(
        "memoryguard.runtime_v2.native_ports.NativeV2RuntimePort.dispatch_cli",
        preflight,
    )

    assert cli.main(["gui"]) == 1
    assert Path(seen["workspace"]).resolve() == control_home.resolve()
    assert default_home != Path(seen["workspace"]).resolve()


def test_explicit_data_home_and_memoryguard_home_outrank_profile_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bare_system32_environment(monkeypatch, tmp_path)
    recovered = tmp_path / "recovered-v2"
    explicit = tmp_path / "explicit-v2"
    env_home = tmp_path / "env-home"
    _activate_v2(recovered)
    _activate_v2(explicit)
    env_home.mkdir()
    _bind(recovered, "codex-stable")
    router = tmp_path / "router"
    monkeypatch.setenv("CODEXROUTER_DATA", str(router))
    _profile_config(_router_profile(router, "acct-a"), agent_id="codex-stable", control_home=recovered)

    monkeypatch.setenv("MEMORYGUARD_HOME", str(env_home))
    explicit_args = cli.build_parser().parse_args(["gui", "--data-home", str(explicit)])
    assert cli._cli_workspace(explicit_args) == explicit.resolve()

    bare_args = cli.build_parser().parse_args(["gui"])
    assert cli._cli_workspace(bare_args) == env_home.resolve()


def test_bare_cli_ignores_cwd_v2_without_trusted_provider_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_home = _bare_system32_environment(monkeypatch, tmp_path)
    nearby_v2 = tmp_path / "nearby-v2"
    _activate_v2(nearby_v2)
    monkeypatch.chdir(nearby_v2)

    assert cli._resolve_gui_workspace([]) == default_home.resolve()
    for argv in (("doctor",), ("mcp-status",), ("open",)):
        assert cli._cli_workspace(cli.build_parser().parse_args(list(argv))) == default_home.resolve()


def test_conflicting_verified_provider_homes_fail_closed_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _bare_system32_environment(monkeypatch, tmp_path)
    first = tmp_path / "first-v2"
    second = tmp_path / "second-v2"
    _activate_v2(first)
    _activate_v2(second)
    _bind(first, "codex-first")
    _bind(second, "codex-second")
    router = tmp_path / "router"
    monkeypatch.setenv("CODEXROUTER_DATA", str(router))
    _profile_config(_router_profile(router, "acct-a"), agent_id="codex-first", control_home=first)
    _profile_config(_router_profile(router, "acct-b"), agent_id="codex-second", control_home=second)

    assert cli.main(["gui"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "stable_control_home_ambiguous"
    assert payload["candidates"] == [str(first.resolve()), str(second.resolve())]


def test_unbound_relative_and_unowned_profiles_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_home = _bare_system32_environment(monkeypatch, tmp_path)
    active = tmp_path / "active-v2"
    unbound = tmp_path / "unbound-v2"
    _activate_v2(active)
    _activate_v2(unbound)
    _bind(active, "different-agent")
    router = tmp_path / "router"
    monkeypatch.setenv("CODEXROUTER_DATA", str(router))
    _profile_config(_router_profile(router, "unbound"), agent_id="unbound-agent", control_home=unbound)
    _profile_config(_router_profile(router, "relative"), agent_id="different-agent", control_home="relative-v2")
    _profile_config(_router_profile(router, "unowned"), agent_id="different-agent", control_home=active, owned=False)

    assert cli._resolve_gui_workspace([]) == default_home.resolve()
