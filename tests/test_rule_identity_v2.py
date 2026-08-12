"""RuleDefinition identity and strength gates on the canonical V2 rules DB."""
from __future__ import annotations

import json
import sqlite3

from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_must_and_should_never_share_definition_id():
    must = build_definition("always run tests before commit", kind="procedure", rule_strength="must")
    should = build_definition("always run tests before commit", kind="procedure", rule_strength="should")
    assert must.definition_id != should.definition_id
    assert must.semantic_hash == should.semantic_hash


def test_english_must_not_has_negative_polarity():
    definition = build_definition("must not commit untested code", kind="procedure", rule_strength="must")
    assert definition.polarity == "negative"
    assert definition.rule_strength == "must"


def test_strength_conflict_surfaces_after_v2_identity(tmp_path):
    store = RuleV2Store(tmp_path)
    must = store.upsert_definition(build_definition("run tests before commit", rule_strength="must"))
    should = store.upsert_definition(build_definition("run tests before commit", rule_strength="should"))
    assert must.definition_id != should.definition_id
    assert {item.rule_strength for item in store.list_definitions()} == {"must", "should"}


def test_v2_definition_round_trip_preserves_binding_and_source_link(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("record deployment provenance", kind="procedure", rule_strength="must"))
    binding = build_binding(
        definition.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a",
        owner_agent_id="agent-a", binding_id="binding-a", created_by="native-v2",
    )
    store.upsert_binding(binding)
    store.upsert_source_link(
        source_link_id="source-link-a", source_kind="native", share_group_id="group-a",
        memory_id="source-memory-a", source_ref="receipt-a",
        original_definition_id=definition.definition_id,
        canonical_definition_id=definition.definition_id,
    )
    reopened = RuleV2Store(tmp_path)
    assert reopened.get_definition(definition.definition_id).to_dict() == definition.to_dict()
    assert reopened.list_bindings(definition_id=definition.definition_id)[0].binding_id == "binding-a"
    source_link = _rows(reopened, "rule_source_links")[0]
    assert source_link["original_definition_id"] == definition.definition_id
    assert source_link["canonical_definition_id"] == definition.definition_id


def test_orphan_v2_definition_marked_unknown(tmp_path):
    store = RuleV2Store(tmp_path)
    orphan = store.upsert_definition(build_definition("unrecoverable rule strength", rule_strength="unknown"))
    store.upsert_binding(build_binding(
        orphan.definition_id, share_group_id="group-a", target_type="agent", target_id="agent-a",
        owner_agent_id="agent-a", binding_id="orphan-binding",
    ))
    assert store.get_definition(orphan.definition_id).rule_strength == "unknown"
    assert store.get_definition(orphan.definition_id).status == "active"


def test_unknown_strength_never_auto_merges(tmp_path):
    store = RuleV2Store(tmp_path)
    left = store.upsert_definition(build_definition("retain provenance", rule_strength="unknown"))
    right = store.upsert_definition(build_definition("retain provenance", rule_strength="unknown"))
    store.record_merge_proposal({
        "proposal_id": "unknown-proposal", "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "blocked", "metadata_json": json.dumps({"merge_block": "unknown_strength"}),
    })
    assert _rows(store, "rule_merge_proposals")[0]["status"] == "blocked"


def test_human_cannot_merge_unknown_strength(tmp_path):
    store = RuleV2Store(tmp_path)
    left = store.upsert_definition(build_definition("retain provenance", rule_strength="unknown"))
    right = store.upsert_definition(build_definition("retain provenance", rule_strength="must"))
    store.record_decision({
        "decision_id": "unknown-merge-denied", "rule_id": left.definition_id,
        "action": "rule_merge_rejected", "reason": "unknown_strength_requires_review",
        "metadata_json": json.dumps({"other_definition_id": right.definition_id}),
    })
    decision = store.get_decision("unknown-merge-denied")
    assert decision["action"] == "rule_merge_rejected"
    assert store.get_definition(left.definition_id).status == "active"
    assert store.get_definition(right.definition_id).status == "active"
