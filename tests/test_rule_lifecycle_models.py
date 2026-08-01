from __future__ import annotations

import json
import sqlite3

import pytest

from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleDecision,
    RuleException,
    RuleScopeStats,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(memory_id: str = "rule-1", agent: str = "agent-a") -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body="keep this rule scoped",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        agent_instance_id=agent,
    )


def test_rule_decision_round_trip_and_legacy_decision_compatibility(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    decision = RuleDecision(
        decision_id="decision-1",
        actor="auto",
        before={"assignments": []},
        after={"assignments": [{"target_type": "agent", "target_id": "agent-a"}]},
        reason="narrow after wrong-scope feedback",
        confidence=0.75,
        undo_id="undo-1",
        rule_id="rule-1",
        action="auto_narrow",
        memory_id="rule-1",
        scope_reason="feedback",
    )
    persisted = store.append_rule_decision(decision)
    assert persisted.to_dict()["decision_id"] == "decision-1"
    assert store.get_rule_decision("decision-1").after["assignments"]
    assert store.list_rule_decisions(rule_id="rule-1")[0].undo_id == "undo-1"


def test_scope_stats_are_cumulative_and_filterable_by_agent_project_rule(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.record_rule_scope("rule-1", agent_instance_id="agent-a", project_ref="C:/Work/App", outcome="accepted")
    store.record_rule_scope("rule-1", agent_instance_id="agent-a", project_ref="c:/work/app", outcome="corrected")
    store.record_rule_scope("rule-1", agent_instance_id="agent-b", project_ref="p", outcome="wrong_scope", count=2)
    stats = store.get_rule_scope_stats("rule-1", agent_instance_id="agent-a", project_ref="c:/work/app")
    assert stats is not None
    assert (stats.total, stats.accepted, stats.corrected, stats.wrong_scope) == (2, 1, 1, 0)
    assert len(store.list_rule_scope_stats(agent_instance_id="agent-b", project_ref="p")) == 1
    assert store.export_state()["rule_scope_stats"]


def test_exception_relation_rollback_is_reversible_metadata(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    exception = store.append_rule_exception(RuleException(
        parent_rule="rule-1", child_exception="exception-1", priority=10,
        reason="temporary migration", rollback={"restore": "rule-1"},
    ))
    assert exception.exception_id
    rolled_back = store.rollback_rule_exception(
        exception.exception_id, rollback={"restore": "rule-1", "at": "now"},
    )
    assert rolled_back.active is False
    assert store.list_rule_exceptions(active=False)[0].rollback["restore"] == "rule-1"


def test_automatic_assignment_fails_closed_without_cross_scope_expansion(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    with pytest.raises(ValueError, match="cannot broaden"):
        store.set_rule_assignments(
            "rule-1", [{"target_type": "system"}],
            automatic=True, actor_agent_id="agent-a",
        )
    with pytest.raises(ValueError, match="another agent"):
        store.set_rule_assignments(
            "rule-1", [{"target_type": "agent", "target_id": "agent-b"}],
            automatic=True, actor_agent_id="agent-a",
        )
    # Manual governance keeps system matching intact.
    store.set_rule_assignments("rule-1", [{"target_type": "system"}])
    assert store.list_rule_assignments("rule-1")[0].target_type == "system"


def test_lifecycle_tables_survive_snapshot_rollback_and_clear(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    receipt = store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="hit-1", memory_id="rule-1", share_group_id="team",
        agent_instance_id="agent-a", task_hash="task", task="use rule",
    ))
    store.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id="feedback-1", receipt_id=receipt.receipt_id,
        outcome="corrected", actor="agent-a",
    ))
    store.append_rule_decision(RuleDecision(
        decision_id="decision-2", actor="auto", rule_id="rule-1",
    ))
    store.record_rule_scope("rule-1", agent_instance_id="agent-a", outcome="corrected")
    store.append_rule_exception(RuleException("rule-1", "exception-1"))
    version = store.create_version_snapshot("lifecycle")
    store.rollback_to_version(version)
    assert len(store.list_rule_decisions()) == 1
    assert len(store.list_rule_scope_stats()) == 1
    assert len(store.list_rule_exceptions()) == 1
    store.clear_all()
    assert store.list_rule_decisions() == []
    assert store.list_rule_scope_stats() == []
    assert store.list_rule_exceptions() == []
    assert store.get_rule_match_feedback_by_receipt("hit-1") is None


def test_old_database_gets_lifecycle_schema_migration(tmp_path):
    root = tmp_path / ".memoryguard" / "shared-memory" / "team"
    root.mkdir(parents=True)
    path = root / "memory.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE records (memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, confidence REAL NOT NULL, conflict_group_id TEXT, locked INTEGER NOT NULL, supersedes TEXT, provenance TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT)")
        conn.commit()
    store = SharedMemoryStore(tmp_path, "team")
    tables = {row[0] for row in store._connect().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rule_decisions", "rule_scope_stats", "rule_exceptions"} <= tables
