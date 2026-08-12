"""P6 V2 rule migration invariants: source links, bindings, evidence, rollback."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import pytest

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _seed(store: RuleV2Store, text: str, strength: str = "must"):
    definition = store.upsert_definition(build_definition(text, kind="procedure", rule_strength=strength))
    binding = store.upsert_binding(build_binding(
        definition.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a",
        owner_agent_id="agent-a", binding_id=f"binding-{definition.definition_id}",
    ))
    return definition, binding


def test_backfill_after_merge_does_not_resurrect_definition(tmp_path):
    store = RuleV2Store(tmp_path)
    source, _ = _seed(store, "source migration rule")
    canonical, _ = _seed(store, "canonical migration rule")
    store.record_alias(source.definition_id, canonical.definition_id, source_ref="merge")
    store.upsert_definition(replace(source, status="inactive", revision=source.revision + 1))
    assert store.get_definition(source.definition_id).status == "inactive"
    assert _rows(store, "rule_definition_aliases")[0]["new_definition_id"] == canonical.definition_id


def test_v1_strength_collision_splits_existing_evidence_by_source(tmp_path):
    store = RuleV2Store(tmp_path)
    must, _ = _seed(store, "same semantic rule", "must")
    should, _ = _seed(store, "same semantic rule", "should")
    for definition, source in ((must, "source-must"), (should, "source-should")):
        store.record_evidence_contribution({
            "contribution_id": source, "definition_id": definition.definition_id,
            "independence_key": source, "kind": "migration", "polarity": "positive", "authority": 3,
            "confidence": 1.0, "active": 1,
        })
    assert {row["definition_id"] for row in _rows(store, "rule_evidence_contributions")} == {must.definition_id, should.definition_id}


def test_v1_collision_routes_binding_runtime_and_source_links_per_source(tmp_path):
    store = RuleV2Store(tmp_path)
    first, _ = _seed(store, "collision rule", "must")
    second, _ = _seed(store, "collision rule", "should")
    for index, definition in enumerate((first, second), 1):
        store.upsert_source_link(
            source_kind="migration", share_group_id="group-a", memory_id=f"memory-{index}", source_ref=f"source-{index}",
            original_definition_id=definition.definition_id, canonical_definition_id=definition.definition_id,
        )
    links = _rows(store, "rule_source_links")
    assert {row["canonical_definition_id"] for row in links} == {first.definition_id, second.definition_id}
    assert {row["definition_id"] for row in _rows(store, "rule_bindings")} == {first.definition_id, second.definition_id}


def test_v1_backfill_fault_rolls_back_definitions_bindings_evidence_and_links(tmp_path):
    store = RuleV2Store(tmp_path)
    before = {table: len(_rows(store, table)) for table in ("rule_definitions", "rule_bindings", "rule_evidence_contributions", "rule_source_links")}
    with pytest.raises(RuntimeError):
        with store.transaction():
            definition = store.upsert_definition(build_definition("faulted migration", kind="procedure"))
            store.upsert_binding(build_binding(definition.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a", binding_id="fault-binding"))
            store.record_evidence_contribution({"contribution_id": "fault-evidence", "definition_id": definition.definition_id, "independence_key": "fault", "active": 1})
            store.upsert_source_link(source_kind="migration", share_group_id="group-a", memory_id="fault", source_ref="fault", original_definition_id=definition.definition_id, canonical_definition_id=definition.definition_id)
            raise RuntimeError("backfill failure")
    assert {table: len(_rows(store, table)) for table in before} == before


def test_sync_after_merge_targets_canonical_definition(tmp_path):
    store = RuleV2Store(tmp_path)
    source, _ = _seed(store, "source rule")
    canonical, _ = _seed(store, "canonical rule")
    store.record_alias(source.definition_id, canonical.definition_id, source_ref="sync")
    store.record_receipt({"receipt_id": "sync-receipt", "definition_id": canonical.definition_id, "source_rule_id": source.definition_id, "share_group_id": "group-a", "agent_instance_id": "agent-a"})
    assert store.get_receipt("sync-receipt")["definition_id"] == canonical.definition_id


def test_backfill_preserves_manual_system_and_group_bindings(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("binding classes", rule_strength="must"))
    for target_type, target_id, created_by in (("agent", "agent-a", "manual"), ("system", "system", "manual"), ("group", "group-a", "manual")):
        store.upsert_binding(build_binding(definition.definition_id, share_group_id="group-a", target_type=target_type, target_id=target_id, owner_agent_id="agent-a", created_by=created_by, binding_id=f"binding-{target_type}"))
    assert {item.target_type for item in store.list_bindings(definition_id=definition.definition_id)} == {"agent", "system", "group"}


def test_backfill_skips_non_rule_relevant_memories(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("only formal rules are migrated", kind="procedure"))
    assert len(store.list_definitions()) == 1
    assert definition.rule_kind == "procedure"


def test_strength_evolution_moves_bindings_atomically(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed(store, "evolving rule", "should")
    evolved = build_definition("evolving rule", kind="procedure", rule_strength="must")
    with store.transaction():
        store.upsert_definition(replace(definition, status="inactive", revision=definition.revision + 1))
        store.upsert_definition(evolved)
        store.upsert_binding({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1})
        store.upsert_binding({**binding.to_dict(), "binding_id": "binding-evolved", "definition_id": evolved.definition_id, "revision": 1})
    assert store.get_definition(evolved.definition_id).rule_strength == "must"
    assert store.list_bindings(definition_id=evolved.definition_id)[0].revision == 1


def test_strength_evolution_failure_rolls_back_all_state(tmp_path):
    store = RuleV2Store(tmp_path)
    definition, binding = _seed(store, "rollback evolution", "should")
    evolved = build_definition("rollback evolution", kind="procedure", rule_strength="must")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert_definition(replace(definition, status="inactive", revision=definition.revision + 1))
            store.upsert_definition(evolved)
            store.upsert_binding({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1})
            raise RuntimeError("strength update failed")
    assert store.get_definition(definition.definition_id).rule_strength == "should"
    assert store.list_bindings(definition_id=definition.definition_id)[0].revision == binding.revision
