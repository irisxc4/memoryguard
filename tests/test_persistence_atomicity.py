"""Store-level regression tests for the report's P0 persistence edges."""

import sqlite3

import pytest

from memoryguard.schema_v3 import (
    ConflictGroup,
    EffectiveAgentContext,
    MemoryEvent,
    MemoryKind,
    QuarantineEntry,
    RuleDecision,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.rule_creation import RuleCreationService
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(memory_id: str = "r", *, agent: str = "agent-a") -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=f"body-{memory_id}",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        agent_instance_id=agent,
        created_at=_now_iso(),
        updated_at=_now_iso(),
    )


def _decision(memory_id: str) -> RuleDecision:
    return RuleDecision(
        decision_id=f"decision-{memory_id}",
        actor="agent:agent-a",
        owner_agent_id="agent-a",
        action="rule_create_auto",
        rule_id=memory_id,
        memory_id=memory_id,
        before={},
        after={},
    )


def test_rule_create_decision_failure_rolls_back_record_and_event(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "atomic")
    original = store._insert_rule_decision

    def fail(*args, **kwargs):
        raise RuntimeError("decision fault")

    monkeypatch.setattr(store, "_insert_rule_decision", fail)
    with pytest.raises(RuntimeError, match="decision fault"):
        store.apply_rule_create_atomic(
            _record(),
            MemoryEvent(
                event_id="event-r",
                agent_instance_id="agent-a",
                share_group_id="atomic",
                raw_content="body-r",
            ),
            assignments=[{"target_type": "agent", "target_id": "agent-a"}],
            decision=_decision("r"),
        )
    assert store.get_record("r") is None
    assert not store.list_events()
    monkeypatch.setattr(store, "_insert_rule_decision", original)


def test_rule_create_undo_decision_failure_rolls_back_delete(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "atomic")
    result = store.apply_rule_create_atomic(
        _record(),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
        decision=_decision("r"),
    )
    # Revision is the full rule behavior hash (body + kind + priority + locked
    # + provenance + audience), captured at creation time -- never a body-only
    # canonical hash, which would let an edit of priority/assignments still be
    # undone by the stale create decision.
    expected_hash = result["decision"]["metadata"]["record_revision_hash"]
    original = store._insert_rule_decision

    def fail(*args, **kwargs):
        raise RuntimeError("inverse fault")

    monkeypatch.setattr(store, "_insert_rule_decision", fail)
    with pytest.raises(RuntimeError, match="inverse fault"):
        store.revert_rule_create_atomic(
            "r", expected_hash,
            RuleDecision(
                decision_id="inverse-r", actor="agent:agent-a",
                owner_agent_id="agent-a", action="rule_create_undo",
                rule_id="r", memory_id="r",
            ),
        )
    assert store.get_record("r").status == SharedMemoryStatus.ACTIVE
    monkeypatch.setattr(store, "_insert_rule_decision", original)


