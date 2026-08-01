from __future__ import annotations

import json
import sqlite3

import pytest

from memoryguard.schema_v3 import (
    EffectiveAgentContext,
    MemoryKind,
    RuleDecision,
    RuleAssignment,
    RuleException,
    RuleScopeStats,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore
from memoryguard.context_bootstrap import build_context_packet


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


def test_c971_feedback_stream_migration_preserves_rows_and_allows_append(tmp_path):
    root = tmp_path / ".memoryguard" / "shared-memory" / "team"
    root.mkdir(parents=True)
    path = root / "memory.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE records (
                memory_id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, confidence REAL NOT NULL,
                conflict_group_id TEXT, locked INTEGER NOT NULL, supersedes TEXT,
                provenance TEXT, agent_instance_id TEXT, created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE rule_match_receipts (
                receipt_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL,
                share_group_id TEXT NOT NULL, agent_instance_id TEXT NOT NULL,
                task_hash TEXT NOT NULL, task TEXT NOT NULL,
                assignment_ids TEXT NOT NULL, selection_reason TEXT NOT NULL,
                matcher_version TEXT NOT NULL, confidence REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE rule_match_feedbacks (
                feedback_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL, actor TEXT NOT NULL,
                evidence TEXT NOT NULL, confidence REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO records VALUES
                ('rule-1', 'body', 'procedure', 'active', 1.0, '', 0,
                 '[]', '[]', 'agent-a', '2026-01-01T00:00:00+00:00',
                 '2026-01-01T00:00:00+00:00');
            INSERT INTO rule_match_receipts VALUES
                ('receipt-1', 'rule-1', 'team', 'agent-a', 'task', 'task',
                 '[]', '', 'v1', 1.0, '2026-01-01T00:00:00+00:00');
            INSERT INTO rule_match_feedbacks VALUES
                ('feedback-1', 'receipt-1', 'unobserved', 'hook:x', '', 0.0,
                 '2026-01-01T00:00:00+00:00');
            """
        )
    store = SharedMemoryStore(tmp_path, "team")
    store.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id="feedback-2", receipt_id="receipt-1",
        outcome="followed", actor="agent-a",
    ))
    rows = store.list_rule_match_feedbacks(receipt_id="receipt-1")
    assert [item.feedback_id for item in rows] == ["feedback-1", "feedback-2"]
    assert store.get_rule_match_feedback_by_receipt("receipt-1").outcome == "followed"
    with store._connect() as conn:
        indexes = conn.execute("PRAGMA index_list(rule_match_feedbacks)").fetchall()
        assert not any(
            row[2]
            and [column[2] for column in conn.execute(
                f"PRAGMA index_info([{row[1]}])"
            ).fetchall()] == ["receipt_id"]
            for row in indexes
        )


def test_first_receipt_insert_persists_context_fields(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    receipt = store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="context-receipt", memory_id="rule-1", share_group_id="team",
        agent_instance_id="agent-a", task_hash="task", task="use rule",
        project_ref="C:/Work/App", provider="codex", runtime_role="subagent",
        session_id="session-a", context_hash="context-a",
    ))
    assert receipt.project_ref == "c:/work/app"
    assert receipt.provider == "codex"
    assert receipt.runtime_role == "subagent"
    assert receipt.session_id == "session-a"
    assert receipt.context_hash == "context-a"


def test_effective_feedback_evidence_groups_one_event_per_receipt(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    for index, session in enumerate(("session-a", "session-b", "session-b"), start=1):
        receipt = store.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=f"receipt-{index}", memory_id="rule-1", share_group_id="team",
            agent_instance_id="agent-a", task_hash=f"task-{index}", task="use rule",
            project_ref="project", session_id=session, context_hash=f"context-{index}",
        ))
        store.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"unobserved-{index}", receipt_id=receipt.receipt_id,
            outcome="unobserved", actor="hook:test", source="hook",
        ))
        store.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"scope-{index}", receipt_id=receipt.receipt_id,
            outcome="not_applicable", actor="agent-a", source="agent",
            confidence=0.9,
        ))
    evidence = store.list_effective_rule_feedback_evidence(
        "rule-1", "agent-a", "project",
    )
    assert len(evidence) == 3
    assert {item.session_id for item in evidence} == {"session-a", "session-b"}
    assert all(item.outcome == "not_applicable" for item in evidence)


def test_rule_split_is_atomic_and_revert_restores_behavior(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    before = store.list_rule_assignments("rule-1")
    child = _record("child-1")
    exclude = RuleAssignment(
        memory_id="rule-1", target_type="agent_project", target_id="agent-a",
        project_ref="project", effect="exclude",
    )
    exception = RuleException(
        parent_rule="rule-1", child_exception="child-1",
        rollback={"generated_parent_assignment": exclude.to_dict(), "project_ref": "project"},
    )
    decision = RuleDecision(
        decision_id="split-decision", actor="auto", rule_id="rule-1",
        action="rule_exception",
    )
    result = store.apply_rule_exception_atomic(
        "rule-1", before, before + [exclude], child, [
            RuleAssignment(memory_id="child-1", target_type="agent", target_id="agent-a")
        ], exception, decision,
    )
    assert result["child_record"].memory_id == "child-1"
    assert store.get_record("child-1").status == SharedMemoryStatus.ACTIVE
    assert store.get_rule_exception(exception.exception_id).active is True
    assert any(item.effect == "exclude" for item in store.list_rule_assignments("rule-1"))
    store.revert_rule_exception(
        exception.exception_id,
        expected_parent_assignment_hash=store.rule_assignment_hash("rule-1"),
    )
    assert store.get_record("child-1").status == SharedMemoryStatus.DELETED
    assert store.get_rule_exception(exception.exception_id).active is False
    assert [item.assignment_id for item in store.list_rule_assignments("rule-1")] == [
        item.assignment_id for item in before
    ]


def test_rule_split_failure_rolls_back_child_parent_exception_and_decision(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    before = store.list_rule_assignments("rule-1")
    child = _record("child-1")
    exception = RuleException("rule-1", "child-1")
    decision = RuleDecision(decision_id="split-decision", actor="auto", rule_id="rule-1")
    original = store._insert_rule_exception
    def fail(*args, **kwargs):
        raise RuntimeError("injected exception write failure")
    monkeypatch.setattr(store, "_insert_rule_exception", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.apply_rule_exception_atomic(
            "rule-1", before, before, child, [], exception, decision,
        )
    monkeypatch.setattr(store, "_insert_rule_exception", original)
    assert store.get_record("child-1") is None
    assert store.list_rule_assignments("rule-1") == before
    assert store.get_rule_exception(exception.exception_id) is None
    assert store.get_rule_decision("split-decision") is None


def test_bootstrap_receipts_separate_sessions_and_persist_context(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record())
    first = build_context_packet(
        store,
        task="use rule",
        effective_context=EffectiveAgentContext(
            "agent-a", "team", provider="codex", project_ref="project",
            runtime_role="root", session_id="session-a",
        ),
    )
    second = build_context_packet(
        store,
        task="use rule",
        effective_context=EffectiveAgentContext(
            "agent-a", "team", provider="codex", project_ref="project",
            runtime_role="root", session_id="session-b",
        ),
    )
    first_receipt = RuleMatchReceipt.from_dict(first["mandatory_match_receipts"][0])
    second_receipt = RuleMatchReceipt.from_dict(second["mandatory_match_receipts"][0])
    assert first_receipt.receipt_id != second_receipt.receipt_id
    assert first_receipt.session_id == "session-a"
    assert second_receipt.session_id == "session-b"
    store.append_rule_match_receipt(first_receipt)
    persisted = store.get_rule_match_receipt(first_receipt.receipt_id)
    assert persisted is not None and persisted.context_hash
