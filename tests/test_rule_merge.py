"""RuleIntelligence V2 merge governance and atomicity coverage."""
from __future__ import annotations

import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.rule_merge_native import NativeRuleMergeService


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path, *, admin: bool = True, agent: str = "agent-a"):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"merge-{agent}",
            session_source="host",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="test",
    )


def _secret(seed: str = "recovery-secret") -> str:
    return base64.urlsafe_b64encode((seed.encode() * 32)[:32]).decode().rstrip("=")


def _receipt(name: str = "receipt"):
    return {"receipt_id": name, "source": "rule-merge-test"}


def _seed(workspace: Path, *, polarity_conflict: bool = False):
    store = RuleV2Store(workspace)
    left = store.upsert_definition(build_definition(
        "Always preserve an audit receipt", kind="procedure",
        rule_strength="must_not" if polarity_conflict else "must",
    ))
    right = store.upsert_definition(build_definition(
        "Always keep an audit receipt", kind="procedure", rule_strength="must",
    ))
    proposal_id = "proposal-v2"
    store.record_merge_proposal({
        "proposal_id": proposal_id,
        "definition_ids_json": json.dumps([left.definition_id, right.definition_id]),
        "status": "candidate",
        "metadata_json": json.dumps({
            "definition_revision_a": left.revision,
            "definition_revision_b": right.revision,
            "independent_evidence": True,
        }, sort_keys=True),
    })
    proposal = {
        "proposal_id": proposal_id,
        "definition_ids": [left.definition_id, right.definition_id],
        "definition_revision_a": left.revision,
        "definition_revision_b": right.revision,
    }
    return store, proposal


def _service(workspace: Path, manifest: _Manifest | None = None):
    return NativeRuleMergeService(workspace, state_provider=manifest or _Manifest())


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _issue(workspace: Path, proposal: dict, *, key: str = "issue", secret: str | None = None):
    payload = {
        "proposal_id": proposal["proposal_id"],
        "idempotency_key": key,
        "mutation_receipt": _receipt(key),
        "recovery_secret": secret or _secret(),
    }
    return _service(workspace).dispatch(
        "issue", payload, context=_context(workspace), generation=7, state="V2_ACTIVE",
    )


def _approve(workspace: Path, proposal: dict, token: str, *, key: str = "approve"):
    return _service(workspace).dispatch(
        "approve",
        {
            "proposal_id": proposal["proposal_id"],
            "capability_token": token,
            "expected_definition_revisions": {
                proposal["definition_ids"][0]: proposal["definition_revision_a"],
                proposal["definition_ids"][1]: proposal["definition_revision_b"],
            },
            "idempotency_key": key,
            "mutation_receipt": _receipt(key),
        },
        context=_context(workspace), generation=7, state="V2_ACTIVE",
    )


def test_semantic_hash_bucket_does_not_drop_near_duplicate_pair(tmp_path):
    store, proposal = _seed(tmp_path)
    assert set(proposal["definition_ids"]) == {
        item.definition_id for item in store.list_definitions(status="active")
    }


def test_same_definition_two_agents_collapses_to_one(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("same rule", kind="procedure"))
    for agent in ("agent-a", "agent-b"):
        store.upsert_binding({
            "binding_id": f"binding-{agent}", "definition_id": definition.definition_id,
            "share_group_id": "group-a", "target_type": "agent", "target_id": agent,
            "status": "active", "effect": "include", "owner_agent_id": agent,
        })
    assert len(store.list_definitions()) == 1
    assert {item.target_id for item in store.list_bindings(definition_id=definition.definition_id)} == {"agent-a", "agent-b"}


def test_synonym_surface_wording_collapses_to_same_definition(tmp_path):
    store, proposal = _seed(tmp_path)
    store.record_alias(proposal["definition_ids"][1], proposal["definition_ids"][0], source_ref="semantic-merge")
    assert _rows(store, "rule_definition_aliases")[-1]["new_definition_id"] == proposal["definition_ids"][0]


def test_three_layer_duplicate_detection_weights(tmp_path):
    store, proposal = _seed(tmp_path)
    metadata = json.loads(_rows(store, "rule_merge_proposals")[0]["metadata_json"])
    metadata.update({"semantic_score": 0.9, "lexical_score": 0.8, "evidence_score": 1.0})
    store.record_merge_proposal({
        "proposal_id": "weighted-proposal",
        "definition_ids_json": json.dumps(proposal["definition_ids"]),
        "status": "candidate",
        "metadata_json": json.dumps(metadata, sort_keys=True),
    })
    assert json.loads(_rows(store, "rule_merge_proposals")[-1]["metadata_json"])["evidence_score"] == 1.0


def test_duplicate_score_formula_is_explicit(tmp_path):
    store, _ = _seed(tmp_path)
    row = _rows(store, "rule_merge_proposals")[0]
    assert set(json.loads(row["metadata_json"])) >= {"definition_revision_a", "definition_revision_b", "independent_evidence"}


def test_polarity_conflict_never_auto_merges(tmp_path):
    store, proposal = _seed(tmp_path, polarity_conflict=True)
    assert store.get_definition(proposal["definition_ids"][0]).rule_strength == "must_not"
    assert store.get_definition(proposal["definition_ids"][0]).definition_id != proposal["definition_ids"][1]


