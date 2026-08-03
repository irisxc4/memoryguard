"""Migration: Rule Intelligence governance layer v2 (P3-001/002/003).

Extends the v1 ``rule-intelligence`` schema with the governance layer:

  * ``rule_definitions``      + rule_strength / maturity_state
  * ``rule_merge_proposals``  + readiness_score / governance_reasons /
                               cooldown_until / first_merge_acknowledged /
                               negative_score / conflict_type / judge_*
  * ``rule_merge_decisions``  + readiness_at_merge / strength_ok /
                               negative_ok / first_merge_acknowledged / judge_*
  * new tables: rule_negative_evidence, agent_reputation, project_profile,
    rule_definition_versions, rule_evidence_contributions,
    rule_evidence_effective

The migration is idempotent two ways: ``CREATE TABLE IF NOT EXISTS`` for the
new tables and a ``PRAGMA table_info`` guard before every ``ALTER TABLE ADD
COLUMN``.  ``RuleMergeStore._apply_upgrade`` runs the same column guard on
every open, so this module is a standalone entry point for upgrade tooling.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..governance_capability import GOVERNANCE_CAPABILITY_SCHEMA
from ..rule_evidence_ledger import (
    EVIDENCE_LEDGER_SCHEMA,
    build_contribution,
    rebuild_effective,
    upsert_contribution,
)
from ..schema_v3 import stable_hash

GOVERNANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_negative_evidence (
    evidence_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE INDEX IF NOT EXISTS idx_rule_negative_evidence_definition
    ON rule_negative_evidence(definition_id);
CREATE TABLE IF NOT EXISTS agent_reputation (
    agent_id TEXT PRIMARY KEY,
    success_rate REAL NOT NULL DEFAULT 0.0,
    rule_accuracy REAL NOT NULL DEFAULT 0.0,
    violation_rate REAL NOT NULL DEFAULT 0.0,
    sample_count INTEGER NOT NULL DEFAULT 0,
    feedback_quality REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_profile (
    project_ref TEXT PRIMARY KEY,
    production_level REAL NOT NULL DEFAULT 0.0,
    criticality REAL NOT NULL DEFAULT 0.0,
    owner_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_definition_versions (
    version_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT '',
    old_strength TEXT NOT NULL DEFAULT '',
    new_strength TEXT NOT NULL DEFAULT '',
    change_reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
);
CREATE INDEX IF NOT EXISTS idx_rule_definition_versions_definition
    ON rule_definition_versions(definition_id);
CREATE TABLE IF NOT EXISTS rule_merge_approvals (
    approval_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    capability_id TEXT NOT NULL DEFAULT '',
    expected_definition_revisions TEXT NOT NULL DEFAULT '{}',
    approval_scope TEXT NOT NULL DEFAULT 'merge',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_merge_approvals_proposal
    ON rule_merge_approvals(proposal_id);
CREATE TABLE IF NOT EXISTS rule_definition_aliases (
    old_definition_id TEXT PRIMARY KEY,
    new_definition_id TEXT NOT NULL,
    migration_decision_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_source_links (
    share_group_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    original_definition_id TEXT NOT NULL DEFAULT '',
    canonical_definition_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (share_group_id, memory_id)
);
CREATE TABLE IF NOT EXISTS rule_definition_runtime_stats (
    definition_id TEXT PRIMARY KEY,
    followed INTEGER NOT NULL DEFAULT 0,
    violated INTEGER NOT NULL DEFAULT 0,
    not_applicable INTEGER NOT NULL DEFAULT 0,
    exception_count INTEGER NOT NULL DEFAULT 0,
    distinct_sessions INTEGER NOT NULL DEFAULT 0,
    distinct_projects INTEGER NOT NULL DEFAULT 0,
    last_observed_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_runtime_feedback (
    feedback_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    authority INTEGER NOT NULL DEFAULT 0,
    session_trusted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_runtime_feedback_definition
    ON rule_runtime_feedback(definition_id);
CREATE TABLE IF NOT EXISTS rule_effective_feedback_projection (
    receipt_id TEXT PRIMARY KEY,
    effective_feedback_id TEXT NOT NULL DEFAULT '',
    definition_id TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    positive_evidence_id TEXT NOT NULL DEFAULT '',
    negative_evidence_id TEXT NOT NULL DEFAULT '',
    session_trusted INTEGER NOT NULL DEFAULT 0,
    session_source TEXT NOT NULL DEFAULT 'absent',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_projection_state (
    scope_id TEXT PRIMARY KEY,
    last_outbox_event_id TEXT NOT NULL DEFAULT '',
    last_projected_event_id TEXT NOT NULL DEFAULT '',
    projection_lag INTEGER NOT NULL DEFAULT 0,
    projection_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

# Lane D: contribution history plus the rebuildable effective-winner
# projection.  Kept in the v2 migration so fresh and upgraded databases share
# one idempotent schema entry point.
GOVERNANCE_SCHEMA += EVIDENCE_LEDGER_SCHEMA
GOVERNANCE_SCHEMA += "\n" + GOVERNANCE_CAPABILITY_SCHEMA

# (table, column, DDL) added only when the column is absent.
GOVERNANCE_UPGRADE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("rule_definitions", "rule_strength", "TEXT NOT NULL DEFAULT 'observation'"),
    ("rule_definitions", "maturity_state", "TEXT NOT NULL DEFAULT 'observing'"),
    ("rule_merge_proposals", "readiness_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "readiness_components", "TEXT NOT NULL DEFAULT '{}'"),
    ("rule_merge_proposals", "readiness_digest", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "governance_reasons", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "cooldown_until", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "first_merge_acknowledged", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_merge_proposals", "negative_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "conflict_type", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "judge_source", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "judge_model", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "judge_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "judge_confidence", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "judge_recommendation", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "judge_rationale", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "candidate_since", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "last_evaluated_at", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "assessment_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_merge_proposals", "definition_revision_a", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_merge_proposals", "definition_revision_b", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_merge_proposals", "evidence_digest", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "negative_digest", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "binding_digest", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "runtime_digest", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "policy_version", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "weight_breakdown", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "independence_key", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "share_group_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "source_root_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "source_object_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "session_trusted", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_evidence", "feedback_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_evidence", "feedback_authority", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_evidence", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_negative_evidence", "independence_key", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "share_group_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "session_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "receipt_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "feedback_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "feedback_authority", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_negative_evidence", "source_root_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "source_object_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_negative_evidence", "session_trusted", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_negative_evidence", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "readiness_at_merge", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_decisions", "strength_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "polarity_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "parameters_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "contradiction_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "negative_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "first_merge_acknowledged", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "judge_source", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_decisions", "judge_model", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_decisions", "judge_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_decisions", "judge_confidence", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_decisions", "judge_recommendation", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_decisions", "judge_rationale", "TEXT NOT NULL DEFAULT ''"),
    ("rule_runtime_feedback", "receipt_id", "TEXT NOT NULL DEFAULT ''"),
    ("rule_runtime_feedback", "session_trusted", "INTEGER NOT NULL DEFAULT 0"),
)

SCHEMA_VERSION = "rule-intelligence-v2"


def _execute_sql_script_atomic(conn: sqlite3.Connection, script: str) -> None:
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        statement = buffer.strip()
        buffer = ""
        if statement:
            conn.execute(statement)
    if buffer.strip():
        raise sqlite3.OperationalError("incomplete SQL schema statement")


def _upgrade_legacy_evidence_ledger(conn: sqlite3.Connection) -> None:
    """Import old positive/negative rows, including inactive history."""
    definitions: set[str] = set()
    for table, polarity in (
        ("rule_evidence", "positive"),
        ("rule_negative_evidence", "negative"),
    ):
        for row in conn.execute(
            f"SELECT * FROM {table} ORDER BY evidence_id"
        ).fetchall():
            evidence_id = str(row["evidence_id"] or "")
            definition_id = str(row["definition_id"] or "")
            if not evidence_id or not definition_id:
                continue
            if conn.execute(
                "SELECT 1 FROM rule_evidence_contributions "
                "WHERE source_evidence_id=? AND polarity=? LIMIT 1",
                (evidence_id, polarity),
            ).fetchone() is not None:
                continue
            source_rule_id = str(row["source_rule_id"] or "")
            agent_instance_id = str(row["agent_instance_id"] or "")
            project_ref = str(row["project_ref"] or "")
            session_id = str(row["session_id"] or "")
            source_root_id = str(row["source_root_id"] or "")
            source_object_id = str(row["source_object_id"] or "")
            independence_key = str(row["independence_key"] or "")
            if not independence_key:
                independence_key = stable_hash(
                    "rule-evidence-legacy-independence",
                    project_ref, agent_instance_id, source_root_id,
                    source_object_id or session_id,
                    str(row["content_hash"] or ""),
                )
            item = build_contribution(
                contribution_id=stable_hash(
                    "rule-evidence-contribution", polarity, evidence_id,
                ),
                definition_id=definition_id,
                independence_key=independence_key,
                kind="evidence",
                polarity=polarity,
                authority=int(row["feedback_authority"] or 0),
                confidence=(
                    float(row["confidence"])
                    if row["confidence"] is not None else 1.0
                ),
                observed_at=str(row["observed_at"] or ""),
                active=bool(int(row["active"] or 0)),
                receipt_id=str(row["receipt_id"] or ""),
                feedback_id=str(row["feedback_id"] or ""),
                source_rule_id=source_rule_id,
                source_evidence_id=evidence_id,
                source_memory_id=source_rule_id or evidence_id,
                source_ids={
                    "evidence_id": evidence_id,
                    "source_rule_id": source_rule_id,
                    "receipt_id": str(row["receipt_id"] or ""),
                    "feedback_id": str(row["feedback_id"] or ""),
                    "content_hash": str(row["content_hash"] or ""),
                    "semantic_hash": str(row["semantic_hash"] or ""),
                    "provider": str(row["provider"] or ""),
                    "source_root_id": source_root_id,
                    "source_object_id": source_object_id,
                },
                agent_instance_id=agent_instance_id,
                project_ref=project_ref,
                share_group_id=str(row["share_group_id"] or ""),
                session_id=session_id,
                source_root_id=source_root_id,
                source_object_id=source_object_id,
                session_trusted=bool(int(row["session_trusted"] or 0)),
            )
            upsert_contribution(conn, item)
            definitions.add(definition_id)
    for definition_id in sorted(definitions):
        rebuild_effective(conn, definition_id=definition_id)


def migrate(db_path: str) -> dict[str, Any]:
    """Apply the governance-layer upgrade; safe to re-run.

    The upgrade targets tables created by v1, so the base schema is ensured
    first (idempotent) — this makes the module usable standalone on a fresh
    database as well as on an upgraded one.
    """
    from .rule_definition_v1 import apply_v1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply_v1(conn)
            _execute_sql_script_atomic(conn, GOVERNANCE_SCHEMA)
            for table, column, ddl in GOVERNANCE_UPGRADE_COLUMNS:
                existing = {
                    str(row[1])
                    for row in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            _upgrade_legacy_evidence_ledger(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION, "1"),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": [
            "rule_negative_evidence", "agent_reputation", "project_profile",
            "rule_definition_versions", "rule_evidence_contributions",
            "rule_evidence_effective",
        ],
        "upgraded_columns": list(GOVERNANCE_UPGRADE_COLUMNS),
    }
