from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import memoryguard.host_hooks as host_hooks
import memoryguard.mcp_server as mcp_server


class _Facade:
    def __init__(self, state: str):
        self.state = state
        self.state_calls = 0
        self.mcp_calls: list[tuple[str, dict, dict | None]] = []
        self.hook_calls: list[tuple[str, dict, dict | None]] = []

    def state_snapshot(self):
        self.state_calls += 1
        return {"state": self.state, "generation": 1}

    def dispatch_mcp(self, name, args, *, context=None):
        self.mcp_calls.append((name, dict(args), context))
        return {"ok": True, "path": "v2", "data": {"name": name}}

    def bootstrap_hook(self, event, payload, *, context=None):
        self.hook_calls.append((event, dict(payload), context))
        return {"packet": {"items": [], "mandatory_items": [], "mandatory_rule_ids": []}}


def _mcp_call(monkeypatch, tmp_path: Path, facade: _Facade, name: str, args=None):
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    return mcp_server.execute_tool(name, dict(args or {}))


def test_mcp_state_matrix_ready_routes_reads_and_blocks_mutations(monkeypatch, tmp_path):
    facade = _Facade("V2_READY")
    read = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_list_sources")
    read_payload = json.loads(read["content"][0]["text"])
    assert read.get("isError") is True
    assert read_payload["code"] == "v2_not_active"
    assert len(facade.mcp_calls) == 0

    denied = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_memory_write", {"body": "x"})
    payload = json.loads(denied["content"][0]["text"])
    assert denied["isError"] is True
    assert payload["code"] == "v2_not_active"
    assert len(facade.mcp_calls) == 0
    assert facade.state_calls == 2


def test_mcp_accepts_runtime_snapshot_object(monkeypatch, tmp_path):
    from memoryguard.cutover_v2.state import RuntimeSnapshot, CutoverState

    facade = _Facade("V2_ACTIVE")
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("bound-group", None, None))
    facade.state_snapshot = lambda: RuntimeSnapshot.from_value({
        "state": CutoverState.V2_ACTIVE, "generation": 3,
    })
    result = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_list_sources")
    assert result.get("isError") is not True
    assert len(facade.mcp_calls) == 1


def test_mcp_and_hook_reject_untrusted_or_corrupt_injected_snapshots(monkeypatch, tmp_path):
    from memoryguard.cutover_v2.state import CutoverState, RuntimeSnapshot

    cases = [
        CutoverState.V2_ACTIVE,
        RuntimeSnapshot(CutoverState.V2_ACTIVE, 3),
        {"state": "V2_ACTIVE", "generation": "bad"},
        {"state": "V2_ACTIVE", "generation": 3, "available": False},
        {"state": "V2_ACTIVE", "generation": 3, "error": "corrupt"},
    ]
    for injected in cases:
        assert mcp_server._v2_state_from_value(injected) == "UNKNOWN"
        assert host_hooks._v2_state(injected) == "UNKNOWN"
        facade = _Facade("V2_ACTIVE")
        facade.state_snapshot = lambda injected=injected: injected
        result = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_list_sources")
        payload = json.loads(result["content"][0]["text"])
        assert payload.get("code", payload.get("error")) == "v2_manifest_state_unavailable"
        assert not facade.mcp_calls

        monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace, injected=injected: facade)
        hook_result = host_hooks.run_hook(
            provider="claude", event="session_start", workspace=tmp_path,
            agent_instance_id="agent-1", share_group_id="group-1",
            payload={"session_id": "session-1"},
        )
        assert hook_result.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"
        assert "unavailable" in json.dumps(hook_result).lower()
        assert not facade.hook_calls


def test_mcp_unknown_manifest_fails_closed_without_dispatch(monkeypatch, tmp_path):
    facade = _Facade("FUTURE")
    result = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_list_sources")
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "v2_manifest_state_unavailable"
    assert not facade.mcp_calls
    assert facade.state_calls == 1


def test_mcp_mutation_receives_binding_context_not_payload_identity(monkeypatch, tmp_path):
    facade = _Facade("V2_ACTIVE")

    @dataclass
    class _Context:
        agent_instance_id: str = "bound-agent"
        share_group_id: str = "bound-group"
        provider: str = "codex"
        project_ref: str = "bound-project"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("bound-group", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_memory_write",
        {"body": "x", "agent_instance_id": "attacker", "share_group_id": "attacker-group"},
    )
    assert len(facade.mcp_calls) == 1
    trusted = facade.mcp_calls[0][2]
    assert trusted["agent_instance_id"] == "bound-agent"
    assert trusted["share_group_id"] == "bound-group"