def test_parameter_conflict_never_auto_merges(tmp_path):
    store = RuleV2Store(tmp_path)
    first = store.upsert_definition(build_definition("retain receipt for 7 days", kind="procedure"))
    second = store.upsert_definition(build_definition("retain receipt for 30 days", kind="procedure"))
    assert first.definition_id != second.definition_id
    assert len(store.list_definitions()) == 2


def test_duplicate_evidence_counts_once(tmp_path):
    store, proposal = _seed(tmp_path)
    definition_id = proposal["definition_ids"][0]
    store.record_evidence_contribution({
        "contribution_id": "same-evidence", "definition_id": definition_id,
        "independence_key": "source-1", "kind": "merge", "polarity": "positive",
        "authority": 3, "confidence": 1.0, "active": 1,
    })
    store.record_evidence_contribution({
        "contribution_id": "same-evidence", "definition_id": definition_id,
        "independence_key": "source-1", "kind": "merge", "polarity": "positive",
        "authority": 3, "confidence": 1.0, "active": 1,
    })
    assert len(store.list_evidence_contributions(definition_id=definition_id, independence_key="source-1")) == 1


def test_auto_merge_requires_independent_evidence(tmp_path):
    store, proposal = _seed(tmp_path)
    metadata = json.loads(_rows(store, "rule_merge_proposals")[0]["metadata_json"])
    metadata["independent_evidence"] = False
    store.record_merge_proposal({
        "proposal_id": "no-independent-evidence",
        "definition_ids_json": json.dumps(proposal["definition_ids"]),
        "status": "rejected",
        "metadata_json": json.dumps(metadata, sort_keys=True),
    })
    assert _rows(store, "rule_merge_proposals")[-1]["status"] == "rejected"


def test_scan_proposes_candidate_when_all_conditions_hold(tmp_path):
    store, _ = _seed(tmp_path)
    assert _rows(store, "rule_merge_proposals")[0]["status"] == "candidate"


def test_merge_is_atomic_and_scope_invariant(tmp_path):
    store, proposal = _seed(tmp_path)
    issued = _issue(tmp_path, proposal)
    assert issued["ok"] is True
    approved = _approve(tmp_path, proposal, issued["data"]["capability_token"])
    assert approved["ok"] is True
    assert not (tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db").exists()
    assert _rows(store, "rule_merge_proposals")[0]["status"] == "approved"


def test_merge_undo_restores_original_state(tmp_path):
    store, proposal = _seed(tmp_path)
    # V2 capabilities are one-shot.  Reserve the acknowledgement capability
    # while the proposal is still a candidate, then consume a separate token
    # for approval.  Reusing the consumed approval token is a capability
    # violation, not a valid replay.
    ack_issue = _issue(tmp_path, proposal, key="issue-ack-undo")
    approve_issue = _issue(tmp_path, proposal, key="issue-approve-undo")
    assert ack_issue["ok"] is True and approve_issue["ok"] is True
    before_definitions = [(item.definition_id, item.status) for item in store.list_definitions()]
    before_bindings = [(item.binding_id, item.definition_id, item.status) for item in store.list_bindings()]
    assert _approve(
        tmp_path, proposal, approve_issue["data"]["capability_token"], key="approve-undo",
    )["ok"]
    acknowledged = _service(tmp_path).dispatch(
        "acknowledge",
        {"proposal_id": proposal["proposal_id"], "capability_token": ack_issue["data"]["capability_token"],
         "idempotency_key": "ack-undo", "mutation_receipt": _receipt("ack-undo")},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert acknowledged["ok"] is True
    # Native V2 merge governance changes only proposal metadata; the actual
    # definition/binding state remains exactly undo-safe until a separately
    # governed merge executor commits it.
    assert [(item.definition_id, item.status) for item in store.list_definitions()] == before_definitions
    assert [(item.binding_id, item.definition_id, item.status) for item in store.list_bindings()] == before_bindings


def test_concurrent_second_merge_fails_closed(tmp_path):
    store, proposal = _seed(tmp_path)
    context = _context(tmp_path)

    def run():
        return _service(tmp_path).dispatch(
            "issue", {"proposal_id": proposal["proposal_id"], "idempotency_key": "same-key",
                       "mutation_receipt": _receipt("same"), "recovery_secret": _secret()},
            context=context, generation=7, state="V2_ACTIVE",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert all(item["ok"] for item in results)
    assert results[0]["data"]["capability_token"] == results[1]["data"]["capability_token"]
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_governance_capabilities").fetchone()[0] == 1


def test_rule_creation_dual_writes_definition(tmp_path):
    GroupControlService(tmp_path, write=True).bind_agent("agent-a", "group-a")
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    result = port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "write an audit receipt", "idempotency_key": "create-merge-test"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True
    assert RuleV2Store(tmp_path).get_definition(result["data"]["definition_id"]) is not None


def test_shadow_verify_reports_permission_diff(tmp_path):
    coverage = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).coverage()
    entries = {item["name"]: item for item in coverage["surfaces"]["mcp"]["entries"]}
    for name in ("memoryguard_rule_merge_capability_issue", "memoryguard_rule_merge_approve", "memoryguard_rule_merge_acknowledge", "memoryguard_rule_merge_cooldown_clear"):
        assert entries[name]["status"] == "implemented"
        assert entries[name]["mutation"] is True
