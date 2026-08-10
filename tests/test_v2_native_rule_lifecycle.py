from __future__ import annotations

import hashlib
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


def _context(workspace: Path, *, agent: str = "agent-a", admin: bool = False):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=admin,
            strict_binding=True,
            allow_anon=False,
            session_id=f"session-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id="shared-a",
        project_ref="project-a",
        provider="codex",
        runtime_role="root",
    )


def _data(result):
    assert result["ok"] is True, result
    return result["data"]


def test_native_rule_create_is_v2_scoped_idempotent_and_compensating_undo(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    created = _data(port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "Always preserve audit receipts", "priority": 9, "idempotency_key": "create-1"},
        context=context,
        generation=7,
    ))
    replay = _data(port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "Always preserve audit receipts", "priority": 9, "idempotency_key": "create-1"},
        context=context,
        generation=7,
    ))
    assert replay["idempotent_replay"] is True
    assert replay["decision"]["decision_id"] == created["decision"]["decision_id"]

    store = RuleV2Store(tmp_path)
    definition = store.get_definition(created["definition_id"])
    assert definition is not None and definition.status == "active"
    binding = next(item for item in store.list_bindings(definition_id=created["definition_id"]) if item.binding_id == created["binding_id"])
    assert binding.target_type == "agent"
    assert binding.target_id == "agent-a"
    assert binding.share_group_id == "shared-a"
    assert binding.owner_agent_id == "agent-a"
    assert binding.priority == 9

    undone = _data(port.dispatch_mcp(
        "memoryguard_rule_undo",
        {"undo_id": created["undo_id"], "idempotency_key": "undo-1"},
        context=context,
        generation=7,
    ))
    assert undone["compensation"]["binding_status"] == "inactive"
    definition_after = store.get_definition(created["definition_id"])
    assert definition_after is not None and definition_after.status == "inactive"
    binding_after = next(item for item in store.list_bindings(definition_id=created["definition_id"]) if item.binding_id == created["binding_id"])
    assert binding_after.status == "inactive"
    # Immutable history remains: create + compensating undo.
    assert store.get_decision(created["decision"]["decision_id"]) is not None
    assert store.get_decision(undone["decision"]["decision_id"]) is not None


def test_native_rule_create_rejects_scope_spoof_and_broad_auto_scope(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    other = port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "Always do X", "scope": {"target_type": "agent", "target_id": "agent-b"}},
        context=context,
        generation=7,
    )
    assert other["ok"] is False
    assert other["code"] == "other_agent_scope_denied"
    group = port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "Always do X", "scope": {"target_type": "group", "target_id": "shared-a"}},
        context=context,
        generation=7,
    )
    assert group["ok"] is False
    assert group["code"] == "automatic_scope_expansion_denied"


def test_native_rule_feedback_requires_owned_receipt_and_never_persists_raw_evidence(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("Always cite the source", kind="procedure"))
    store.record_receipt({
        "receipt_id": "receipt-a",
        "definition_id": definition.definition_id,
        "source_rule_id": "source-rule",
        "share_group_id": "shared-a",
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "session_id": "session-agent-a",
        "task_hash": "task",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-10T00:00:00+00:00",
    })
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    evidence = "PRIVATE EVIDENCE BODY 4e9f7a"
    result = _data(port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {
            "receipt_id": "receipt-a",
            "outcome": "followed",
            "evidence": evidence,
            "confidence": 0.9,
            "idempotency_key": "feedback-a",
        },
        context=_context(tmp_path),
        generation=7,
    ))
    assert result["outcome"] == "followed"
    digest = hashlib.sha256(evidence.encode("utf-8")).hexdigest()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT evidence_digest,metadata_json FROM rule_feedback_refs WHERE feedback_id=?", (result["feedback_id"],)).fetchone()
        assert row is not None and row["evidence_digest"] == digest
        dump = "\n".join(str(value) for table in ("rule_feedback_refs", "rule_decisions", "rule_evidence_contributions") for record in conn.execute(f"SELECT * FROM {table}").fetchall() for value in record)
    assert evidence not in dump

    wrong_owner = port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-a", "outcome": "followed", "idempotency_key": "feedback-b"},
        context=_context(tmp_path, agent="agent-b"),
        generation=7,
    )
    assert wrong_owner["ok"] is False
    assert wrong_owner["code"] == "rule_receipt_owner_mismatch"


def test_native_rule_feedback_undo_is_compensating_and_cross_agent_undo_is_rejected(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    definition = store.upsert_definition(build_definition("Always test before release", kind="procedure"))
    store.record_receipt({
        "receipt_id": "receipt-a",
        "definition_id": definition.definition_id,
        "source_rule_id": "source-rule",
        "share_group_id": "shared-a",
        "agent_instance_id": "agent-a",
        "project_ref": "project-a",
        "session_id": "session-agent-a",
        "task_hash": "task",
        "selection_digest": "selection",
        "metadata_json": "{}",
        "created_at": "2026-08-10T00:00:00+00:00",
    })
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    feedback = _data(port.dispatch_mcp(
        "memoryguard_rule_feedback",
        {"receipt_id": "receipt-a", "outcome": "violated", "idempotency_key": "fb"},
        context=_context(tmp_path),
        generation=7,
    ))
    rejected = port.dispatch_mcp(
        "memoryguard_rule_undo",
        {"undo_id": feedback["undo_id"], "idempotency_key": "undo-other"},
        context=_context(tmp_path, agent="agent-b"),
        generation=7,
    )
    assert rejected["ok"] is False
    assert rejected["code"] == "rule_undo_owner_mismatch"

    undone = _data(port.dispatch_mcp(
        "memoryguard_rule_undo",
        {"undo_id": feedback["undo_id"], "idempotency_key": "undo-owner"},
        context=_context(tmp_path),
        generation=7,
    ))
    assert undone["compensation"]["feedback_id"] == feedback["feedback_id"]
    with sqlite3.connect(store.db_path) as conn:
        rows = conn.execute("SELECT feedback_id,outcome,metadata_json FROM rule_feedback_refs ORDER BY created_at,feedback_id").fetchall()
    assert any(row[0] == feedback["feedback_id"] and row[1] == "violated" for row in rows)
    assert any(row[1] == "ignored" and feedback["feedback_id"] in row[2] for row in rows)


def test_native_rule_reads_are_real_scoped_services_not_neutral_placeholders(tmp_path: Path):
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    context = _context(tmp_path)
    created = _data(port.dispatch_mcp(
        "memoryguard_rule_create_auto",
        {"text": "Always keep provenance", "idempotency_key": "read-create"},
        context=context,
        generation=7,
    ))
    read = _data(port.dispatch_mcp(
        "memoryguard_rule_decision_read",
        {"decision_id": created["decision"]["decision_id"]},
        context=context,
        generation=7,
    ))
    assert read["decision"]["decision_id"] == created["decision"]["decision_id"]
    stats = _data(port.dispatch_mcp(
        "memoryguard_rule_scope_stats",
        {}, context=context, generation=7,
    ))
    assert stats["active"] == 1
    entries = {item["name"]: item for item in port.coverage()["surfaces"]["mcp"]["entries"]}
    for name in (
        "memoryguard_rule_create_auto", "memoryguard_rule_feedback", "memoryguard_rule_undo",
        "memoryguard_rule_decision_read", "memoryguard_rule_scope_stats",
    ):
        assert entries[name]["status"] == "implemented"
