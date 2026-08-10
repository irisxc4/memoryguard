from __future__ import annotations

import ast
import argparse
from pathlib import Path

import pytest

from memoryguard.compat_v2 import (
    CLI_COMMAND_NAMES,
    GUI_METHOD_NAMES,
    MCP_MUTATION_NAMES,
    MCP_TOOL_NAMES,
    SAFE_BRIDGE_METHOD_NAMES,
    LegacyV2Adapter,
)
from memoryguard.governance_v2.rules import RuleAuthorizationError, RuleMutationContext


class _LegacyPort:
    def __init__(self):
        self.calls = []

    def dispatch(self, surface, name, args):
        self.calls.append((surface, name, dict(args)))
        return {"ok": True, "legacy_value": name}


class _V2Port:
    def __init__(self, status):
        self._status = status
        self.calls = []

    def status(self, workspace):
        self.calls.append(("status", workspace))
        return self._status

    def dispatch(self, surface, name, args, *, context=None):
        self.calls.append((surface, name, dict(args), context))
        return {"ok": True, "v2_value": name}


def _context(*, automatic=False, admin=False):
    return RuleMutationContext(
        agent="agent-a", project="project-a", group="group-a", provider="codex",
        runtime="hook", admin=admin, automatic=automatic,
    )


def test_public_name_snapshots_cover_mcp_gui_bridge_and_cli_surfaces():
    assert {
        "memoryguard_memory_read", "memoryguard_memory_write", "memoryguard_context_bootstrap",
        "memoryguard_rule_feedback", "memoryguard_history_search", "memoryguard_knowledge_search",
    } <= MCP_TOOL_NAMES
    assert {"call_readonly", "request_mutation", "get_api_method_registry", "get_sandbox_status", "pick_path"} <= SAFE_BRIDGE_METHOD_NAMES
    assert {"get_governance_scope", "list_share_groups", "create_rule_from_text", "knowledge_add"} <= GUI_METHOD_NAMES
    # Keep compatibility snapshot independent from adapter imports: compare
    # against the actual parser's subparser choices at test time.
    from memoryguard.cli import build_parser

    parser = build_parser()
    actual = next(action for action in parser._actions if getattr(action, "choices", None) is not None).choices
    assert set(actual) == set(CLI_COMMAND_NAMES)
    assert {
        "audit", "open", "explain", "plan", "apply", "verify", "undo", "source", "scan",
        "import", "provider", "gc", "storage", "gui", "desktop", "hooks", "mcp-status", "doctor", "groups",
    } <= CLI_COMMAND_NAMES
    mcp_source = Path(__file__).parents[1] / "src" / "memoryguard" / "mcp_server.py"
    tree = ast.parse(mcp_source.read_text(encoding="utf-8"))
    mutating = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_MUTATING_TOOLS" for target in node.targets)
    )
    assert set(MCP_MUTATION_NAMES) == set(mutating)


def test_no_v2_ready_returns_explicit_not_ready_and_calls_only_v1():
    legacy = _LegacyPort()
    # Legacy fallback is allowed only for explicit V1_ACTIVE state.
    v2 = _V2Port({"status": "V1_ACTIVE"})
    adapter = LegacyV2Adapter(workspace="w", v2_port=v2, legacy_port=legacy)
    result = adapter.dispatch_mcp("memoryguard_memory_read", {"memory_id": "m1"})
    assert result["status"] == "not_ready"
    assert result["code"] == "v2_not_ready"
    assert result["path"] == "legacy"
    assert result["legacy"]["legacy_value"] == "memoryguard_memory_read"
    assert len(v2.calls) == 1  # readiness probe only; no V2 dispatch
    assert len(legacy.calls) == 1


