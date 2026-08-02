"""Migration: Rule Intelligence Layer v1 (P3).

Creates the four new tables plus the merge-decision ledger in the
``rule-intelligence`` database.  Idempotent ``CREATE TABLE IF NOT EXISTS`` —
the same script is safe to re-run after upgrades and is shared with the
``RuleMergeStore`` bootstrap.
"""
from __future__ import annotations

import sqlite3
from typing import Any

RULE_INTELLIGENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_definitions (
    definition_id TEXT PRIMARY KEY,
    canonical_text TEXT NOT NULL,
    normalized_intent TEXT NOT NULL,
    rule_kind TEXT NOT NULL,
    polarity TEXT NOT NULL,
    semantic_hash TEXT NOT NULL,
    parameter_schema TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    confidence REAL NOT NULL DEFAULT 1.0,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    superseded_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_definitions_semantic
    ON rule_definitions(semantic_hash);
CREATE TABLE IF NOT EXISTS rule_bindings (
    binding_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL,
    share_group_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority INTEGER NOT NULL DEFAULT 0,
    owner_agent_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    authorization TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id),
    CHECK (
        created_by NOT IN ('auto', 'backfill')
        OR target_type IN ('agent', 'agent_project')
    ),
    CHECK (
        target_type != 'system'
        OR created_by NOT IN ('auto', 'backfill')
    )
);
CREATE INDEX IF NOT EXISTS idx_rule_bindings_definition
    ON rule_bindings(definition_id);
CREATE INDEX IF NOT EXISTS idx_rule_bindings_group
    ON rule_bindings(share_group_id);
CREATE TABLE IF NOT EXISTS rule_evidence (
    evidence_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    semantic_hash TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_definition
    ON rule_evidence(definition_id);
CREATE INDEX IF NOT EXISTS idx_rule_evidence_source
    ON rule_evidence(source_rule_id);
CREATE TABLE IF NOT EXISTS rule_merge_proposals (
    proposal_id TEXT PRIMARY KEY,
    definition_ids TEXT NOT NULL,
    similarity_score REAL NOT NULL DEFAULT 0.0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    agent_count INTEGER NOT NULL DEFAULT 0,
    project_count INTEGER NOT NULL DEFAULT 0,
    contradiction_score REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'candidate',
    explanation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_merge_decisions (
    decision_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    canonical_definition_id TEXT NOT NULL,
    merged_definition_ids TEXT NOT NULL,
    before_bindings TEXT NOT NULL DEFAULT '[]',
    after_bindings TEXT NOT NULL DEFAULT '[]',
    migration TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL DEFAULT 'auto',
    status TEXT NOT NULL DEFAULT 'merged',
    created_at TEXT NOT NULL,
    undone_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_merge_decisions_proposal
    ON rule_merge_decisions(proposal_id);
"""

SCHEMA_VERSION = "rule-intelligence-v1"


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


def apply_v1(conn: sqlite3.Connection) -> None:
    """Apply v1 schema inside caller-owned transaction."""
    _execute_sql_script_atomic(conn, RULE_INTELLIGENCE_SCHEMA)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION, "1"),
    )


def migrate(db_path: str) -> dict[str, Any]:
    """Apply the Rule Intelligence v1 schema and return a migration ledger."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            apply_v1(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "tables": [
            "rule_definitions", "rule_bindings", "rule_evidence",
            "rule_merge_proposals", "rule_merge_decisions",
        ],
    }
