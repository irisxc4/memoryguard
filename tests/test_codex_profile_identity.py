"""Stable Codex program identity across Router account profiles."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest
from memoryguard import toml_compat as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.agent_locator import current_codex_home, discover_codex_homes
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.host_hooks import _best_effort_codex_profile_repair
from memoryguard.memory import MemoryAtomStore
from memoryguard.provider_adapters import repair_global_provider_configs
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.schema_v3 import AgentInstance
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _patch_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEXROUTER_DATA", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_DATA", raising=False)
    monkeypatch.delenv("CODEXROUTER_HOME", raising=False)
    monkeypatch.delenv("CODEX_ROUTER_HOME", raising=False)


def _activate_v2_workspace(workspace: Path) -> None:
    manager = ManifestManager(workspace)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager.transition(ManifestState.V2_BUILDING, migration_id="codex-profile-identity")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="codex-profile-source",
        target_digest="codex-profile-target",
        manifest_digest="codex-profile-manifest",
        digests={"validator_passed": True, "checkpoints": {"codex_profile": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE


def _v2_bind(workspace: Path, agent_id: str, group_id: str) -> dict:
    _activate_v2_workspace(workspace)
    return GroupControlService(workspace, write=True).bind_agent(
        agent_id,
        group_id,
        idempotency_key=f"codex-profile-bind:{agent_id}:{group_id}",
    )


@pytest.fixture(autouse=True)
def _v2_provider_binding_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "memoryguard.provider_adapters._binding_plane_for_workspace",
        lambda _workspace: "v2",
    )


def _make_profile(root: Path, name: str, agent_id: str = "") -> Path:
    home = root / name / "codex-home"
    home.mkdir(parents=True)
    env = ""
    if agent_id:
        env = f'\nenv = {{ MEMORYGUARD_AGENT_ID = "{agent_id}" }}'
    (home / "config.toml").write_text(
        "[mcp_servers.memoryguard]\n"
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]'
        f"{env}\n",
        encoding="utf-8",
    )
    return home


def _env_of(path: Path) -> dict:
    parsed = tomllib.loads((path / "config.toml").read_text(encoding="utf-8"))
    return parsed["mcp_servers"]["memoryguard"]["env"]


def _active_bindings(workspace: Path) -> list[dict]:
    return GroupControlService(workspace).list_bindings(include_inactive=False)["bindings"]


def _managed_codex_hook(workspace: Path, agent_id: str, group_id: str) -> str:
    return json.dumps({
        "hooks": {
            "SessionStart": [{
                "hooks": [{
                    "type": "command",
                    "command": (
                        "python -m memoryguard.host_hooks run "
                        "--provider codex --event session_start "
                        f"--workspace {workspace} "
                        f"--agent-id {agent_id} "
                        f"--share-group-id {group_id} "
                        "--managed-by memoryguard"
                    ),
                }],
            }],
        },
    })


def test_two_codex_homes_reuse_stable_identity_and_repair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    data_home = tmp_path / "data-home"
    router = tmp_path / "codexrouter-data" / "profiles"
    home.mkdir()
    data_home.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.setenv("CODEXROUTER_DATA", str(tmp_path / "codexrouter-data"))
    profile_a = _make_profile(router, "acct-aaaa", agent_id="old-profile-a")
    profile_b = _make_profile(router, "acct-bbbb")
    default_home = home / ".codex"
    default_home.mkdir()
    (default_home / "config.toml").write_text(
        "[mcp_servers.memoryguard]\n"
        'command = "python"\n'
        'args = ["-m", "memoryguard.mcp_server"]\n'
        'env = { MEMORYGUARD_AGENT_ID = "old-default-home" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(profile_a))
    _v2_bind(data_home, "codex-current", "canonical-group")
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([AgentInstance("codex-current", "codex", "codex")], {}),
    )

    discovered = discover_codex_homes()
    assert current_codex_home() == profile_a.resolve()
    assert profile_a.resolve() in discovered
    assert profile_b.resolve() in discovered
    assert default_home.resolve() in discovered

    first = repair_global_provider_configs(["codex"])
    assert first["ok"] is True
    first_env_a = _env_of(profile_a)
    first_hooks_a = (profile_a / "hooks.json").read_text(encoding="utf-8")
    second = repair_global_provider_configs(["codex"])
    assert second["ok"] is True
    assert _env_of(profile_a) == first_env_a
    assert (profile_a / "hooks.json").read_text(encoding="utf-8") == first_hooks_a

    env_a = _env_of(profile_a)
    env_b = _env_of(profile_b)
    canonical_home = str(data_home.resolve())
    for env in (env_a, env_b):
        assert env["MEMORYGUARD_AGENT_ID"] == "codex-current"
        assert env["MEMORYGUARD_WORKSPACE"] == canonical_home
        assert env["MEMORYGUARD_HOME"] == canonical_home
    assert _env_of(default_home)["MEMORYGUARD_AGENT_ID"] == "old-default-home"
    assert "codex-current" in first_hooks_a
    assert "canonical-group" in first_hooks_a
    identity = GroupControlService(data_home).provider_identity("codex")
    assert identity is not None
    assert identity["canonical_id"] == "codex-current"
    assert identity["share_group_id"] == "canonical-group"
    assert "old-profile-a" in identity["aliases"]
    assert "old-default-home" in identity["aliases"]
    assert [item["agent_instance_id"] for item in _active_bindings(data_home)] == [
        "codex-current",
    ]
    assert {item["share_group_id"] for item in _active_bindings(data_home)} == {
        "canonical-group",
    }

    profile_c = _make_profile(router, "acct-cccc", agent_id="old-profile-c")
    monkeypatch.setenv("CODEX_HOME", str(profile_c))
    assert current_codex_home() == profile_c.resolve()
    replica = _best_effort_codex_profile_repair(
        workspace=data_home,
        agent_instance_id="codex-current",
        share_group_id="canonical-group",
    )
    assert replica.get("homes")
    env_c = _env_of(profile_c)
    assert env_c["MEMORYGUARD_AGENT_ID"] == "codex-current"
    assert env_c["MEMORYGUARD_HOME"] == canonical_home
    identity = GroupControlService(data_home).provider_identity("codex")
    assert identity["canonical_id"] == "codex-current"
    assert identity["share_group_id"] == "canonical-group"
    assert "old-profile-a" in identity["aliases"]
    assert "old-default-home" in identity["aliases"]
    assert "old-profile-c" in identity["aliases"]
    assert [item["agent_instance_id"] for item in _active_bindings(data_home)] == [
        "codex-current",
    ]


def test_missing_mcp_identity_reuses_verified_generated_hook_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    data_home = tmp_path / "data-home"
    router_data = tmp_path / "codexrouter-data"
    profile = _make_profile(router_data / "profiles", "acct-hook-only")
    home.mkdir()
    data_home.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(data_home))
    monkeypatch.setenv("CODEX_HOME", str(profile))
    monkeypatch.setenv("CODEXROUTER_DATA", str(router_data))
    _v2_bind(data_home, "codex-hook-stable", "canonical-group")
    (profile / "hooks.json").write_text(
        json.dumps({
            "hooks": {
                "SessionStart": [{
                    "hooks": [{
                        "type": "command",
                        "command": (
                            "python -m memoryguard.host_hooks run "
                            "--provider codex --event session_start "
                            f"--workspace {data_home} "
                            "--agent-id codex-hook-stable "
                            "--share-group-id canonical-group "
                            "--managed-by memoryguard"
                        ),
                    }],
                }],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([], {}),
    )

    result = repair_global_provider_configs(["codex"])

    assert result["ok"] is True
    assert _env_of(profile)["MEMORYGUARD_AGENT_ID"] == "codex-hook-stable"
    identity = GroupControlService(data_home).provider_identity("codex")
    assert identity is not None
    assert identity["canonical_id"] == "codex-hook-stable"
    assert identity["share_group_id"] == "canonical-group"
    assert [item["agent_instance_id"] for item in _active_bindings(data_home)] == [
        "codex-hook-stable",
    ]


def test_repair_uses_unique_verified_v2_hook_home_over_v1_default_and_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    v1_default = tmp_path / "v1-default"
    control = tmp_path / "verified-v2-control"
    router_data = tmp_path / "codexrouter-data"
    home.mkdir()
    v1_default.mkdir()
    control.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(v1_default))
    monkeypatch.setenv("CODEXROUTER_DATA", str(router_data))
    _activate_v2_workspace(control)
    profile_a = _make_profile(router_data / "profiles", "acct-aaaa")
    profile_b = _make_profile(router_data / "profiles", "acct-bbbb")
    duplicate_owned = (
        "[mcp_servers.other]\ncommand = 'keep'\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\n"
        "args = ['-m', 'memoryguard.mcp_server']\n"
        "[mcp_servers.memoryguard.env]\n"
        "MEMORYGUARD_AGENT_ID = 'old-router-agent'\n"
        "MEMORYGUARD_SHARE_GROUP_ID = 'shared-router'\n\n"
        "[mcp_servers.memoryguard]\ncommand = 'python'\n"
        "args = ['-m', 'memoryguard.mcp_server']\n"
        "[mcp_servers.memoryguard.env]\n"
        "MEMORYGUARD_AGENT_ID = 'old-router-agent'\n"
        "MEMORYGUARD_SHARE_GROUP_ID = 'shared-router'\n"
    )
    for profile in (profile_a, profile_b):
        (profile / "config.toml").write_text(duplicate_owned, encoding="utf-8")
        (profile / "hooks.json").write_text(
            _managed_codex_hook(control, "old-router-agent", "shared-router"),
            encoding="utf-8",
        )
    monkeypatch.setenv("CODEX_HOME", str(profile_a))
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([], {}),
    )
    first = repair_global_provider_configs(["codex"])
    assert first["ok"] is True
    assert first["data_home"] == str(control.resolve())
    backups = sorted(
        str(item)
        for profile in (profile_a, profile_b)
        for item in profile.glob("config.toml.memoryguard-provider-*.bak")
    )
    assert len(backups) == 2
    first_bytes = {
        profile: (
            (profile / "config.toml").read_bytes(),
            (profile / "hooks.json").read_bytes(),
        )
        for profile in (profile_a, profile_b)
    }

    second = repair_global_provider_configs(["codex"])
    assert second["ok"] is True
    assert {
        profile: (
            (profile / "config.toml").read_bytes(),
            (profile / "hooks.json").read_bytes(),
        )
        for profile in (profile_a, profile_b)
    } == first_bytes
    assert sorted(
        str(item)
        for profile in (profile_a, profile_b)
        for item in profile.glob("config.toml.memoryguard-provider-*.bak")
    ) == backups
    for profile in (profile_a, profile_b):
        env = _env_of(profile)
        assert env["MEMORYGUARD_AGENT_ID"] == "old-router-agent"
        assert env["MEMORYGUARD_HOME"] == str(control.resolve())
        assert env["MEMORYGUARD_WORKSPACE"] == str(control.resolve())
        assert (profile / "config.toml").read_text(encoding="utf-8").count(
            "[mcp_servers.memoryguard]"
        ) == 1
        assert "old-router-agent" in (profile / "hooks.json").read_text(encoding="utf-8")
        assert "shared-router" in (profile / "hooks.json").read_text(encoding="utf-8")
    assert [
        (item["agent_instance_id"], item["share_group_id"])
        for item in _active_bindings(control)
    ] == [("old-router-agent", "shared-router")]


def test_repair_rejects_ambiguous_verified_v2_control_homes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    v1_default = tmp_path / "v1-default"
    control_a = tmp_path / "control-a"
    control_b = tmp_path / "control-b"
    router_data = tmp_path / "codexrouter-data"
    for path in (home, v1_default, control_a, control_b):
        path.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(v1_default))
    monkeypatch.setenv("CODEXROUTER_DATA", str(router_data))
    _activate_v2_workspace(control_a)
    _activate_v2_workspace(control_b)
    profile_a = _make_profile(router_data / "profiles", "acct-aaaa")
    profile_b = _make_profile(router_data / "profiles", "acct-bbbb")
    (profile_a / "hooks.json").write_text(
        _managed_codex_hook(control_a, "router-agent", "shared-router"),
        encoding="utf-8",
    )
    (profile_b / "hooks.json").write_text(
        _managed_codex_hook(control_b, "router-agent", "shared-router"),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(profile_a))

    with pytest.raises(ValueError, match="verified_v2_control_home_ambiguous"):
        repair_global_provider_configs(["codex"])
    assert not (v1_default / "config.toml").exists()


def test_repair_reports_partial_when_a_router_profile_replica_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    control = tmp_path / "control"
    router_data = tmp_path / "codexrouter-data"
    home.mkdir()
    control.mkdir()
    _patch_home(monkeypatch, home)
    monkeypatch.setenv("MEMORYGUARD_HOME", str(control))
    monkeypatch.setenv("CODEXROUTER_DATA", str(router_data))
    profile = _make_profile(router_data / "profiles", "acct-aaaa", "router-stable")
    monkeypatch.setenv("CODEX_HOME", str(profile))
    _v2_bind(control, "router-stable", "router-group")
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([AgentInstance("router-stable", "codex", "codex")], {}),
    )
    monkeypatch.setattr(
        "memoryguard.provider_adapters._repair_discovered_codex_homes",
        lambda *args, **kwargs: {
            "homes": [], "aliases": [], "warnings": ["acct-bbbb: invalid TOML"],
        },
    )

    result = repair_global_provider_configs(["codex"])

    assert result["ok"] is False
    assert result["partial"] == 1
    assert result["errors"] == 0
    assert result["restart_required"] is True
    assert result["providers"][0]["status"] == "partial"
    assert result["providers"][0]["profile_errors"] == ["acct-bbbb: invalid TOML"]


def test_untrusted_request_identity_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _v2_bind(workspace, "codex-current", "canonical-group")

    ctx = AccessContext(
        trusted_agent_id="codex-current",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="codex-profile-session",
        session_source="host",
        session_trusted=True,
    )
    ok, err = ctx.check_agent("acct-bbbb-impostor")
    assert ok is False
    assert "mismatch" in err
    ok, err = ctx.check_agent("")
    assert ok is True
    ok, err = ctx.check_agent("codex-current")
    assert ok is True

    context = bind_native_transport_context(
        ctx,
        workspace_id=str(workspace.resolve()),
        share_group_id="canonical-group",
        project_ref=str(workspace.resolve()).casefold(),
        provider="codex",
        runtime_role="root",
        entrypoint="mcp",
    )
    port = NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1},
    )
    mismatch = port.dispatch_mcp(
        "memoryguard_memory_status",
        {"agent_instance_id": "acct-bbbb-impostor"},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert mismatch["ok"] is False
    assert mismatch["code"] == "context_identity_spoof"
    trusted = port.dispatch_mcp(
        "memoryguard_memory_status",
        {},
        context=context,
        generation=1,
        state="V2_ACTIVE",
    )
    assert trusted["ok"] is True, trusted
