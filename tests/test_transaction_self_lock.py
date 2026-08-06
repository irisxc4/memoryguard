# -*- coding: utf-8 -*-
"""Req1: one ``backfill_group()`` uses exactly one transaction connection.

SQLite self-lock: opening a *second* connection to the same database file
while the same thread holds ``BEGIN IMMEDIATE`` on the active write
connection can raise ``OperationalError: database is locked`` against our own
transaction (the rule-intelligence DB is rollback-journal, not WAL, so there
is no concurrency escape hatch).  ``_read_conn()`` reuses the active write
connection for in-transaction reads, and the atomic backfill passes its
transaction's connection explicitly to the source-link query instead of
re-opening the database.

This test proves that a real ``backfill_group()`` over a P3 database with all
three interesting states present -- an un-consumed legacy outbox, an existing
source link, and a legacy Definition -- opens only the one transaction
connection and never a second ``_db()`` connection while the write transaction
is active.
"""
from __future__ import annotations

from contextlib import contextmanager

from memoryguard.rule_merge import RuleMergeService, RuleMergeStore
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _seed_record(store, memory_id, body, *, agent="agent-1"):
    store.append_record(SharedMemoryRecord(
        memory_id=memory_id, body=body, kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        priority=10, agent_instance_id=agent,
        created_at=_now_iso(), updated_at=_now_iso(),
    ), assignments=[{"target_type": "agent", "target_id": agent}])


def _pend_feedback(legacy: SharedMemoryStore, group_id: str, memory_id: str) -> None:
    """Create one trusted receipt + feedback -> a pending outbox event."""
    receipt = RuleMatchReceipt(
        receipt_id=f"sl-receipt-{memory_id}", memory_id=memory_id,
        share_group_id=group_id, agent_instance_id="agent-1",
        task_hash=f"sl-task-{memory_id}", task="self-lock probe",
        session_id=f"sl-session-{memory_id}", session_trusted=True,
        session_source="host", project_ref="project-1", provider="codex",
        runtime_role="worker", context_hash=f"sl-context-{memory_id}",
        created_at=_now_iso(),
    )
    legacy.append_rule_match_receipt(receipt)
    legacy.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id=f"sl-feedback-{memory_id}", receipt_id=receipt.receipt_id,
        outcome="followed", actor="agent-1", source="agent", authority=3,
    ))


def test_backfill_group_uses_single_transaction_connection(monkeypatch, tmp_path):
    group = "self-lock-group"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "sl-1", "must run tests before commit")
    _seed_record(legacy, "sl-2", "must run lint before commit")

    store = RuleMergeStore(tmp_path)
    service = RuleMergeService(store)

    # First pass establishes the "legacy Definition" and "existing source
    # link" states that a real re-run must read inside its transaction.
    service.backfill_group(legacy, group)
    assert store.get_source_link(group, "sl-1") is not None
    assert len(store.list_definitions()) >= 1

    # A pending, un-consumed outbox event.  Re-running backfill with live
    # source links, legacy definitions and an unconsumed outbox is the exact
    # state that used to open a second _db() connection per record.
    _pend_feedback(legacy, group, "sl-2")
    assert legacy.list_unconsumed_rule_events(), "outbox must be pending"

    # Instrument every _db() open that happens while the write transaction is
    # active: only the transaction's own connection is allowed.
    opened = {"during_txn": 0, "extra": 0}
    real_write_conn = RuleMergeStore._write_conn

    @contextmanager
    def tracking_write_conn(self):
        with real_write_conn(self) as conn:
            real_db = self._db

            def counting_db():
                fresh = real_db()
                opened["during_txn"] += 1
                if fresh is not conn:
                    opened["extra"] += 1
                return fresh

            self._db = counting_db
            try:
                yield conn
            finally:
                self._db = real_db

    monkeypatch.setattr(RuleMergeStore, "_write_conn", tracking_write_conn)

    ledger = service.backfill_group(legacy, group)

    assert ledger["records"] == 2
    # The write transaction opened exactly one connection (inside
    # ``real_write_conn``); every read during the transaction reused it, so no
    # second ``_db()`` connection was opened against the P3 database.
    assert opened["during_txn"] == 0
    assert opened["extra"] == 0
    # Both sources keep their durable canonical route after the re-run.
    assert store.get_source_link(group, "sl-1") is not None
    assert store.get_source_link(group, "sl-2") is not None


def test_source_link_read_works_outside_transaction(tmp_path):
    """The independent-read branch of ``_read_conn()`` still works."""
    group = "self-lock-read"
    legacy = SharedMemoryStore(tmp_path, group)
    _seed_record(legacy, "slr-1", "must run tests before commit")
    store = RuleMergeStore(tmp_path)
    RuleMergeService(store).backfill_group(legacy, group)
    link = store.get_source_link(group, "slr-1")
    assert link is not None
    assert link["status"] == "active"
    assert store.list_source_links(share_group_id=group, status="active")