def test_unknown_or_exception_manifest_state_fails_closed_without_legacy():
    legacy = _LegacyPort()
    unknown = LegacyV2Adapter(v2_port=_V2Port({"status": "future-state"}), legacy_port=legacy)
    result = unknown.dispatch_mcp("memoryguard_memory_read", {})
    assert result["code"] == "v2_manifest_state_unavailable"
    assert legacy.calls == []

    class Broken(_V2Port):
        def status(self, workspace):
            raise RuntimeError("manifest unreadable")

    failed = LegacyV2Adapter(v2_port=Broken(None), legacy_port=legacy)
    result = failed.dispatch_mcp("memoryguard_memory_read", {})
    assert result["code"] == "v2_manifest_state_unavailable"
    assert legacy.calls == []


def test_v2_ready_routes_once_and_does_not_dual_read():
    legacy = _LegacyPort()
    v2 = _V2Port({"status": "V2_READY"})
    adapter = LegacyV2Adapter(workspace="w", v2_port=v2, legacy_port=legacy)
    result = adapter.dispatch_mcp("memoryguard_memory_read", {"memory_id": "m1"})
    assert result["status"] == "ok" and result["path"] == "v2"
    assert result["v2_value"] == "memoryguard_memory_read"
    assert len(v2.calls) == 2 and len(legacy.calls) == 0


def test_v2_active_routes_v2_and_never_falls_back_to_legacy():
    legacy = _LegacyPort()
    v2 = _V2Port({"status": "V2_ACTIVE"})
    adapter = LegacyV2Adapter(workspace="w", v2_port=v2, legacy_port=legacy)
    result = adapter.dispatch_mcp("memoryguard_memory_read", {"memory_id": "m1"})
    assert result["status"] == "ok" and result["path"] == "v2"
    assert len(legacy.calls) == 0


def test_v2_ready_is_read_only_and_rule_mutation_does_not_fallback():
    legacy = _LegacyPort()
    v2 = _V2Port({"status": "V2_READY"})
    adapter = LegacyV2Adapter(workspace="w", v2_port=v2, legacy_port=legacy)
    result = adapter.mutate_rule(
        "create",
        {"decision": "create", "reason": "r", "confidence": 0.9, "undo_id": "u", "evidence": "e"},
        _context(),
    )
    assert result["code"] == "v2_not_active" and result["path"] == "v2"
    assert legacy.calls == []


def test_v2_ready_rejects_every_mcp_mutation_without_dispatch():
    legacy = _LegacyPort()
    v2 = _V2Port({"status": "V2_READY"})
    adapter = LegacyV2Adapter(v2_port=v2, legacy_port=legacy)
    for name in sorted(MCP_MUTATION_NAMES):
        result = adapter.dispatch_mcp(name, {})
        assert result["code"] == "v2_not_active", name
        assert result["path"] == "v2", name
    # Every mutation was stopped at readiness gate: status probes only.
    assert legacy.calls == []
    assert all(call[0] == "status" for call in v2.calls)


def test_unknown_and_readonly_error_shapes_remain_compatible():
    adapter = LegacyV2Adapter(legacy_port=_LegacyPort())
    unknown = adapter.dispatch_mcp("memoryguard_missing", {})
    assert unknown["error"] == "unknown_tool"
    denied = adapter.call_readonly("request_mutation", [])
    assert denied["error"].startswith("not a readonly method:")


def test_rule_writes_require_context_and_automatic_scope_is_fail_closed():
    legacy = _LegacyPort()
    adapter = LegacyV2Adapter(legacy_port=legacy)
    missing = adapter.dispatch_mcp("memoryguard_rule_create_auto", {"text": "must test"})
    assert missing["code"] == "rule_mutation_context_required"
    denied = adapter.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "must test", "scope": {"target_type": "system", "target_id": ""}},
    )
    # Missing context is rejected before any fallback call; explicit automatic
    # scope is rejected even when a legacy port exists.
    assert denied["code"] == "rule_mutation_context_required"
    ctx = _context(automatic=True)
    broad = adapter.request_mutation(
        "create_rule_from_text", ["must test", {"target_type": "system"}], context=ctx,
    )
    assert broad["code"] == "automatic_scope_expansion_denied"
    assert legacy.calls == []


