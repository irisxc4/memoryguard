from __future__ import annotations

import json
import builtins
import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest
import memoryguard.host_hooks as host_hooks
import memoryguard.mcp_server as mcp_server


@pytest.fixture(autouse=True)
def _isolated_v2_data_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep cutover tests off the operator's live user-level V2 control plane."""
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))


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
    # Runtime control is user-Data-Home based. Tests that need an isolated V2
    # control plane must override MEMORYGUARD_HOME explicitly; WORKSPACE is
    # only a project/migration hint and must not redirect production control.
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
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


def test_v2_mcp_and_hook_dispatch_never_import_or_construct_retired_runtime(
    monkeypatch, tmp_path,
):
    """The live entrypoints must survive exploding retired imports/constructors."""
    retired_modules = {
        "memoryguard.agent_binding",
        "memoryguard.shared_memory_store",
        "memoryguard.conversation_history",
        "memoryguard.source_registry",
        "memoryguard.compat_v2",
    }
    for module_name, class_name in (
        ("memoryguard.agent_binding", "AgentBindingStore"),
        ("memoryguard.shared_memory_store", "SharedMemoryStore"),
        ("memoryguard.conversation_history", "ConversationHistoryStore"),
        ("memoryguard.source_registry", "SourceRegistry"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        constructor = getattr(module, class_name, None)
        if constructor is not None:
            monkeypatch.setattr(
                constructor,
                "__init__",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    AssertionError(f"retired constructor used: {class_name}")
                ),
            )

    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if any(name == module or name.startswith(module + ".") for module in retired_modules):
            raise AssertionError(f"retired import used: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    facade = _Facade("V2_ACTIVE")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group-1", None, None))
    mcp_result = mcp_server.execute_tool(
        "memoryguard_list_sources", {"workspace": str(tmp_path)},
    )
    assert mcp_result.get("isError") is not True
    assert len(facade.mcp_calls) == 1

    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    hook_result = host_hooks.run_hook(
        provider="claude", event="session_start", workspace=tmp_path,
        agent_instance_id="agent-1", share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert hook_result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert len(facade.hook_calls) == 1


def test_real_v2_facade_dispatches_mcp_and_hook_ports(monkeypatch, tmp_path):
    from memoryguard.cutover_v2 import V2RuntimeFacade

    class Manifest:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 9}

    class Native:
        supports_rule_mutation_context = True

        def __init__(self):
            self.mcp_calls = []
            self.hook_calls = []

        def dispatch(self, surface, name, args, **kwargs):
            self.mcp_calls.append((surface, name, dict(args)))
            return {"ok": True, "path": "native", "name": name}

        def bootstrap_hook(self, request, payload, **kwargs):
            self.hook_calls.append((request, dict(payload)))
            return {"packet": {"items": [], "mandatory_items": []}}

    native = Native()
    facade = V2RuntimeFacade(
        manifest=Manifest(), v2=native, hook_v2=native, workspace=str(tmp_path),
    )
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group-1", None, None))
    monkeypatch.setattr(
        mcp_server,
        "_effective_agent_context",
        lambda args, group: {"agent_instance_id": "agent-1", "share_group_id": group},
    )
    mcp_result = mcp_server.execute_tool("memoryguard_list_sources", {})
    assert mcp_result.get("isError") is not True
    assert native.mcp_calls == [("mcp", "memoryguard_list_sources", {})]

    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    hook_result = host_hooks.run_hook(
        provider="claude", event="session_start", workspace=tmp_path,
        agent_instance_id="agent-1", share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert hook_result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert native.hook_calls and native.hook_calls[0][0] == "session_start"


@pytest.mark.parametrize(
    "name",
    [
        "memoryguard_history_search",
        "memoryguard_history_list_sessions",
        "memoryguard_rule_feedback",
        "memoryguard_provider_install",
    ],
)
def test_v2_product_surfaces_route_through_native_mcp_facade(
    monkeypatch, tmp_path, name,
):
    facade = _Facade("V2_ACTIVE")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group-1", None, None))

    @dataclass
    class _Context:
        agent_instance_id: str = "agent-1"
        share_group_id: str = "group-1"
        provider: str = "codex"

    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    args = {"workspace": str(tmp_path)}
    if name == "memoryguard_provider_install":
        args["provider"] = "cursor"
    result = mcp_server.execute_tool(name, args)
    assert result.get("isError") is not True
    assert facade.mcp_calls[-1][0] == name


def test_v2_hook_stop_does_not_invoke_retired_archive_or_feedback_helpers(
    monkeypatch, tmp_path,
):
    facade = _Facade("V2_ACTIVE")
    monkeypatch.setattr(host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade)
    monkeypatch.setattr(
        host_hooks,
        "_archive_history_event",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("retired Hook history archive was reached")
        ),
    )
    monkeypatch.setattr(
        host_hooks,
        "_flush_pending_rule_feedback",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("retired Hook feedback flush was reached")
        ),
    )
    for event in ("user_prompt", "stop"):
        host_hooks.run_hook(
            provider="codex", event=event, workspace=tmp_path,
            agent_instance_id="agent-1", share_group_id="group-1",
            payload={"session_id": "session-1", "prompt": "remember", "loop_count": 0},
        )
    assert [item[0] for item in facade.hook_calls] == ["user_prompt", "stop"]


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


