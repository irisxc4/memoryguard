"""Grok discovery and stable program identity regressions."""
from __future__ import annotations

from pathlib import Path

from memoryguard.agent_locator import AgentLocator, DetectionContext
from memoryguard.agent_mapping import normalize_program_identity
from memoryguard.agent_profiles import AgentProfileRegistry
from memoryguard.runtime_v2.agent_native import AgentNativeService
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