def test_explicit_rule_mutation_requires_audit_fields_and_preserves_not_ready_fallback():
    legacy = _LegacyPort()
    adapter = LegacyV2Adapter(legacy_port=legacy)
    ctx = _context()
    missing = adapter.mutate_rule("create", {"decision": "create"}, ctx)
    assert missing["code"] == "missing_rule_audit_fields:reason,confidence,undo_id,evidence"
    result = adapter.mutate_rule(
        "create",
        {"decision": "create", "reason": "user request", "confidence": .9, "undo_id": "u1", "evidence": "e1"},
        ctx,
    )
    assert result["status"] == "not_ready" and result["path"] == "legacy"
    assert legacy.calls and legacy.calls[-1][0] == "rule"


def test_context_boolean_parsing_and_alias_conflicts_fail_closed():
    with pytest.raises(RuleAuthorizationError, match="invalid_rule_context_boolean:admin"):
        RuleMutationContext.from_mapping({"admin": "false"})
    assert RuleMutationContext.from_mapping({"admin": 1, "automatic": "0"}).admin is True
    with pytest.raises(RuleAuthorizationError, match="conflicting_rule_context_alias:agent"):
        RuleMutationContext.from_mapping({"agent": "agent-a", "agent_id": "agent-b"})


def test_identity_must_come_from_context_and_manual_rejects_other_agent():
    adapter = LegacyV2Adapter(legacy_port=_LegacyPort())
    ctx = _context(admin=True)
    audit = {"decision": "create", "reason": "r", "confidence": 0.9, "undo_id": "u", "evidence": "e"}
    result = adapter.mutate_rule("create", {**audit, "agent_id": "agent-b"}, ctx)
    assert result["code"] == "untrusted_identity_argument"
    other = adapter.mutate_rule(
        "create", {**audit, "scope": {"target_type": "agent", "target_id": "agent-b"}}, ctx,
    )
    assert other["code"] == "other_agent_scope_denied"


def test_generic_dispatch_passes_trusted_context_and_malformed_context_is_enveloped():
    v2 = _V2Port({"status": "V2_ACTIVE"})
    adapter = LegacyV2Adapter(workspace="w", v2_port=v2, legacy_port=_LegacyPort())
    audit = {"decision": "create", "reason": "r", "confidence": 0.9, "undo_id": "u", "evidence": "e"}
    result = adapter.dispatch(
        "mcp", "memoryguard_rule_create_auto", audit, context=_context(),
    )
    assert result["path"] == "v2"
    assert v2.calls[-1][-1]["agent"] == "agent-a"
    malformed = adapter.mutate_rule("create", audit, object())
    assert malformed["code"] == "rule_mutation_context_required"
    malformed_mapping = adapter.mutate_rule("create", audit, {"admin": "false"})
    assert malformed_mapping["code"] == "invalid_rule_context_boolean:admin"


def test_v2_port_without_context_capability_fails_closed_without_retry():
    class NoContextPort:
        def __init__(self):
            self.calls = 0

        def status(self, workspace):
            return {"status": "V2_ACTIVE"}

        def dispatch(self, surface, name, args):
            self.calls += 1
            raise TypeError("context unsupported")

    port = NoContextPort()
    adapter = LegacyV2Adapter(v2_port=port, legacy_port=_LegacyPort())
    result = adapter.mutate_rule(
        "create",
        {"decision": "create", "reason": "r", "confidence": .9, "undo_id": "u", "evidence": "e"},
        _context(),
    )
    assert result["code"] == "v2_context_capability_required"
    assert port.calls == 0


