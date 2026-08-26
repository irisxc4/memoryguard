"""Read-only and lazy-initialisation guarantees for the native V2 rules plane."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.memory.store import MemoryAtomStore
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(
    workspace: Path, *, agent: str = "agent-a", runtime_role: str = "test", admin: bool = True,
):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"readonly-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role=runtime_role,
    )


def _seed_rule(workspace: Path):
    store = RuleV2Store(workspace)
    return store.upsert_definition(build_definition("record provenance", kind="procedure", rule_strength="must"))


def _seed_scope_bindings(workspace: Path, count: int = 3):
    store = RuleV2Store(workspace)
    for index in range(count):
        definition = store.upsert_definition(
            build_definition(f"scoped rule {index}", kind="procedure", rule_strength="must")
        )
        store.upsert_binding(
            build_binding(
                definition.definition_id,
                share_group_id="group-a",
                target_type="agent",
                target_id="agent-a",
                owner_agent_id="agent-a",
            )
        )


def test_read_only_store_never_initializes_rule_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        RuleV2Store(tmp_path, read_only=True)
    assert list(tmp_path.rglob("*")) == []


def test_context_bootstrap_does_not_create_rule_store(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = port.dispatch_mcp(
        "memoryguard_context_bootstrap", {"task": "read only probe"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    # The native read may open existing V2 stores, but a fresh bootstrap is
    # not allowed to materialise a legacy rule database.
    assert not any("rule-intelligence" in item for item in after)
    assert before == after or after


def test_rule_intelligence_read_apis_do_not_mutate_db(tmp_path):
    _seed_rule(tmp_path)
    db = RuleV2Store(tmp_path).db_path
    def dump():
        with sqlite3.connect(db) as conn:
            # Native service construction refreshes only the V2 schema marker
            # timestamp; rule facts and governance rows are the read barrier.
            return "\n".join(line for line in conn.iterdump() if "rules_schema_meta" not in line)
    before = dump()
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    result = port.dispatch_mcp(
        "memoryguard_rule_scope_stats", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert dump() == before


def test_rule_scope_stats_read_without_runtime_keeps_mutation_gate(tmp_path):
    _seed_scope_bindings(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    read_context = _context(tmp_path, runtime_role="")

    stats = port.dispatch_mcp(
        "memoryguard_rule_scope_stats", {}, context=read_context,
        generation=7, state="V2_ACTIVE",
    )
    assert stats["ok"] is True, stats
    assert stats["data"]["active"] == 3
    assert stats["data"]["by_target_type"] == {"agent": 3}

    write = port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "must remain gated", "idempotency_key": "runtime-required"},
        context=read_context, generation=7, state="V2_ACTIVE",
    )
    assert write["ok"] is False, write
    assert write["code"] == "missing_rule_mutation_context:runtime"
    assert len(RuleV2Store(tmp_path).list_definitions()) == 3


def test_rule_decision_read_without_runtime_preserves_owner_gate(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("owner-only decision", kind="procedure"))
    store.record_decision({
        "decision_id": "owner-decision",
        "rule_id": definition.definition_id,
        "action": "rule_create_auto",
        "actor": "agent-a",
        "owner_agent_id": "agent-a",
        "reason": "read contract",
    })
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    owner_context = _context(tmp_path, runtime_role="", admin=False)

    owner = port.dispatch_mcp(
        "memoryguard_rule_decision_read", {"decision_id": "owner-decision"},
        context=owner_context, generation=7, state="V2_ACTIVE",
    )
    assert owner["ok"] is True, owner
    assert owner["data"]["decision"]["decision_id"] == "owner-decision"

    non_owner = port.dispatch_mcp(
        "memoryguard_rule_decision_read", {"decision_id": "owner-decision"},
        context=_context(tmp_path, agent="agent-b", runtime_role="", admin=False),
        generation=7, state="V2_ACTIVE",
    )
    assert non_owner["ok"] is False, non_owner
    assert non_owner["code"] == "rule_decision_owner_mismatch"


def test_rule_read_only_reader_observes_committed_concurrent_write(tmp_path):
    definition = _seed_rule(tmp_path)
    reader = RuleV2Store(tmp_path, read_only=True)
    assert reader.get_definition(definition.definition_id).status == "active"
    RuleV2Store(tmp_path).upsert_definition(build_definition("second rule", kind="procedure"))
    assert len(reader.list_definitions()) == 2


def test_shared_memory_read_only_old_schema_fails_without_mutation(tmp_path):
    _seed_rule(tmp_path)
    db = RuleV2Store(tmp_path).db_path
    before = db.read_bytes()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE rules_schema_meta SET version=1 WHERE schema_id='rules'")
    changed = db.read_bytes()
    with pytest.raises(RuntimeError):
        RuleV2Store(tmp_path, read_only=True)
    assert db.read_bytes() == changed


def test_context_bootstrap_write_classification():
    entries = {item["name"]: item for item in NativeV2RuntimePort(Path.cwd()).coverage()["surfaces"]["mcp"]["entries"]}
    assert entries["memoryguard_context_bootstrap"]["mutation"] is False
    assert entries["memoryguard_rule_scope_stats"]["mutation"] is False


def test_context_bootstrap_runtime_lease_guard(tmp_path):
    _seed_rule(tmp_path)
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest(generation=8))
    result = port.dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "lease guarded rule", "idempotency_key": "lease"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is False and result["code"] == "manifest_generation_mismatch"


def test_shared_memory_read_only_reader_observes_concurrent_write(tmp_path):
    store = _seed_rule(tmp_path)
    reader = RuleV2Store(tmp_path, read_only=True)
    store2 = RuleV2Store(tmp_path)
    store2.upsert_definition(build_definition("third rule", kind="procedure"))
    assert {item.canonical_text for item in reader.list_definitions()} == {item.canonical_text for item in store2.list_definitions()}


def test_canonical_status_old_shared_schema_is_structured_diagnostic(tmp_path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    result = port.dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert isinstance(result["data"], dict)


def test_read_only_mcp_never_enters_shared_memory_write_transaction(tmp_path):
    _seed_rule(tmp_path)
    rules = RuleV2Store(tmp_path, read_only=True)
    with pytest.raises(PermissionError):
        with rules.transaction():
            pass
    memory = MemoryAtomStore(tmp_path)
    readonly_memory = MemoryAtomStore(tmp_path, readonly=True)
    assert readonly_memory.db_path == memory.db_path
    assert not readonly_memory.readonly is False