@pytest.mark.parametrize(
    ("state", "code"),
    [("V1_ACTIVE", "v2_upgrade_required"),
     ("V2_BUILDING", "v2_upgrade_required"),
     ("FUTURE", "v2_manifest_state_unavailable")],
)
def test_hook_state_gate_fails_closed_before_native_dispatch(
    monkeypatch, tmp_path, state, code,
):
    facade = _Facade(state)
    monkeypatch.setattr(
        host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade,
    )
    result = host_hooks.run_hook(
        provider="claude", event="session_start", workspace=tmp_path,
        agent_instance_id="agent-1", share_group_id="group-1",
        payload={"session_id": "session-1"},
    )
    assert result["code"] == code
    assert not facade.hook_calls


@pytest.mark.parametrize("provider", ["claude", "codex", "cursor"])
@pytest.mark.parametrize("state", ["V1_ACTIVE", "V2_BUILDING", "FUTURE"])
def test_hook_stop_state_gate_is_fail_open_before_native_dispatch(
    monkeypatch, tmp_path, provider, state,
):
    facade = _Facade(state)
    monkeypatch.setattr(
        host_hooks, "_v2_runtime_facade_factory", lambda workspace: facade,
    )
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_reconcile",
        lambda **_kwargs: {"ok": True},
    )

    result = host_hooks.run_hook(
        provider=provider,
        event="stop",
        workspace=tmp_path,
        agent_instance_id="agent-1",
        share_group_id="group-1",
        payload={"session_id": "session-stop"},
    )

    assert result == {}
    assert not facade.hook_calls


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


def test_retired_v1_source_reads_return_upgrade_required(monkeypatch, tmp_path):
    facade = _Facade("V1_ACTIVE")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    listed = mcp_server.execute_tool("memoryguard_list_sources", {"workspace": str(tmp_path)})
    listed_payload = json.loads(listed["content"][0]["text"])
    assert listed_payload["code"] == "v2_upgrade_required"
    assert not facade.mcp_calls


def test_retired_building_source_reads_return_upgrade_required(monkeypatch, tmp_path):
    facade = _Facade("V2_BUILDING")
    monkeypatch.setattr(mcp_server, "_v2_runtime_facade_factory", lambda workspace: facade)
    result = mcp_server.execute_tool("memoryguard_list_sources", {"workspace": str(tmp_path)})
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["code"] == "v2_upgrade_required"
    assert not facade.mcp_calls


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


def test_public_v2_merge_and_undo_schemas_expose_native_mutation_proof():
    tools = {item["name"]: item for item in mcp_server.TOOLS}
    merge_requirements = {
        "memoryguard_rule_merge_capability_issue": {
            "proposal_id", "mutation_receipt", "idempotency_key", "recovery_secret",
        },
        "memoryguard_rule_merge_approve": {
            "proposal_id", "capability_token", "expected_definition_revisions",
            "mutation_receipt", "idempotency_key",
        },
        "memoryguard_rule_merge_acknowledge": {
            "proposal_id", "capability_token", "mutation_receipt", "idempotency_key",
        },
        "memoryguard_rule_merge_cooldown_clear": {
            "proposal_id", "capability_token", "mutation_receipt", "idempotency_key",
        },
    }
    for name, required in merge_requirements.items():
        schema = tools[name]["inputSchema"]
        assert required <= set(schema["required"])
        assert "include_legacy_fields" not in schema["properties"]
    issue = tools["memoryguard_rule_merge_capability_issue"]["inputSchema"]["properties"]
    assert issue["recovery_secret"]["pattern"] == "^[A-Za-z0-9_-]+$"
    assert "never persisted" in issue["recovery_secret"]["description"]

    undo = tools["memoryguard_rule_undo"]["inputSchema"]
    assert "idempotency_key" in undo["properties"]
    assert "include_legacy_fields" not in undo["properties"]


def test_mcp_merge_secret_validation_is_before_native_and_does_not_reflect_secret(
    monkeypatch, tmp_path,
):
    facade = _Facade("V2_ACTIVE")

    @dataclass
    class _Context:
        agent_instance_id: str = "admin-agent"
        share_group_id: str = "group-1"
        provider: str = "codex"
        project_ref: str = "project-1"
        runtime_role: str = "root"

    monkeypatch.setattr(mcp_server, "_resolve_access", lambda args, workspace: ("group-1", None, None))
    monkeypatch.setattr(mcp_server, "_effective_agent_context", lambda args, group: _Context())
    secret = "this is not base64url"
    rejected = _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_rule_merge_capability_issue",
        {
            "proposal_id": "proposal",
            "mutation_receipt": {"receipt_id": "receipt"},
            "idempotency_key": "issue-key",
            "recovery_secret": secret,
        },
    )
    rejected_payload = json.loads(rejected["content"][0]["text"])
    assert rejected_payload["code"] == "recovery_secret_invalid"
    assert secret not in rejected["content"][0]["text"]
    assert not facade.mcp_calls

    accepted = _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_rule_merge_capability_issue",
        {
            "proposal_id": "proposal",
            "mutation_receipt": {"receipt_id": "receipt"},
            "idempotency_key": "issue-key",
            "recovery_secret": "A" * 43,
        },
    )
    assert accepted.get("isError") is not True
    assert facade.mcp_calls[-1][1]["recovery_secret"] == "A" * 43