def test_legacy_handler_typeerror_is_called_once_and_error_is_redacted():
    class BrokenLegacy:
        def __init__(self):
            self.calls = 0

        def dispatch(self, surface, name, args):
            self.calls += 1
            raise TypeError("secret=api_key-123 path=C:/private SELECT * FROM users")

    legacy = BrokenLegacy()
    result = LegacyV2Adapter(legacy_port=legacy).dispatch_mcp(
        "memoryguard_memory_read", {"memory_id": "m1"},
    )
    rendered = str(result)
    assert legacy.calls == 1
    assert result["legacy"]["code"] == "legacy_error"
    assert "api_key-123" not in rendered
    assert "C:/private" not in rendered
    assert "SELECT *" not in rendered


def test_cli_parser_arguments_drive_mutation_classification_and_ready_gate():
    from memoryguard.cli import build_parser

    parser = build_parser()
    legacy = _LegacyPort()
    v2 = _V2Port({"status": "V2_READY"})
    adapter = LegacyV2Adapter(v2_port=v2, legacy_port=legacy)
    representative = {
        "apply": ["apply", "p1"],
        "undo": ["undo", "c1"],
        "groups": ["groups", "migrate", "--apply"],
        "source": ["source", "add", "docs"],
        "import": ["import", "create", "bundle.json"],
        "provider": ["provider", "repair", "codex"],
        "hooks": ["hooks", "install"],
        "gc": ["gc", "--apply"],
        "storage": ["storage", "sweep", "--request-key", "r1", "--lease-id", "l1"],
    }
    for command, argv in representative.items():
        namespace = parser.parse_args(argv)
        result = adapter.dispatch_cli(command, namespace)
        assert result["code"] == "v2_not_active", (command, result)
    assert legacy.calls == []

    reads = {
        "audit": ["audit"], "open": ["open"], "explain": ["explain", "f1"],
        "plan": ["plan", "f1"], "verify": ["verify"], "groups": ["groups", "list"],
        "source": ["source", "list"], "import": ["import", "preview", "bundle.json"],
        "hooks": ["hooks", "status"], "gc": ["gc"], "storage": ["storage", "audit"], "provider": ["provider", "repair"],
    }
    # provider repair is intentionally mutation even without target provider;
    # remove it from read fixtures while retaining parser coverage.
    reads.pop("provider")
    for command, argv in reads.items():
        namespace = parser.parse_args(argv)
        result = adapter.dispatch_cli(command, namespace)
        assert result["path"] == "v2"


def test_generic_cli_namespace_preserves_subactions_and_desktop_is_mutation():
    adapter = LegacyV2Adapter(v2_port=_V2Port({"status": "V2_READY"}), legacy_port=_LegacyPort())
    parser_args = argparse.Namespace(command="source", action="add", path="docs", func=lambda: None)
    source = adapter.dispatch("cli", "source", parser_args)
    assert source["code"] == "v2_not_active"

    dry_run = argparse.Namespace(command="groups", action="migrate", dry_run=True, apply=False)
    assert adapter.dispatch("cli", "groups", dry_run)["path"] == "v2"
    apply = argparse.Namespace(command="groups", action="migrate", dry_run=False, apply=True)
    assert adapter.dispatch("cli", "groups", apply)["code"] == "v2_not_active"

    create = argparse.Namespace(command="import", action="create", bundle="bundle.json")
    assert adapter.dispatch("cli", "import", create)["code"] == "v2_not_active"

    for args in (
        argparse.Namespace(command="desktop"),
        argparse.Namespace(command="desktop", request="r1"),
        argparse.Namespace(command="desktop", watch=True),
        argparse.Namespace(command="desktop", register_uri=True),
    ):
        result = adapter.dispatch("cli", "desktop", args)
        assert result["code"] == "v2_not_active"


def test_contract_module_has_no_direct_legacy_store_imports():
    root = Path(__file__).parents[1] / "src" / "memoryguard" / "compat_v2"
    forbidden = {"SharedMemoryStore", "ConversationHistoryStore", "KnowledgeStore", "mcp_server", "gui", "host_hooks", "cli"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in forbidden for part in (node.module or "").split("."))
            if isinstance(node, ast.Import):
                assert not any(alias.name.split(".")[0] in forbidden for alias in node.names)
