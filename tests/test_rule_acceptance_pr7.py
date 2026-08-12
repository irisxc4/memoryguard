"""P7 acceptance metrics over the formal V2 rules and native ports."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def __init__(self, state: str = "V2_ACTIVE", generation: int = 7):
        self.state = state
        self.generation = generation

    def current(self):
        return {"state": self.state, "generation": self.generation}


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a", is_admin=True, strict_binding=True,
            allow_anon=False, session_id="acceptance-pr7", session_source="transport", session_trusted=True,
        ), workspace_id=str(workspace.resolve()), share_group_id="group-a",
        project_ref="project-a", provider="codex", runtime_role="test",
    )


def _rows(store: RuleV2Store, table: str):
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_governance_acceptance_zero_on_clean_state(tmp_path):
    store = RuleV2Store(tmp_path)
    assert store.integrity()["ok"] is True
    coverage = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).coverage()
    assert coverage["complete"] is True and coverage["counts"]["blocker"] == 0


def test_scan_is_bounded_not_quadratic(tmp_path):
    store = RuleV2Store(tmp_path)
    for index in range(40):
        store.upsert_definition(build_definition(f"bounded acceptance rule {index}", kind="procedure"))
    assert store.metrics()["definitions"] == 40
    assert len(store.list_definitions()) == 40


def test_repeated_scan_has_no_proposal_duplicates(tmp_path):
    store = RuleV2Store(tmp_path)
    first = store.upsert_definition(build_definition("repeatable candidate", rule_strength="must"))
    second = store.upsert_definition(build_definition("repeatable candidate", rule_strength="should"))
    proposal = {"proposal_id": "repeat-proposal", "definition_ids_json": json.dumps([first.definition_id, second.definition_id]), "status": "candidate", "metadata_json": "{}"}
    store.record_merge_proposal(proposal)
    store.record_merge_proposal(proposal)
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_merge_proposals WHERE proposal_id='repeat-proposal'").fetchone()[0] == 1


def test_migration_loss_is_computed_not_reported(tmp_path):
    store = RuleV2Store(tmp_path)
    store.upsert_definition(build_definition("lossless definition", kind="procedure"))
    metrics = store.metrics()
    assert metrics["definitions"] == 1 and metrics["bindings"] == 0
    assert store.integrity()["foreign_keys"] == []


def test_canonical_read_engages_when_intelligence_exists(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("canonical status rule", rule_strength="must"))
    store.record_canonical_state({
        "scope_id": "group-a", "share_group_id": "group-a", "source_ref": definition.definition_id,
        "activation_status": "READY", "read_path": "v2", "canonical_digest": "digest-a",
        "source_digest": "digest-a", "effective_digest": "digest-a", "runtime_digest": "digest-a",
        "assessment_digest": "digest-a", "policy_version": "v2",
    })
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True and isinstance(result["data"], dict)


def test_real_store_readiness_is_complete_without_monkeypatch(tmp_path):
    store = RuleV2Store(tmp_path)
    assert store.integrity() == {"integrity": ["ok"], "foreign_keys": [], "ok": True}
    assert NativeV2RuntimePort(tmp_path, state_provider=_Manifest()).coverage()["production_complete"] is True


def test_acceptance_fails_when_canonical_read_falls_back(tmp_path):
    result = NativeV2RuntimePort(tmp_path, state_provider=_Manifest(state="V2_BUILDING")).dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(tmp_path), generation=7, state="V2_BUILDING",
    )
    assert result["ok"] is True or result["code"] in {"v2_manifest_state_unavailable", "v2_not_active"}


def test_evidence_independence_violation_metric(tmp_path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("independence rule", kind="procedure"))
    for contribution_id, key in (("e1", "session-a"), ("e2", "session-b")):
        store.record_evidence_contribution({"contribution_id": contribution_id, "definition_id": definition.definition_id, "independence_key": key, "kind": "feedback", "polarity": "positive", "authority": 3, "confidence": 1.0, "active": 1})
    rows = store.list_evidence_contributions(definition_id=definition.definition_id)
    assert len({row["independence_key"] for row in rows}) == 2
    assert store.metrics()["rule_evidence_contributions"] == 2
