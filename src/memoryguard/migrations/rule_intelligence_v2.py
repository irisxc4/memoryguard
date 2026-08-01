"""Migration: Rule Intelligence governance layer v2 (P3-001/002/003).

Extends the v1 ``rule-intelligence`` schema with the governance layer:

  * ``rule_definitions``      + rule_strength / maturity_state
  * ``rule_merge_proposals``  + readiness_score / governance_reasons /
                               cooldown_until / first_merge_acknowledged /
                               negative_score / conflict_type
  * ``rule_merge_decisions``  + readiness_at_merge / strength_ok /
                               negative_ok / first_merge_acknowledged
  * new tables: rule_negative_evidence, agent_reputation, project_profile,
    rule_definition_versions

The migration is idempotent two ways: ``CREATE TABLE IF NOT EXISTS`` for the
new tables and a ``PRAGMA table_info`` guard before every ``ALTER TABLE ADD
COLUMN``.  ``RuleMergeStore._apply_upgrade`` runs the same column guard on
every open, so this module is a standalone entry point for upgrade tooling.
"""
from __future__ import annotations

import sqlite3
from typing import Any

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
"""

# (table, column, DDL) added only when the column is absent.
GOVERNANCE_UPGRADE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("rule_definitions", "rule_strength", "TEXT NOT NULL DEFAULT 'observation'"),
    ("rule_definitions", "maturity_state", "TEXT NOT NULL DEFAULT 'observing'"),
    ("rule_merge_proposals", "readiness_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "governance_reasons", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "cooldown_until", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_proposals", "first_merge_acknowledged", "INTEGER NOT NULL DEFAULT 0"),
    ("rule_merge_proposals", "negative_score", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_proposals", "conflict_type", "TEXT NOT NULL DEFAULT ''"),
    ("rule_merge_decisions", "readiness_at_merge", "REAL NOT NULL DEFAULT 0.0"),
    ("rule_merge_decisions", "strength_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "negative_ok", "INTEGER NOT NULL DEFAULT 1"),
    ("rule_merge_decisions", "first_merge_acknowledged", "INTEGER NOT NULL DEFAULT 1"),
)

SCHEMA_VERSION = "rule-intelligence-v2"


def migrate(db_path: str) -> dict[str, Any]:
    """Apply the governance-layer upgrade; safe to re-run.

    The upgrade targets tables created by v1, so the base schema is ensured
    first (idempotent) — this makes the module usable standalone on a fresh
    database as well as on an upgraded one.
    """
    from .rule_definition_v1 import migrate as migrate_v1

    migrate_v1(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(GOVERNANCE_SCHEMA)
        for table, column, ddl in GOVERNANCE_UPGRADE_COLUMNS:
            existing = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
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
    finally:
        conn.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": [
            "rule_negative_evidence", "agent_reputation", "project_profile",
            "rule_definition_versions",
        ],
        "upgraded_columns": list(GOVERNANCE_UPGRADE_COLUMNS),
    }
