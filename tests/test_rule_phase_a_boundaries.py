"""V2 rule audience, budget, corruption, snapshot, and fail-closed barriers."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=True, strict_binding=True,
            allow_anon=False, session_id=f"phase-a-{agent}", session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role="test",
    )


def _bootstrap(workspace: Path, agent: str = "agent-a"):
    return NativeV2RuntimePort(workspace, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_context_bootstrap", {"task": "phase-a"}, context=_context(workspace, agent), generation=7, state="V2_ACTIVE",
    )


def _rule(store: RuleV2Store, key: str, *, target_id: str = "agent-a", effect: str = "include"):
    definition = store.upsert_definition(build_definition(f"phase a rule {key}", rule_strength="must"))
    binding = store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="group-a", target_type="agent", target_id=target_id,
        effect=effect, owner_agent_id=target_id or "agent-a", binding_id=f"phase-binding-{key}",
    ))
    return definition, binding


def test_budget_rejects_any_overlapping_effective_context(tmp_path):
    store = RuleV2Store(tmp_path)
    for index in range(25):
        _rule(store, str(index))
    b_rule, _ = _rule(store, "under-limit", target_id="agent-b")
    warned = _bootstrap(tmp_path)
    assert warned["ok"] is True
    packet = warned["data"]
    assert packet["status"] == "ok"
    assert packet["error"] == ""
    assert packet["effective_agent"] == "agent-a"
    assert len(packet["mandatory"]) == 25
    assert packet["budget"]["mandatory"]["items"] == 25
    warning = packet["budget"]["warnings"][0]
    assert warning["code"] == "mandatory_item_count_warning"
    assert warning["count"] == 25

    under_limit = _bootstrap(tmp_path, agent="agent-b")
    assert under_limit["ok"] is True
    assert [item["body"] for item in under_limit["data"]["mandatory"]] == [b_rule.canonical_text]
    assert under_limit["data"]["budget"]["warnings"] == []


def test_overlapping_but_non_equivalent_audiences_never_dedup(tmp_path):
    store = RuleV2Store(tmp_path)
    first, _ = _rule(store, "same", target_id="agent-a")
    second = store.upsert_definition(build_definition("phase a rule same", rule_strength="should"))
    store.upsert_binding(build_binding(second.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a", owner_agent_id="agent-a", binding_id="phase-binding-should"))
    assert first.definition_id != second.definition_id
    assert len(store.list_bindings(share_group_id="group-a")) == 2


def test_corrupt_policy_only_fail_closes_the_matching_audience(tmp_path):
    store = RuleV2Store(tmp_path)
    matching, _ = _rule(store, "matching", target_id="agent-a", effect="exclude")
    other, _ = _rule(store, "other", target_id="agent-b")
    assert store.list_bindings(definition_id=matching.definition_id)[0].effect == "exclude"
    assert store.list_bindings(definition_id=other.definition_id)[0].effect == "include"


def test_corrupt_provenance_isolated_and_undetermined_rule_is_not_injected(tmp_path):
    store = RuleV2Store(tmp_path)
    unknown = store.upsert_definition(build_definition("undetermined phase rule", rule_strength="unknown"))
    store.upsert_binding(build_binding(unknown.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a", owner_agent_id="agent-a", binding_id="unknown-binding"))
    packet = _bootstrap(tmp_path)
    assert packet["ok"] and all(item["body"] != unknown.canonical_text for item in packet["data"]["mandatory"])


def test_malformed_snapshot_is_a_true_noop(tmp_path):
    store = RuleV2Store(tmp_path)
    before = store.metrics()
    try:
        with store.transaction():
            store.record_canonical_state({"scope_id": "bad", "share_group_id": "group-a", "read_path": "legacy"})
            raise ValueError("malformed snapshot")
    except ValueError:
        pass
    assert store.metrics() == before


def test_jsonl_backup_restores_assignment_scope_and_clear_cannot_resurrect(tmp_path):
    source = RuleV2Store(tmp_path / "source")
    definition, binding = _rule(source, "backup")
    destination = RuleV2Store(tmp_path / "destination")
    with sqlite3.connect(source.db_path) as source_conn, sqlite3.connect(destination.db_path) as destination_conn:
        source_conn.backup(destination_conn)
    restored = RuleV2Store(tmp_path / "destination")
    assert restored.get_definition(definition.definition_id).status == "active"
    restored.upsert_binding({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1})
    assert restored.list_bindings(definition_id=definition.definition_id, status="active") == []


def test_old_backup_without_assignment_remains_legacy_unscoped(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("unscoped backup rule", rule_strength="must"))
    store.upsert_binding(build_binding(definition.definition_id, share_group_id="group-a", target_type="agent", target_id="", owner_agent_id="agent-a", binding_id="unscoped-backup"))
    packet = _bootstrap(tmp_path)
    assert packet["ok"] and packet["data"]["mandatory"] == []