def test_jsonl_failure_reports_committed_degraded(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "atomic")
    monkeypatch.setattr(store, "_append_jsonl", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    result = store.apply_rule_create_atomic(
        _record(),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
        decision=_decision("r"),
    )
    assert result["committed"] is True
    assert result["backup_status"] == "degraded"
    assert store.get_record("r") is not None


def test_deduplicated_decision_targets_existing_record(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    store.append_record(
        _record("original"),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    candidate = _record("candidate")
    candidate.body = "body-original"
    decision = _decision("candidate")
    decision.after = {"record": candidate.to_dict()}
    result = store.apply_rule_create_atomic(
        candidate,
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
        decision=decision,
    )
    assert result["mutation_kind"] == "deduplicated"
    assert result["memory_id"] == "original"
    persisted = store.get_rule_decision("decision-candidate")
    assert persisted is not None
    assert persisted.memory_id == "original"
    assert persisted.rule_id == "original"
    assert persisted.after["record"]["memory_id"] == "original"


def test_owner_agent_column_migrates_from_legacy_table(tmp_path):
    store = SharedMemoryStore(tmp_path, "legacy")
    store.append_rule_decision(_decision("old"))
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_rule_decisions_owner")
        conn.execute("ALTER TABLE rule_decisions RENAME TO rule_decisions_old")
        conn.execute(
            "CREATE TABLE rule_decisions ("
            "decision_id TEXT PRIMARY KEY, actor TEXT NOT NULL, "
            "before_state TEXT NOT NULL DEFAULT '{}', after_state TEXT NOT NULL DEFAULT '{}', "
            "reason TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 1.0, "
            "undo_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, "
            "rule_id TEXT NOT NULL DEFAULT '', action TEXT NOT NULL DEFAULT '', "
            "target_ids TEXT NOT NULL DEFAULT '[]', metadata TEXT NOT NULL DEFAULT '{}')"
        )
        conn.execute(
            "INSERT INTO rule_decisions SELECT decision_id,actor,before_state,after_state,reason,confidence,undo_id,created_at,rule_id,action,target_ids,metadata FROM rule_decisions_old"
        )
        conn.execute("DROP TABLE rule_decisions_old")
    # Keep this migration test focused on SQLite schema compatibility rather
    # than re-importing the JSONL projection on the next writable open.
    store.rule_decisions_bak_path.unlink(missing_ok=True)
    migrated = SharedMemoryStore(tmp_path, "legacy")
    loaded = migrated.get_rule_decision("decision-old")
    assert loaded is not None
    assert loaded.owner_agent_id == ""
    migrated.append_rule_decision(_decision("new"))
    assert migrated.get_rule_decision("decision-new").owner_agent_id == "agent-a"


def test_migration_failure_leaves_no_partial_feedback_schema(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "legacy-fault")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DROP TABLE rule_match_feedbacks")
        conn.execute(
            "CREATE TABLE rule_match_feedbacks ("
            "feedback_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, "
            "outcome TEXT NOT NULL, actor TEXT NOT NULL, "
            "evidence TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 1.0, "
            "created_at TEXT NOT NULL, UNIQUE(receipt_id))"
        )

    original = SharedMemoryStore._migration_checkpoint

    def fail_after_rename(self, name):
        if name == "feedback_after_rename":
            raise RuntimeError("migration fault")
        return original(self, name)

    monkeypatch.setattr(SharedMemoryStore, "_migration_checkpoint", fail_after_rename)
    with pytest.raises(RuntimeError, match="migration fault"):
        SharedMemoryStore(tmp_path, "legacy-fault")
    with sqlite3.connect(store.db_path) as conn:
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='rule_match_feedbacks'"
        ).fetchone()[0]
    assert "UNIQUE(receipt_id)" in table_sql.replace(" ", "")


def test_last_sibling_revoke_removes_generated_exclude(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    store.append_record(
        _record("parent"),
        assignments=[{"target_type": "agent", "target_id": "agent-a"}],
    )
    service = RuleCreationService(tmp_path, "atomic", store=store)
    context = EffectiveAgentContext(
        agent_instance_id="agent-a", share_group_id="atomic",
        project_ref=str(tmp_path / "project"), session_id="s1",
    )
    for idx in (1, 2):
        store.append_rule_match_receipt(RuleMatchReceipt(
            receipt_id=f"receipt-{idx}", memory_id="parent",
            share_group_id="atomic", agent_instance_id="agent-a",
            task_hash=f"task-{idx}", task="task", project_ref=context.project_ref,
            session_id=f"s{idx}", created_at=_now_iso(),
        ))
        decision = service.submit_feedback(
            f"receipt-{idx}", "exception", f"a{idx}",
            evidence=f"override {idx}", effective_context=context,
        )
        assert decision.status == "created"
    relations = store.list_rule_exceptions(parent_rule="parent")
    # Pass the recorded post-create revision explicitly; Store must never
    # substitute the current hash itself.
    for relation in relations:
        expected = relation.rollback["parent_assignments_after_hash"]
        inverse = RuleDecision(
            decision_id=f"undo-{relation.exception_id}", actor="agent:agent-a",
            owner_agent_id="agent-a", action="rule_exception_revoke",
            rule_id=relation.child_exception, memory_id=relation.child_exception,
        )
        store.revert_rule_exception(
            relation.exception_id, expected_parent_assignment_hash=expected,
            decision=inverse,
        )
        if relation is relations[0]:
            # First revoke transfers generated ownership to the sibling.
            assert store.list_rule_assignments("parent")
    assert not any(
        item.effect == "exclude" for item in store.list_rule_assignments("parent")
    )


def _event(event_id: str, *, raw: str = "candidate") -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        agent_instance_id="agent-a",
        share_group_id="atomic",
        raw_content=raw,
        created_at=_now_iso(),
    )


def test_lifecycle_supersede_round_trip_is_atomic(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    store.append_record(_record("old"), assignments=[{"target_type": "agent", "target_id": "agent-a"}])
    result = store.apply_rule_lifecycle_atomic(
        _record("new"),
        _event("event-supersede"),
        decision=_decision("new"),
        mutation_kind="superseded",
        old_record_ids=["old"],
    )
    assert result["target_ids"] == ["new", "old"]
    assert store.get_record("new").status == SharedMemoryStatus.ACTIVE
    assert store.get_record("old").status == SharedMemoryStatus.SHADOWED
    undo = store.revert_rule_lifecycle_atomic(
        result["decision"], result["record_hashes"]["new"]
    )
    assert undo["undone"] is True
    assert store.get_record("new").status == SharedMemoryStatus.DELETED
    assert store.get_record("old").status == SharedMemoryStatus.ACTIVE


def test_lifecycle_conflict_round_trip_preserves_unrelated_members(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    store.append_record(_record("old"), assignments=[{"target_type": "agent", "target_id": "agent-a"}])
    store.append_record(_record("other"), assignments=[{"target_type": "agent", "target_id": "agent-a"}])
    group = ConflictGroup(
        group_id="group-1", member_ids=["old", "other"], reason="existing conflict"
    )
    store.append_conflict(group)
    result = store.apply_rule_lifecycle_atomic(
        _record("new"),
        _event("event-conflict"),
        decision=_decision("new"),
        mutation_kind="conflicted",
        old_record_ids=["old"],
        conflict_group=group,
    )
    assert store.get_record("new").status == SharedMemoryStatus.CONFLICTED
    assert set(store.list_conflicts()[0].member_ids) == {"old", "other", "new"}
    store.revert_rule_lifecycle_atomic(
        result["decision"], result["record_hashes"]["new"]
    )
    assert store.get_record("new").status == SharedMemoryStatus.DELETED
    remaining = {item.group_id: set(item.member_ids) for item in store.list_conflicts()}
    assert remaining["group-1"] == {"old", "other"}


def test_lifecycle_quarantine_round_trip_writes_release_tombstone(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    quarantine = QuarantineEntry(
        quarantine_id="q-1", memory_id="new", reason="secret",
        detected_pattern="token", original_content="candidate",
    )
    result = store.apply_rule_lifecycle_atomic(
        _record("new"),
        _event("event-quarantine"),
        decision=_decision("new"),
        mutation_kind="quarantined",
        quarantine_entry=quarantine,
    )
    assert store.get_record("new").status == SharedMemoryStatus.QUARANTINED
    assert any(item.quarantine_id == "q-1" and not item.released for item in store.list_quarantine())
    store.revert_rule_lifecycle_atomic(
        result["decision"], result["record_hashes"]["new"]
    )
    assert store.get_record("new").status == SharedMemoryStatus.DELETED
    assert any(item.memory_id == "new" and item.released for item in store.list_quarantine())


def test_lifecycle_decision_failure_rolls_back_every_branch(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "atomic")
    store.append_record(_record("old"), assignments=[{"target_type": "agent", "target_id": "agent-a"}])

    def fail(*args, **kwargs):
        raise RuntimeError("decision fault")

    monkeypatch.setattr(store, "_insert_rule_decision", fail)
    with pytest.raises(RuntimeError, match="decision fault"):
        store.apply_rule_lifecycle_atomic(
            _record("new"), _event("event-failure"),
            decision=_decision("new"), mutation_kind="superseded",
            old_record_ids=["old"],
        )
    assert store.get_record("new") is None
    assert store.get_record("old").status == SharedMemoryStatus.ACTIVE
    assert not store.list_events()


def test_lifecycle_automatic_scope_guard_and_manual_broad_override(tmp_path):
    store = SharedMemoryStore(tmp_path, "atomic")
    broad = [{"target_type": "group", "target_id": "atomic"}]
    with pytest.raises(ValueError, match="automatic assignment cannot broaden"):
        store.apply_rule_lifecycle_atomic(
            _record("auto-broad"), _event("event-auto-broad"),
            decision=_decision("auto-broad"), mutation_kind="quarantined",
            assignments=broad, actor_agent_id="agent-a", automatic=True,
        )
    assert store.get_record("auto-broad") is None

    result = store.apply_rule_lifecycle_atomic(
        _record("manual-broad"), _event("event-manual-broad"),
        decision=_decision("manual-broad"), mutation_kind="quarantined",
        assignments=broad, actor_agent_id="agent-a", automatic=False,
    )
    assert result["committed"] is True
    assert store.get_record("manual-broad") is not None
    assert store.list_rule_assignments("manual-broad")[0].target_type == "group"
