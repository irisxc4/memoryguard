"""Rule Intelligence governance-layer migration tests (v2).

The upgrade must be safe for databases created before the governance layer:

  * ``rule_intelligence_v2.migrate`` is idempotent and adds the four new
    tables plus the guarded ``ALTER TABLE ADD COLUMN`` for every existing one;
  * opening ``RuleMergeStore`` on an old v1 database upgrades it in place —
    existing rows keep their data and the new columns get their defaults;
  * the new CRUD (negative evidence / reputation / profile / versions) works
    against an upgraded database.
"""
from __future__ import annotations

import sqlite3

from memoryguard.migrations.rule_definition_v1 import migrate as migrate_v1
from memoryguard.migrations.rule_intelligence_v2 import (
    GOVERNANCE_UPGRADE_COLUMNS,
    migrate as migrate_v2,
)
from memoryguard.rule_definition import build_definition
from memoryguard.rule_evidence import build_evidence, build_negative_evidence
from memoryguard.rule_merge_store import RuleMergeStore


def test_v2_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "v2.db"
    first = migrate_v2(str(db_path))
    second = migrate_v2(str(db_path))
    assert first["schema_version"] == second["schema_version"] == "rule-intelligence-v2"
    for table in (
        "rule_negative_evidence", "agent_reputation",
        "project_profile", "rule_definition_versions",
    ):
        assert table in first["tables"]
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"rule_negative_evidence", "agent_reputation", "project_profile",
            "rule_definition_versions"} <= tables


def test_store_upgrades_existing_v1_database_in_place(tmp_path):
    # Build a pure v1 database (no governance columns) and seed a definition.
    db_path = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    migrate_v1(str(db_path))
    definition = build_definition("提交代码前必须运行测试")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO rule_definitions (
                definition_id, canonical_text, normalized_intent, rule_kind,
                polarity, semantic_hash, parameter_schema, status, confidence,
                revision, created_at, updated_at, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 1.0, 1, ?, ?, '')
            """,
            (definition.definition_id, definition.canonical_text,
             definition.normalized_intent, definition.rule_kind,
             definition.polarity, definition.semantic_hash,
             definition.parameter_schema, definition.created_at,
             definition.updated_at),
        )
        conn.commit()
    finally:
        conn.close()

    # Opening the store runs the in-place upgrade.
    store = RuleMergeStore(tmp_path)
    loaded = store.get_definition(definition.definition_id)
    assert loaded is not None
    # Old data preserved, new columns defaulted.
    assert loaded.canonical_text == definition.canonical_text
    assert loaded.rule_strength == "observation"
    assert loaded.maturity_state == "observing"

    conn = store._db()
    try:
        existing = {
            row["name"] for row in conn.execute(
                "PRAGMA table_info(rule_merge_proposals)"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"readiness_score", "cooldown_until",
            "first_merge_acknowledged", "conflict_type"} <= existing


def test_upgrade_guard_matches_store_upgrade_list(tmp_path):
    # The migration module and the store must agree on every column they add,
    # otherwise an upgraded DB and a fresh DB drift apart.
    from memoryguard.rule_merge_store import RuleMergeStore as _Store
    store_columns = set(_Store._UPGRADE_COLUMNS)
    migration_columns = set(GOVERNANCE_UPGRADE_COLUMNS)
    assert store_columns == migration_columns


def test_governance_crud_works_on_upgraded_database(tmp_path):
    store = RuleMergeStore(tmp_path)
    a = build_definition("提交代码前必须运行测试")
    store.upsert_definition(a)
    store.upsert_negative_evidence(build_negative_evidence(
        definition_id=a.definition_id, source_rule_id="neg",
        agent_instance_id="agent-a", project_ref="p1", content="违背",
    ))
    store.upsert_agent_reputation(
        agent_id="agent-a", success_rate=0.9, sample_count=50,
    )
    store.upsert_project_profile(project_ref="p1", production_level=1.0)
    version = store.record_definition_version(
        definition_id=a.definition_id, superseded_by="next",
        old_strength="must", new_strength="should", actor="admin",
    )
    assert store.count_negative_evidence() == 1
    assert store.list_negative_evidence(a.definition_id)[0].content_hash
    assert store.get_agent_reputation("agent-a")["sample_count"] == 50
    assert store.get_project_profile("p1")["production_level"] == 1.0
    assert store.count_definition_versions() == 1
    assert store.list_definition_versions(a.definition_id)[0]["version_id"] == version["version_id"]
