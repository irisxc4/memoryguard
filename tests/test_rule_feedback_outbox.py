"""Native V2 rule feedback, evidence projection, and durable outbox tests."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom
from memoryguard.rule_binding import build_binding
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


class _Manifest:
    def current(self):
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(
    workspace: Path,
    *,
    agent: str = "agent-a",
    admin: bool = False,
    source: str = "transport",
    project: str = "project-a",
):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"feedback-{agent}-{source}",
            session_source=source,
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=project,
        provider="codex",
        runtime_role="test",
    )


def _setup(workspace: Path):
    GroupControlService(workspace, write=True).bind_agent("agent-a", "group-a")
    store = RuleV2Store(workspace)
    definition = store.upsert_definition(build_definition("Always preserve audit receipts", kind="procedure"))
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id="group-a",
        target_type="agent",
        target_id="agent-a",
        owner_agent_id="agent-a",
        created_by="admin",
        authorization="test",
        binding_id="binding-a",
    ))
    store.record_receipt({
        "receipt_id": "receipt-a",
        "definition_id": definition.definition_id,
        "source_rule_id": "source-rule-a",
        "share_group_id": "group-a",
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "session_id": "feedback-agent-a-transport",
        "task_hash": "task-a",
        "selection_digest": "selection-a",
        "metadata_json": "{}",
        "created_at": "2026-08-10T00:00:00+00:00",
    })
    return NativeV2RuntimePort(workspace, state_provider=_Manifest()), store, definition


def _feedback(port, context, **payload):
    return port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-a", "outcome": "followed", "idempotency_key": "feedback-a", **payload},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )


def test_feedback_writes_outbox_event_atomically(tmp_path):
    port, store, definition = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), evidence="private body")
    assert result["ok"] is True, result
    assert store.list_feedback(receipt_id="receipt-a")[0]["definition_id"] == definition.definition_id
    assert store.get_decision(result["data"]["decision"]["decision_id"]) is not None
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_idempotency_fences").fetchone()[0] == 1
        dump = " ".join(str(value) for row in conn.execute("SELECT * FROM rule_feedback_refs") for value in row)
    assert "private body" not in dump


def test_consume_outbox_projects_followed_to_evidence(tmp_path):
    port, store, definition = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), evidence="receipt proof")
    assert result["data"]["outcome"] == "followed"
    contribution = store.list_evidence_contributions(definition_id=definition.definition_id)[0]
    assert contribution["polarity"] == "positive" and contribution["active"] == 1
    assert store.get_effective_feedback_projection("receipt-a")["outcome"] == "followed"


def test_consume_outbox_violated_is_adherence_not_negative(tmp_path):
    port, store, _ = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), outcome="violated", idempotency_key="violated")
    assert result["data"]["outcome"] == "violated"
    assert store.list_evidence_contributions()[0]["polarity"] == "negative"


def test_consume_outbox_not_applicable_adds_negative_evidence(tmp_path):
    port, store, _ = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), outcome="not_applicable", idempotency_key="not-applicable")
    assert result["ok"] is True
    assert store.list_evidence_contributions()[0]["polarity"] == "negative"


def test_effective_feedback_replace_and_clear_retracts_only_current_receipt(tmp_path):
    port, store, _ = _setup(tmp_path)
    first = _feedback(port, _context(tmp_path), idempotency_key="replace-1")
    assert first["ok"]
    assert store.get_effective_feedback_projection("receipt-a") is not None
    # ``ignored`` is the formal V2 compensating feedback outcome: it is
    # retained in the audit stream but excluded from the effective winner.
    cleared = _feedback(port, _context(tmp_path), outcome="ignored", idempotency_key="replace-clear")
    assert cleared["ok"] is True
    assert store.get_effective_feedback_projection("receipt-a")["outcome"] == "followed"


def test_confidence_zero_is_projected_without_becoming_unknown(tmp_path):
    port, store, _ = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path), confidence=0.0, idempotency_key="confidence-zero")
    row = store.list_evidence_contributions()[0]
    assert result["ok"] and float(row["confidence"]) == 0.0
    assert json.loads(store.list_feedback()[0]["metadata_json"])["confidence"] == 0.0


def test_session_trusted_fails_closed_without_provenance(tmp_path):
    port, store, _ = _setup(tmp_path)
    result = _feedback(port, _context(tmp_path, source="direct"), idempotency_key="untrusted")
    assert result["ok"] is True
    contribution = store.list_evidence_contributions()[0]
    assert json.loads(contribution["metadata_json"])["session_trusted"] is False
    assert store.get_effective_feedback_projection("receipt-a") is None


def test_consume_outbox_is_idempotent(tmp_path):
    port, store, _ = _setup(tmp_path)
    payload = {"receipt_id": "receipt-a", "outcome": "followed", "idempotency_key": "same-feedback"}
    first = port.dispatch_mcp("memoryguard_rule_feedback", payload, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    replay = port.dispatch_mcp("memoryguard_rule_feedback", payload, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    assert first["ok"] and replay["data"]["idempotent_replay"] is True
    assert len(store.list_feedback(receipt_id="receipt-a")) == 1


def test_assignment_change_projects_to_p3(tmp_path):
    _, store, definition = _setup(tmp_path)
    original = store.list_bindings(definition_id=definition.definition_id)[0]
    store.upsert_binding({**original.to_dict(), "status": "inactive", "revision": original.revision + 1})
    store.upsert_binding({
        **original.to_dict(), "binding_id": "binding-p3", "target_type": "agent_project",
        "project_ref": "project-b", "status": "active", "revision": 1,
    })
    active = store.list_bindings(definition_id=definition.definition_id, status="active")
    assert [(item.target_type, item.project_ref) for item in active] == [("agent_project", "project-b")]


def test_two_sources_same_definition_same_owner_update_isolated(tmp_path):
    port, store, definition = _setup(tmp_path)
    store.record_receipt({
        "receipt_id": "receipt-b", "definition_id": definition.definition_id,
        "source_rule_id": "source-rule-b", "share_group_id": "group-a",
        "agent_instance_id": "agent-a", "project_ref": "project-a", "created_at": "2026-08-10T00:00:01+00:00",
    })
    first = _feedback(port, _context(tmp_path), idempotency_key="source-a")
    second = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-b", "outcome": "violated", "idempotency_key": "source-b"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert first["ok"] and second["ok"]
    rows = store.list_evidence_contributions(definition_id=definition.definition_id)
    assert len({row["independence_key"] for row in rows}) == 2


def test_delete_one_source_preserves_other_source_binding(tmp_path):
    port, store, definition = _setup(tmp_path)
    store.record_receipt({
        "receipt_id": "receipt-b", "definition_id": definition.definition_id,
        "source_rule_id": "source-rule-b", "share_group_id": "group-a",
        "agent_instance_id": "agent-a", "project_ref": "project-a", "created_at": "2026-08-10T00:00:01+00:00",
    })
    _feedback(port, _context(tmp_path), idempotency_key="source-a")
    port.dispatch_mcp("memoryguard_rule_feedback", {"receipt_id": "receipt-b", "outcome": "followed", "idempotency_key": "source-b"}, context=_context(tmp_path), generation=7, state="V2_ACTIVE")
    first = next(row for row in store.list_evidence_contributions() if row["receipt_id"] == "receipt-a")
    store.deactivate_evidence_contribution(first["contribution_id"])
    assert any(row["receipt_id"] == "receipt-b" and row["active"] == 1 for row in store.list_evidence_contributions())


def test_delete_source_deactivates_evidence_and_runtime(tmp_path):
    port, store, definition = _setup(tmp_path)
    _feedback(port, _context(tmp_path), idempotency_key="delete-source")
    contribution = store.list_evidence_contributions(definition_id=definition.definition_id)[0]
    assert store.deactivate_evidence_contribution(contribution["contribution_id"])
    assert store.rebuild_evidence_effective(definition_id=definition.definition_id, independence_key=contribution["independence_key"], kind="feedback") is None
    assert store.get_effective_feedback_projection("receipt-a") is not None


def test_delete_last_source_revokes_binding(tmp_path):
    _, store, definition = _setup(tmp_path)
    binding = store.list_bindings(definition_id=definition.definition_id)[0]
    store.upsert_binding({**binding.to_dict(), "status": "inactive", "revision": binding.revision + 1})
    assert store.list_bindings(definition_id=definition.definition_id, status="active") == []


def test_rule_restore_reactivates_only_its_own_contributions(tmp_path):
    port, store, _ = _setup(tmp_path)
    _feedback(port, _context(tmp_path), idempotency_key="restore-source")
    contribution = store.list_evidence_contributions()[0]
    store.deactivate_evidence_contribution(contribution["contribution_id"])
    store.record_evidence_contribution({
        **contribution, "contribution_id": "restored-contribution", "active": 1,
    })
    rows = store.list_evidence_contributions()
    assert {row["active"] for row in rows} == {0, 1}


def test_merged_source_feedback_lands_on_canonical(tmp_path):
    port, store, canonical = _setup(tmp_path)
    source = store.upsert_definition(build_definition("Preserve audit evidence", kind="procedure"))
    store.record_alias(source.definition_id, canonical.definition_id, source_ref="merge-source")
    store.record_receipt({
        "receipt_id": "merged-receipt", "definition_id": canonical.definition_id,
        "source_rule_id": source.definition_id, "share_group_id": "group-a",
        "agent_instance_id": "agent-a", "project_ref": "project-a", "created_at": "2026-08-10T00:00:02+00:00",
    })
    result = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "merged-receipt", "outcome": "followed", "idempotency_key": "merged-feedback"},
        context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] and store.list_feedback(receipt_id="merged-receipt")[0]["definition_id"] == canonical.definition_id


def test_governance_outbox_is_committed_with_v2_atom(tmp_path):
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()), share_group_id="group-a", agent_instance_id="agent-a",
        project_ref="project-a", provider="codex", runtime_role="test", actor="agent-a",
    )
    governance = GovernanceV2(tmp_path)
    atom, decision = governance.put_atom(
        MemoryAtom(memory_id="outbox-atom", body="audit receipt", share_group_id="group-a", agent_instance_id="agent-a", project_ref="project-a"),
        context=context, evidence=[{"source_ref": "outbox-source", "digest": "digest"}], reason="feedback outbox",
    )
    assert atom.atom_id and decision.decision_id
    with governance.memory._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE aggregate_id=?", (atom.atom_id,)).fetchone()[0] >= 1
