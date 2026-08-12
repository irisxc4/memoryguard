"""RuleIntelligence V2 migration/upgrade acceptance without legacy store imports."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(workspace: Path, agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent, is_admin=True, strict_binding=True,
            allow_anon=False, session_id=f"migration-{agent}",
            session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role="test",
    )


def _tables(store: RuleV2Store):
    with sqlite3.connect(store.db_path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_backfill_is_lossless_record_to_definition_assignment_to_binding(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("preserve deployment provenance", kind="procedure", rule_strength="must"))
    binding = store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a",
        owner_agent_id="agent-a", binding_id="binding-a", created_by="migration-v2",
    ))
    store.upsert_source_link(
        source_kind="migration", share_group_id="group-a", memory_id="memory-a", source_ref="source-a",
        original_definition_id=definition.definition_id, canonical_definition_id=definition.definition_id,
    )
    assert store.get_definition(definition.definition_id).canonical_text == definition.canonical_text
    assert store.list_bindings(definition_id=definition.definition_id)[0].binding_id == binding.binding_id
    assert "rule_source_links" in _tables(store)


def test_backfill_covers_multiple_groups(tmp_path):
    control = GroupControlService(tmp_path, write=True)
    control.bind_agents(["agent-a", "agent-b"], share_group_id="group-a")
    control.bind_agent("agent-c", "group-b")
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("group scoped rule", rule_strength="must"))
    for group, agent in (("group-a", "agent-a"), ("group-b", "agent-c")):
        store.upsert_binding(build_binding(
            definition.definition_id, share_group_id=group, target_type="agent", target_id=agent,
            owner_agent_id=agent, binding_id=f"binding-{group}",
        ))
    assert {item.share_group_id for item in store.list_bindings(definition_id=definition.definition_id)} == {"group-a", "group-b"}
    assert {item["share_group_id"] for item in control.list_bindings()["bindings"]} >= {"group-a", "group-b"}


def test_backfill_synonym_rules_become_candidates_not_forced_merge(tmp_path):
    store = RuleV2Store(tmp_path)
    left = store.upsert_definition(build_definition("run tests before commit", rule_strength="must"))
    right = store.upsert_definition(build_definition("execute tests before commit", rule_strength="must"))
    store.record_merge_proposal({
        "proposal_id": "migration-candidate", "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate", "metadata_json": json.dumps({"source": "migration", "auto_merge": False}),
    })
    assert len(store.list_definitions()) == 2


def test_iter_legacy_groups_skips_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        RuleV2Store(tmp_path, read_only=True)
    assert list(tmp_path.rglob("*")) == []


def test_migration_script_is_idempotent(tmp_path):
    first = RuleV2Store(tmp_path)
    with sqlite3.connect(first.db_path) as conn:
        marker_before = conn.execute("SELECT version,marker FROM rules_schema_meta WHERE schema_id='rules'").fetchone()
    second = RuleV2Store(tmp_path)
    with sqlite3.connect(second.db_path) as conn:
        marker_after = conn.execute("SELECT version,marker FROM rules_schema_meta WHERE schema_id='rules'").fetchone()
    assert marker_after[:2] == marker_before[:2]
    assert second.db_path == first.db_path


def test_dual_write_syncs_new_rule(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_rule_create_auto", {"text": "record migration decision", "idempotency_key": "migration-create"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    store = RuleV2Store(tmp_path)
    assert store.get_definition(result["data"]["definition_id"]) is not None
    assert store.list_bindings(definition_id=result["data"]["definition_id"])[0].status == "active"
    assert store.get_decision(result["data"]["decision"]["decision_id"]) is not None