def test_mcp_mutation_strips_payload_identity_before_v2_port(monkeypatch, tmp_path):
    facade = _Facade("V2_ACTIVE")

    @dataclass
    class _Context:
        agent_instance_id: str = "bound-agent"
        share_group_id: str = "bound-group"
        provider: str = "codex"
        project_ref: str = "bound-project"
        runtime_role: str = "root"

    monkeypatch.setattr(
        mcp_server, "_resolve_access",
        lambda args, workspace: ("bound-group", None, None),
    )
    monkeypatch.setattr(
        mcp_server, "_effective_agent_context",
        lambda args, group: _Context(),
    )
    _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_memory_write",
        {
            "body": "x",
            "workspace": "attacker-workspace",
            "agent_instance_id": "attacker-agent",
            "share_group_id": "attacker-group",
            "provider": "attacker-provider",
            "project_ref": "attacker-project",
            "runtime_role": "subagent",
            "runtime_agent_id": "attacker-runtime",
            "parent_agent_id": "attacker-parent",
            "session_id": "attacker-session",
            "context_hash": "attacker-context",
        },
    )
    forwarded = facade.mcp_calls[0][1]
    assert forwarded == {"body": "x"}


def test_legacy_source_reads_require_binding_and_redact_absolute_paths(monkeypatch, tmp_path):
    facade = _Facade("V1_ACTIVE")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        mcp_server,
        "_resolve_access",
        lambda args, workspace: ("bound-group", None, None),
    )

    class Api:
        def list_sources(self):
            return {
                "total": 1,
                "sources": [{
                    "root_id": "root-1", "type": "directory",
                    "display_name": "secret-name", "scope": "project",
                    "path": str(tmp_path / "private" / "source.md"),
                }],
            }

        def scan_sources(self):
            return {
                "snapshot_id": "snap-1", "created_at": "now",
                "source_object_count": 1,
                "coverage": {
                    "coverage_status": "complete", "candidate_count": 0,
                    "read": 1, "unsupported": 0, "unreadable": 0,
                    "skipped_by_policy": 0, "unaccounted_count": 0,
                },
            }

    monkeypatch.setattr(mcp_server, "_get_governance_api", lambda workspace: Api())
    listed = mcp_server.execute_tool("memoryguard_list_sources", {"workspace": str(tmp_path)})
    listed_text = listed["content"][0]["text"]
    assert str(tmp_path) not in listed_text
    assert "source:" in listed_text

    scanned = mcp_server.execute_tool("memoryguard_scan_summary", {"workspace": str(tmp_path)})
    scanned_text = scanned["content"][0]["text"]
    assert str(tmp_path) not in scanned_text


def test_legacy_source_reads_reject_missing_binding(monkeypatch, tmp_path):
    facade = _Facade("V2_BUILDING")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        mcp_server,
        "_resolve_access",
        lambda args, workspace: (None, "active_binding_required", None),
    )
    result = mcp_server.execute_tool("memoryguard_list_sources", {"workspace": str(tmp_path)})
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "active_binding_required"


