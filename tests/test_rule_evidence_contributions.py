from __future__ import annotations

import sqlite3

from memoryguard.migrations.rule_intelligence_v2 import migrate
from memoryguard.rule_evidence_ledger import (
    build_contribution,
    deactivate_contribution,
    list_contributions,
    list_effective,
    rebuild_effective,
    upsert_contribution,
)
from memoryguard.rule_merge_store import RuleMergeStore


def _database(tmp_path):
    db_path = tmp_path / "evidence-ledger.db"
    migrate(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _contribution(
    contribution_id: str,
    *,
    authority: int = 0,
    confidence: float = 1.0,
    polarity: str = "positive",
    receipt_id: str | None = None,
    kind: str = "receipt",
):
    return build_contribution(
        contribution_id=contribution_id,
        definition_id="definition-1",
        independence_key="independence-1",
        kind=kind,
        polarity=polarity,
        authority=authority,
        confidence=confidence,
        observed_at="2026-08-03T00:00:00Z",
        receipt_id=receipt_id or contribution_id,
        session_id=contribution_id,
        session_trusted=True,
    )


def test_higher_authority_wins_even_when_confidence_is_lower(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(conn, _contribution("low", authority=10, confidence=1.0))
        upsert_contribution(conn, _contribution("high", authority=20, confidence=0.0))
        rebuild_effective(conn, updated_at="2026-08-03T01:00:00Z")
        assert list_effective(conn)[0].winner_contribution_id == "high"
    finally:
        conn.close()


def test_deactivated_winner_restores_active_runner_up(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(conn, _contribution("winner", authority=20))
        upsert_contribution(conn, _contribution("runner-up", authority=10))
        rebuild_effective(conn, updated_at="2026-08-03T01:00:00Z")
        assert deactivate_contribution(conn, "winner")
        rebuild_effective(conn, updated_at="2026-08-03T02:00:00Z")
        assert list_effective(conn)[0].winner_contribution_id == "runner-up"
        assert len(list_contributions(conn)) == 2
    finally:
        conn.close()


def test_negative_contribution_can_become_effective_after_positive_is_cleared(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(conn, _contribution("positive", authority=20))
        upsert_contribution(
            conn, _contribution("negative", authority=10, polarity="negative")
        )
        rebuild_effective(conn)
        assert deactivate_contribution(conn, "positive")
        rebuild_effective(conn)
        effective = list_effective(conn)[0]
        assert effective.winner_contribution_id == "negative"
        assert effective.polarity == "negative"
    finally:
        conn.close()


def test_two_receipts_are_retained_for_fallback(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(
            conn, _contribution("receipt-a", authority=20, receipt_id="receipt-a")
        )
        upsert_contribution(
            conn, _contribution("receipt-b", authority=10, receipt_id="receipt-b")
        )
        rebuild_effective(conn)
        assert deactivate_contribution(conn, "receipt-a")
        rebuild_effective(conn)
        assert {item.contribution_id for item in list_contributions(conn)} == {
            "receipt-a", "receipt-b",
        }
        assert list_effective(conn)[0].winner_contribution_id == "receipt-b"
    finally:
        conn.close()


def test_rebuild_is_idempotent_and_does_not_duplicate_projection_rows(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(conn, _contribution("one", authority=1))
        first = rebuild_effective(conn, updated_at="2026-08-03T01:00:00Z")
        second = rebuild_effective(conn, updated_at="2026-08-03T02:00:00Z")
        assert [item.to_dict() for item in first] == [item.to_dict() for item in second]
        assert conn.execute(
            "SELECT COUNT(*) FROM rule_evidence_effective"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_confidence_zero_is_preserved_and_not_treated_as_default(tmp_path):
    conn = _database(tmp_path)
    try:
        upsert_contribution(conn, _contribution("zero", authority=5, confidence=0.0))
        rebuild_effective(conn)
        assert list_contributions(conn)[0].confidence == 0.0
        assert list_effective(conn)[0].confidence == 0.0
    finally:
        conn.close()


def test_migration_creates_complete_keys_and_indexes(tmp_path):
    conn = _database(tmp_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "rule_evidence_contributions", "rule_evidence_effective",
        } <= tables
        columns = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(rule_evidence_contributions)"
            )
        }
        assert {
            "definition_id", "independence_key", "kind", "polarity",
            "authority", "confidence", "observed_at", "active",
            "receipt_id", "feedback_id", "session_trusted",
        } <= columns
        effective_pk = [
            row[1] for row in conn.execute(
                "PRAGMA table_info(rule_evidence_effective)"
            ) if row[5]
        ]
        assert effective_pk == ["definition_id", "independence_key", "kind"]
        indexes = {
            row[1] for row in conn.execute(
                "PRAGMA index_list(rule_evidence_effective)"
            )
        }
        assert "idx_rule_evidence_effective_winner" in indexes
    finally:
        conn.close()


def test_fresh_rule_merge_store_bootstraps_ledger_with_foreign_keys(tmp_path):
    store = RuleMergeStore(tmp_path)
    with store._db() as conn:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert {"rule_evidence_contributions", "rule_evidence_effective"} <= tables
    assert foreign_keys == 1