def test_mcp_forwards_v2_context_feedback_and_audience_controls(
    monkeypatch, tmp_path,
):
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
        "memoryguard_context_bootstrap",
        {"task": "task", "max_tokens": 256},
    )
    _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_rule_feedback",
        {
            "receipt_id": "receipt",
            "outcome": "not_applicable",
            "evidence": "digestable evidence",
            "confidence": 0.8,
            "idempotency_key": "feedback-retry",
        },
    )
    _mcp_call(
        monkeypatch,
        tmp_path,
        facade,
        "memoryguard_memory_write",
        {
            "memory_id": "audience-memory",
            "body": "v2 body",
            "audience": [{"target_type": "agent", "target_id": "bound-agent"}],
            "idempotency_key": "audience-write",
        },
    )
    calls = {name: args for name, args, _context_value in facade.mcp_calls}
    assert calls["memoryguard_context_bootstrap"]["max_tokens"] == 256
    assert calls["memoryguard_rule_feedback"]["outcome"] == "not_applicable"
    assert calls["memoryguard_rule_feedback"]["evidence"] == "digestable evidence"
    assert calls["memoryguard_rule_feedback"]["idempotency_key"] == "feedback-retry"
    assert calls["memoryguard_memory_write"]["audience"] == [
        {"target_type": "agent", "target_id": "bound-agent"},
    ]


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


def test_runtime_lease_denial_is_protocol_payload_only(monkeypatch, tmp_path):
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
    assert "error" not in result
    assert "restart_required" not in result
    assert "conflicting" not in result
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "runtime_split_brain"
    assert payload["restart_required"] is True
    assert payload["conflicting"] == [{"pid": 4242}]


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
        if name == "memoryguard_memory_merge_safe":
            payload.update(
                {
                    "confirmed": True,
                    "expected_atom_revisions": {"canonical": 1, "duplicate": 1},
                    "mutation_receipt": {"receipt_id": f"receipt-{name}"},
                    "idempotency_key": f"key-{name}",
                }
            )
        elif name == "memoryguard_rule_merge_safe":
            payload.update(
                {
                    "confirmed": True,
                    "expected_definition_revisions": {"canonical": 1, "duplicate": 1},
                    "mutation_receipt": {"receipt_id": f"receipt-{name}"},
                    "idempotency_key": f"key-{name}",
                }
            )
        elif name in mcp_server._V2_RULE_MERGE_TOOLS:
            payload.update(
                {
                    "proposal_id": "proposal",
                    "mutation_receipt": {"receipt_id": f"receipt-{name}"},
                    "idempotency_key": f"key-{name}",
                }
            )
            if name == "memoryguard_rule_merge_capability_issue":
                payload["recovery_secret"] = "A" * 43
            else:
                payload["capability_token"] = "token"
            if name == "memoryguard_rule_merge_approve":
                payload["expected_definition_revisions"] = {"definition": 1}
        _mcp_call(monkeypatch, tmp_path, facade, name, payload)
    assert len(facade.mcp_calls) == len(mcp_server._MUTATING_TOOLS)
    assert all(call[2]["agent_instance_id"] == "bound-agent" for call in facade.mcp_calls)
    assert mcp_server._MUTATING_TOOLS == mcp_server.MCP_MUTATION_NAMES


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
    monkeypatch.setenv("MEMORYGUARD_HOME", str(tmp_path))
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
    from memoryguard.rule_scope import canonical_project_ref
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
    assert write.get("isError") is not True, write
    write_payload = json.loads(write["content"][0]["text"])
    assert write_payload["ok"] is True
    atom = write_payload["data"]["atom"]
    expected_project = canonical_project_ref(str(tmp_path))
    assert atom["agent_instance_id"] == "bound-agent"
    assert atom["share_group_id"] == "bound-group"
    assert atom["project_ref"] == expected_project
    assert atom["agent_instance_id"] != "attacker"
    assert atom["share_group_id"] != "attacker-group"
    assert atom["project_ref"] != "attacker-project"
    assert atom["metadata"]["owner_agent_id"] == "bound-agent"
    assert atom["metadata"]["owner_agent_id"] != "attacker"
    assert atom["provenance"]
    assert {item["agent_instance_id"] for item in atom["provenance"]} == {"bound-agent"}
    assert {item["share_group_id"] for item in atom["provenance"]} == {"bound-group"}
    assert all(item["agent_instance_id"] != "attacker" for item in atom["provenance"])
    assert all(item["share_group_id"] != "attacker-group" for item in atom["provenance"])

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
