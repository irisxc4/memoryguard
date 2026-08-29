"""Grok discovery and stable program identity regressions."""
from __future__ import annotations

from pathlib import Path

from memoryguard.agent_locator import AgentLocator, DetectionContext
from memoryguard.agent_mapping import normalize_program_identity
from memoryguard.agent_profiles import AgentProfileRegistry
from memoryguard.runtime_v2.agent_native import AgentNativeService
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.schema_v3 import AgentInstance, DiscoveryLedger, stable_hash


def test_grok_is_a_builtin_profile_and_home_is_discovered(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.joinpath(".grok").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    registry = AgentProfileRegistry(tmp_path / "workspace")
    profile = registry.get_profile("grok@profile-1")
    assert profile is not None
    assert profile.product == "grok"
    assert profile.verified_product_versions == ["1.0.5"]
    assert any(surface.path_template == "%HOME%/.grok" for surface in profile.surfaces)

    instances, _ledgers = AgentLocator(tmp_path / "workspace").detect_instances()
    assert [instance.product for instance in instances if instance.product == "grok"] == ["grok"]


def test_program_identity_uses_verified_provider_cli_or_mcp_metadata() -> None:
    from_provider = normalize_program_identity("grok", metadata={"account": "acct-a"})
    from_cli = normalize_program_identity("", cli_path=r"C:\Users\user\.grok\bin\grok.exe")
    from_mcp = normalize_program_identity("", mcp_name="grok-mcp")
    assert {row["program_id"] for row in (from_provider, from_cli)} == {"grok"}
    assert from_provider["display_name"] == "Grok"
    assert from_mcp["program_id"] == "unknown"
    assert from_mcp["resolution"] == "unresolved"
    assert from_mcp["provider"] == ""
    assert "mcp_name=grok-mcp" in from_mcp["source_hint"]
    for generic in ("agent", "assistant", "generic-server"):
        unresolved = normalize_program_identity("", mcp_name=generic)
        assert unresolved["program_id"] == "unknown"
        assert unresolved["resolution"] == "unresolved"


def test_native_discovery_exposes_program_identity_and_maps_empty_binding_provider(tmp_path: Path) -> None:
    instance = AgentInstance(
        instance_id="grok-instance",
        profile_id="grok@profile-1",
        product="grok",
        surfaces=[{"status": "found", "resolved_path": str(tmp_path / ".grok")}],
    )

    class FakeLocator:
        context = type("Context", (), {"platform": "windows", "host_id": "test-host"})()
        registry = AgentProfileRegistry(tmp_path)

        def detect_instances(self):
            return [instance], {instance.instance_id: DiscoveryLedger(instance.instance_id, [])}

        def discover_candidates(self, **_kwargs):
            return []

    service = AgentNativeService(tmp_path, locator_factory=lambda _workspace: FakeLocator())
    discovered = service.discover_agents()
    assert discovered["instances"][0]["program_id"] == "grok"
    assert discovered["instances"][0]["display_name"] == "Grok"


def test_grok_profile_is_not_claimed_as_executable_engine(monkeypatch, tmp_path: Path) -> None:
    import memoryguard.host_agent_backend as backend

    grok = tmp_path / "grok.exe"
    grok.write_bytes(b"launcher")
    monkeypatch.setattr(
        backend.shutil,
        "which",
        lambda name: str(grok) if name == "grok" else None,
    )
    monkeypatch.setattr(backend, "_probe_cli_launch", lambda path, *args: Path(path) == grok)
    monkeypatch.setattr(backend, "_find_cursor_agent_cli", lambda: None)
    monkeypatch.setattr(backend, "_find_codex_cli", lambda: None)
    monkeypatch.setattr(backend, "_find_claude_cli", lambda: None)
    monkeypatch.setattr(backend, "_find_trae_cli", lambda: None)

    # The public backend list is an executable enrichment-engine allowlist.
    # Grok has no supported _call_cli protocol here; profile/native discovery
    # must remain the management/UI path instead of inventing a runnable row.
    agents = backend.detect_available_agents()
    assert agents == []


def test_agent_locator_instance_id_is_stable_and_program_scoped(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.joinpath(".grok").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    workspace = tmp_path / "workspace"
    context_a = DetectionContext(
        workspace=str(workspace),
        host_id="host-a",
        config_root=str(home / ".grok" / "accounts" / "acct-a" / "session-1"),
    )
    context_b = DetectionContext(
        workspace=str(workspace),
        host_id="host-a",
        config_root=str(home / ".grok" / "accounts" / "acct-b" / "session-2"),
    )
    locator_a = AgentLocator(workspace, context=context_a)
    locator_b = AgentLocator(workspace, context=context_b)
    grok_a = next(item for item in locator_a.detect_instances()[0] if item.product == "grok")
    grok_b = next(item for item in locator_b.detect_instances()[0] if item.product == "grok")

    # Account/session details live in config_root only and must never become
    # part of the binding key.  The locator key is profile+host+workspace.
    assert grok_a.instance_id == grok_b.instance_id
    grok_profile = locator_a.registry.get_profile("grok@profile-1")
    assert grok_profile is not None
    assert grok_a.instance_id == locator_a._detect_one(grok_profile)[0].instance_id
    assert grok_a.instance_id == stable_hash(
        grok_profile.profile_id, context_a.host_id, str(workspace.resolve())
    )

    cursor_profile = locator_a.registry.get_profile("cursor@profile-1")
    assert cursor_profile is not None
    cursor_id = locator_a._detect_one(cursor_profile)[0].instance_id
    assert cursor_id != grok_a.instance_id

    generic = normalize_program_identity("", mcp_name="generic-server")
    assert generic["program_id"] == "unknown"
    assert generic["resolution"] == "unresolved"
    assert generic["program_id"] not in {"grok", "cursor"}


def test_list_agents_does_not_inherit_binding_across_instances_or_generic(tmp_path: Path) -> None:
    def make_instance(instance_id: str, product: str) -> AgentInstance:
        return AgentInstance(
            instance_id=instance_id,
            profile_id=f"{product}@profile-1",
            product=product,
            surfaces=[{
                "status": "found",
                "surface_id": f"{instance_id}-config",
                "resolved_path": str(tmp_path / instance_id),
                "evidence_role": "control_surface",
            }],
        )

    instances = [
        make_instance("grok-old-instance", "grok"),
        make_instance("grok-new-instance", "grok"),
        make_instance("cursor-new-instance", "cursor"),
        make_instance("generic-new-instance", "assistant"),
    ]
    old_binding = {
        "binding_id": "binding-grok",
        "agent_instance_id": "grok-old-instance",
        "share_group_id": "shared-grok",
        "group_id": "shared-grok",
        "group_kind": "shared",
        "mcp_server_name": "memoryguard",
        "native_memory_mode": "shared_mcp",
        "status": "active",
    }

    class FakeControl:
        def list_bindings(self, **_kwargs):
            return {"bindings": [old_binding]}

        def provider_identity(self, _provider):
            return None

        def selected_source_ids(self, _instance_id):
            return []

    class FakeLocator:
        context = type("Context", (), {"platform": "windows", "host_id": "test-host"})()
        registry = AgentProfileRegistry(tmp_path / "registry")

        def detect_instances(self):
            return instances, {
                item.instance_id: DiscoveryLedger(item.instance_id, [])
                for item in instances
            }

        def discover_candidates(self, **_kwargs):
            return []

    service = AgentNativeService(tmp_path, locator_factory=lambda _workspace: FakeLocator())
    service.control = FakeControl()
    rows = {row["instance_id"]: row for row in service.list_agents()["agents"]}

    assert rows["grok-old-instance"]["binding"]["share_group_id"] == "shared-grok"
    # A different instance remains unbound even when its verified program is
    # the same; account/session change is not a license to guess a binding.
    assert rows["grok-new-instance"]["binding_status"] == "unbound"
    assert "binding" not in rows["grok-new-instance"]
    assert "binding" not in rows["cursor-new-instance"]
    assert "binding" not in rows["generic-new-instance"]


def test_group_preview_projects_canonical_identity_and_keeps_orphan_manageable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A group must expose readable program members, including old bindings."""
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    locator = AgentLocator(tmp_path / "workspace")
    grok = next(item for item in locator.detect_instances()[0] if item.product == "grok")
    service = GroupControlService(tmp_path / "workspace", write=True)
    service.bind_agent(grok.instance_id, "shared-grok", idempotency_key="bind-grok")
    service.bind_agent("legacy-mcp-member", "shared-grok", idempotency_key="bind-legacy")

    preview = service.group_preview("shared-grok")
    by_id = {item["agent_instance_id"]: item for item in preview["member_details"]}

    assert by_id[grok.instance_id]["canonical_program_id"] == "grok"
    assert by_id[grok.instance_id]["display_name"] == "Grok"
    assert by_id[grok.instance_id]["member_status"] == "active_detected"
    assert by_id["legacy-mcp-member"]["display_name"] == "未识别的 MCP 助手"
    assert by_id["legacy-mcp-member"]["member_status"] == "historical_unknown"
    assert by_id["legacy-mcp-member"]["can_unbind"] is True
    assert preview["unresolved_member_count"] == 1
    assert preview["program_members"] == ["grok"]


def test_group_binding_resolves_same_program_after_account_instance_switch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A new account instance can reuse the program's existing group binding."""
    service = GroupControlService(tmp_path / "workspace", write=True)
    service.bind_agent("codex-account-a", "shared-codex", idempotency_key="bind-codex")
    codex_identity = {
        "program_id": "codex",
        "display_name": "Codex",
        "resolution": "verified",
        "source": "current_discovery",
        "source_hint": "codex",
    }
    monkeypatch.setattr(
        service,
        "identity_catalog",
        lambda: {
            "codex-account-a": dict(codex_identity),
            "codex-account-b": dict(codex_identity),
        },
    )

    # The resolver keeps the original binding id for safe unbind/audit while
    # exposing it to a new account instance under the same program identity.
    resolved = service.active_binding_for_agent("codex-account-b")
    assert resolved is not None
    assert resolved["canonical_program_id"] == "codex"
    assert resolved["share_group_id"] == "shared-codex"


def test_provider_registry_collapses_same_program_endpoints_without_guessing_unknown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Canonical/alias ids share one member while every endpoint stays auditable."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    service = GroupControlService(tmp_path / "workspace", write=True)
    service.record_provider_identity(
        "codex",
        "codex-canonical",
        "shared-codex",
        aliases=["codex-router"],
        idempotency_key="provider-codex-registry",
    )
    service.bind_agent("codex-canonical", "shared-codex", idempotency_key="bind-canonical")
    service.bind_agent("codex-router", "shared-codex", idempotency_key="bind-alias")
    service.bind_agent("2aee", "shared-codex", idempotency_key="bind-unknown")

    preview = service.group_preview("shared-codex")
    details = {item["agent_instance_id"]: item for item in preview["member_details"]}
    assert preview["member_count"] == 3
    assert preview["endpoint_member_count"] == 3
    assert preview["program_members"] == ["codex"]
    assert preview["program_member_count"] == 1
    assert preview["unresolved_member_count"] == 1
    assert details["codex-canonical"]["is_canonical_endpoint"] is True
    assert details["codex-canonical"]["is_redundant_endpoint"] is False
    assert details["codex-router"]["is_alias_endpoint"] is True
    assert details["codex-router"]["is_redundant_endpoint"] is True
    assert details["2aee"]["program_id"] == "unknown"
    assert details["2aee"]["can_unbind"] is True
    program = preview["program_member_details"][0]
    assert program["canonical_program_id"] == "codex"
    assert program["display_name"] == "Codex"
    assert set(program["endpoint_ids"]) == {"codex-canonical", "codex-router"}

    resolved = service.active_binding_for_agent("codex-router")
    assert resolved is not None
    assert resolved["agent_instance_id"] == "codex-canonical"
    assert resolved["binding_alias"] is True


def test_provider_registry_cross_group_binding_fails_closed(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path / "workspace", write=True)
    service.record_provider_identity(
        "codex",
        "codex-canonical",
        "shared-codex-a",
        aliases=["codex-router"],
        idempotency_key="provider-codex-cross-group",
    )
    service.bind_agent("codex-canonical", "shared-codex-a", idempotency_key="bind-a")
    service.bind_agent("codex-router", "shared-codex-b", idempotency_key="bind-b")

    import pytest

    with pytest.raises(Exception) as error:
        service.active_binding_for_agent("codex-router")
    assert getattr(error.value, "code", "") == "multiple_active_bindings"


def test_agent_native_lists_one_program_member_and_all_endpoint_audit_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    control = GroupControlService(workspace, write=True)
    control.record_provider_identity(
        "codex",
        "codex-canonical",
        "shared-codex",
        aliases=["codex-router"],
        idempotency_key="provider-codex-native",
    )
    control.bind_agent("codex-canonical", "shared-codex", idempotency_key="native-bind-canonical")
    control.bind_agent("codex-router", "shared-codex", idempotency_key="native-bind-alias")
    (tmp_path / "canonical").mkdir()
    (tmp_path / "router").mkdir()
    instances = [
        AgentInstance(
            "codex-canonical", "codex@profile-1", "codex",
            surfaces=[{"status": "found", "resolved_path": str(tmp_path / "canonical"), "evidence_role": "control_surface"}],
        ),
        AgentInstance(
            "codex-router", "codex@profile-1", "codex",
            surfaces=[{"status": "found", "resolved_path": str(tmp_path / "router"), "evidence_role": "control_surface"}],
        ),
    ]

    class FakeLocator:
        context = type("Context", (), {"platform": "windows", "host_id": "test-host"})()
        registry = AgentProfileRegistry(tmp_path / "registry")

        def detect_instances(self):
            return instances, {
                item.instance_id: DiscoveryLedger(item.instance_id, [])
                for item in instances
            }

        def discover_candidates(self, **_kwargs):
            return []

    native = AgentNativeService(workspace, locator_factory=lambda _workspace: FakeLocator())
    native.control = control
    listed = native.list_agents()
    assert listed["endpoint_member_count"] == 2
    assert listed["program_member_count"] == 1
    assert listed["extra_connection_count"] == 1
    assert listed["unresolved_member_count"] == 0
    assert len(listed["program_members"]) == 1
    program = listed["program_members"][0]
    assert program["canonical_program_id"] == "codex"
    assert program["display_name"] == "Codex"
    assert set(program["endpoint_ids"]) == {"codex-canonical", "codex-router"}
    assert {item["agent_instance_id"] for item in listed["member_details"]} == {
        "codex-canonical", "codex-router",
    }