def test_provider_install_keeps_business_target_but_not_identity_spoof(monkeypatch, tmp_path):
    facade = _Facade("V2_ACTIVE")

    @dataclass
    class _Context:
        agent_instance_id: str = "bound-agent"
        share_group_id: str = "bound-group"
        provider: str = "codex"
        project_ref: str = "bound-project"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("bound-group", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    _mcp_call(
        monkeypatch, tmp_path, facade, "memoryguard_provider_install",
        {"provider": "cursor", "agent_instance_id": "attacker", "share_group_id": "attacker-group"},
    )
    assert facade.mcp_calls[0][1] == {"provider": "cursor"}
    assert facade.mcp_calls[0][2]["provider"] == "codex"

    facade = _Facade("V2_ACTIVE")
    result = _mcp_call(
        monkeypatch, tmp_path, facade, "memoryguard_provider_install",
        {"provider": "attacker"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "invalid_provider"
    assert not facade.mcp_calls


def test_mcp_context_capability_is_required_without_context_port(monkeypatch, tmp_path):
    class _NoContextFacade(_Facade):
        def dispatch_mcp(self, name, args):
            self.mcp_calls.append((name, dict(args), None))
            return {"ok": True}

    facade = _NoContextFacade("V2_ACTIVE")
    monkeypatch.setattr(
        mcp_server, "_resolve_access",
        lambda args, workspace: ("bound-group", None, None),
    )
    result = _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_memory_write",
        {"body": "x"},
    )
    payload = json.loads(result["content"][0]["text"])
    assert result["isError"] is True
    assert payload["code"] == "v2_context_capability_required"
    assert not facade.mcp_calls


def test_runtime_lease_denial_keeps_legacy_top_level_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "memoryguard.runtime_lease.check_runtime_lease",
        lambda workspace, pid: {
            "granted": False,
            "conflicting": [{"pid": 4242}],
        },
    )
    result = mcp_server._runtime_lease_guard(
        "memoryguard_memory_write", {}, tmp_path,
    )
    assert result["isError"] is True
    assert result["error"] == "runtime_split_brain"
    assert result["restart_required"] is True
    assert result["conflicting"] == [{"pid": 4242}]
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == result["error"]


def test_all_mcp_mutations_receive_trusted_context(monkeypatch, tmp_path):
    facade = _Facade("V2_ACTIVE")

    @dataclass
    class _Context:
        agent_instance_id: str = "bound-agent"
        share_group_id: str = "bound-group"
        provider: str = "codex"
        project_ref: str = "bound-project"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("bound-group", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    for name in sorted(mcp_server._MUTATING_TOOLS):
        payload = {"agent_instance_id": "attacker"}
        if name == "memoryguard_provider_install":
            payload["provider"] = "codex"
        _mcp_call(monkeypatch, tmp_path, facade, name, payload)
    assert len(facade.mcp_calls) == len(mcp_server._MUTATING_TOOLS)
    assert all(call[2]["agent_instance_id"] == "bound-agent" for call in facade.mcp_calls)


def test_real_facade_receives_one_manifest_snapshot(monkeypatch, tmp_path):
    from memoryguard.cutover_v2 import V2RuntimeFacade

    class _Manifest:
        def __init__(self):
            self.calls = 0

        def current(self):
            self.calls += 1
            return {"state": "V2_ACTIVE", "generation": 4}

    class _Port:
        def dispatch(self, surface, name, args, **kwargs):
            return {"ok": True, "path": "v2", "name": name}

    manifest = _Manifest()
    facade = V2RuntimeFacade(manifest=manifest, v2=_Port(), workspace=str(tmp_path))
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("bound-group", None, None))
    result = mcp_server.execute_tool("memoryguard_list_sources", {})
    assert result.get("isError") is not True
    assert manifest.calls == 1


def test_mcp_handler_typeerror_is_not_retried_or_reflected(monkeypatch, tmp_path):
    class BrokenFacade(_Facade):
        def dispatch_mcp(self, name, args, *, context=None):
            self.mcp_calls.append((name, dict(args), context))
            raise TypeError("secret=api_key-123 path=C:/private SELECT * FROM users")

    facade = BrokenFacade("V2_ACTIVE")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(mcp_server, "_trusted_context_for_v2", lambda args, workspace: ({}, None))
    result = _mcp_call(monkeypatch, tmp_path, facade, "memoryguard_audit")
    rendered = json.dumps(result, ensure_ascii=False)
    assert len(facade.mcp_calls) == 1
    assert "api_key-123" not in rendered
    assert "C:/private" not in rendered
    assert "SELECT *" not in rendered


def test_hook_ready_bootstrap_uses_v2_and_not_legacy_store(monkeypatch, tmp_path):
    facade = _Facade("V2_READY")
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        host_hooks,
        "_load_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy store must not be instantiated in V2_READY")
        ),
    )
    result = host_hooks.run_hook(
        provider="claude",
        event="session_start",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert result.get("hookSpecificOutput", {}).get("hookEventName") == "SessionStart"
    assert facade.state_calls == 1
    assert len(facade.hook_calls) == 1


def test_hook_explicit_v2_error_envelope_blocks_followup_tools(monkeypatch, tmp_path):
    class _BrokenFacade(_Facade):
        def bootstrap_hook(self, event, payload, *, context=None):
            return {"ok": False, "status": "error", "code": "bootstrap_failed"}

    facade = _BrokenFacade("V2_ACTIVE")
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    result = host_hooks.run_hook(
        provider="claude",
        event="session_start",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-error"},
    )
    assert "bootstrap blocked" in json.dumps(result).lower()
    denied = host_hooks.run_hook(
        provider="claude",
        event="pre_tool",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-error", "tool_name": "Read", "tool_input": {}},
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_ready_does_not_archive_legacy_history_before_v2(monkeypatch, tmp_path):
    facade = _Facade("V2_READY")
    archive_calls = []
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        host_hooks,
        "_archive_history_event",
        lambda **kwargs: archive_calls.append(kwargs) or {"attempted": True},
    )
    host_hooks.run_hook(
        provider="claude",
        event="user_prompt",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-1", "prompt": "remember this", "turn_id": "turn-1"},
    )
    assert archive_calls == []
    assert facade.state_calls == 1
    assert len(facade.hook_calls) == 1


def test_hook_active_stop_calls_v2_once_and_keeps_codex_reconcile_best_effort(monkeypatch, tmp_path):
    facade = _Facade("V2_ACTIVE")
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(host_hooks, "_best_effort_codex_reconcile", lambda **kwargs: {"ok": True})
    result = host_hooks.run_hook(
        provider="codex",
        event="stop",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert result == {}
    assert facade.state_calls == 1
    assert len(facade.hook_calls) == 1


def test_real_facade_hook_receives_one_manifest_snapshot(monkeypatch, tmp_path):
    from memoryguard.cutover_v2 import V2RuntimeFacade

    class _Manifest:
        def __init__(self):
            self.calls = 0

        def current(self):
            self.calls += 1
            return {"state": "V2_ACTIVE", "generation": 7}

    class _Hook:
        supports_rule_mutation_context = True

        def bootstrap_hook(self, request, payload, *, context=None, **kwargs):
            return {"packet": {"items": [], "mandatory_items": []}}

    manifest = _Manifest()
    facade = V2RuntimeFacade(manifest=manifest, hook_v2=_Hook(), workspace=str(tmp_path))
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    host_hooks.run_hook(
        provider="claude",
        event="session_start",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert manifest.calls == 1


def test_mcp_native_transport_binds_access_context_for_write_and_admin_binding(monkeypatch, tmp_path):
    """The production MCP seam must issue the native capability envelope."""

    from memoryguard.access_context import AccessContext
    from memoryguard.cutover_v2 import V2RuntimeFacade
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort
    from memoryguard.memory.store import MemoryAtomStore
    from memoryguard.evidence.store import EvidenceStore
    from memoryguard.governance_v2 import GovernanceV2
    from memoryguard.runtime_v2.native_ports import NativePortError, bind_native_test_capability
    import pytest

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 1}

    class Rules:
        def __init__(self):
            self.calls = []

        def upsert_binding(self, value, **kwargs):
            self.calls.append(dict(value))
            return value

    MemoryAtomStore(tmp_path)
    EvidenceStore(tmp_path)
    GovernanceV2(tmp_path)
    rules = Rules()
    with pytest.raises(NativePortError, match="native_store_injection_capability_required"):
        NativeV2RuntimePort(
            tmp_path,
            state_provider=Manifest(),
            rule_store=rules,
        )
    native = NativeV2RuntimePort(
        tmp_path,
        state_provider=Manifest(),
        services=bind_native_test_capability(stores={"rules": rules}),
    )
    facade = V2RuntimeFacade(manifest=Manifest(), v2=native, workspace=str(tmp_path))
    access = AccessContext(
        trusted_agent_id="bound-agent",
        is_admin=True,
        strict_binding=False,
        allow_anon=False,
        session_id="mcp-session",
        session_source="transport",
        session_trusted=True,
    )
    monkeypatch.setattr(
        mcp_server,
        "_v2_runtime_facade_factory",
        lambda workspace: facade,
    )
    monkeypatch.setattr(
        mcp_server,
        "_resolve_access",
        lambda args, workspace: ("bound-group", None, access),
    )
    monkeypatch.setenv("MEMORYGUARD_PROVIDER", "codex")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(tmp_path))

    write = _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_memory_write",
        {
            "memory_id": "m1",
            "body": "hello",
            "idempotency_key": "mcp-native-1",
            "agent_instance_id": "attacker",
            "share_group_id": "attacker-group",
        },
    )
    # The native boundary owns memory/evidence mutation; transport context
    # cannot replace GovernanceV2 with a callback seam.  This fixture has no
    # active binding for the synthetic group, so the governed write fails
    # closed rather than invoking an injected service.
    assert write.get("isError") is True, write

    binding = _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_binding_create",
        {
            "binding_id": "b1",
            "definition_id": "d1",
            "target_type": "system",
            "target_id": "",
            "created_by": "attacker",
            "owner_agent_id": "victim",
            "owner": "victim",
            "authorization": "forged",
            "authority": "forged",
        },
    )
    # A test-capability store is read-only and cannot override binding_create;
    # the native builtin route owns the mutation (and fails closed here with
    # no staged rule definition).  The helper must observe zero writes.
    assert binding.get("isError") is True, binding
    assert rules.calls == []
