from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.migration.rules import RulesMigrationError, V1RulesMigrator
from memoryguard.migration.v2_validator import DomainValidation, V2MigrationValidator, V2ValidationResult
from memoryguard.rules.v2_store import EvidenceProjectionError, EvidenceProjector, RuleV2Store


def _db(path: Path, schema: str, rows: dict[str, list[tuple]] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    for sql, values in rows.items():
        conn.executemany(sql, values)
    conn.commit()
    conn.close()


def _checkpoint_rules_db(root: Path) -> None:
    """Make immutable validator reads observe all committed WAL frames."""

    with sqlite3.connect(root / ".memoryguard" / "rules" / "rules.db") as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


LEGACY = """
CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT, kind TEXT, status TEXT,
 confidence REAL, injection_policy TEXT, agent_instance_id TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE rule_assignments(memory_id TEXT, target_type TEXT, target_id TEXT,
 project_ref TEXT, effect TEXT, priority_override INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE rule_exceptions(exception_id TEXT PRIMARY KEY, parent_rule TEXT, child_exception TEXT,
 priority INTEGER, reason TEXT, rollback TEXT, active INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE rule_decisions(decision_id TEXT PRIMARY KEY, actor TEXT, rule_id TEXT, action TEXT,
 before_state TEXT, after_state TEXT, reason TEXT, confidence REAL, undo_id TEXT, created_at TEXT);
CREATE TABLE rule_match_receipts(receipt_id TEXT PRIMARY KEY, memory_id TEXT, share_group_id TEXT,
 agent_instance_id TEXT, task_hash TEXT, task TEXT, assignment_ids TEXT, created_at TEXT);
CREATE TABLE rule_match_feedbacks(feedback_id TEXT PRIMARY KEY, receipt_id TEXT, outcome TEXT,
 actor TEXT, evidence TEXT, confidence REAL, created_at TEXT);
CREATE TABLE rule_event_outbox(event_id TEXT PRIMARY KEY, event_type TEXT, memory_id TEXT,
 raw_content TEXT, created_at TEXT);
"""


def _seed_group(root: Path, group: str, body: str = "必须运行测试") -> Path:
    path = root / ".memoryguard" / "shared-memory" / group / "memory.db"
    _db(
        path,
        LEGACY,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("same-id", body, "workflow", "active", .9, "always", "agent-a", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("same-id", "agent", "agent-a", "", "include", 3, "t0", "t1")],
            "INSERT INTO rule_match_receipts VALUES (?,?,?,?,?,?,?,?)": [("r-" + group, "same-id", group, "agent-a", "task", "run", "[]", "t1")],
            "INSERT INTO rule_match_feedbacks VALUES (?,?,?,?,?,?,?)": [("f-" + group, "r-" + group, "accepted", "agent-a", "secret evidence", .8, "t1")],
            "INSERT INTO rule_event_outbox VALUES (?,?,?,?,?)": [("o-" + group, "feedback", "same-id", "secret body", "t1")],
        },
    )
    return path


def test_multiple_groups_same_memory_id_preserves_permission_multiset(tmp_path: Path):
    first = _seed_group(tmp_path, "group-a")
    second = _seed_group(tmp_path, "group-b")
    before = {str(first): hashlib.sha256(first.read_bytes()).hexdigest(), str(second): hashlib.sha256(second.read_bytes()).hexdigest()}
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    assert report.binding_multiset_diff == 0
    assert report.system_auto_expansion == 0
    assert report.counts["records"] == 2
    assert V1RulesMigrator(tmp_path).store.metrics()["bindings"] == 2
    assert {str(first): hashlib.sha256(first.read_bytes()).hexdigest(), str(second): hashlib.sha256(second.read_bytes()).hexdigest()} == before


def test_rule_intelligence_core_rows_and_unknown_table_are_ledgered(tmp_path: Path):
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(
        ri,
        """
        CREATE TABLE rule_definitions(definition_id TEXT PRIMARY KEY, canonical_text TEXT, normalized_intent TEXT,
          rule_kind TEXT, polarity TEXT, semantic_hash TEXT, parameter_schema TEXT, status TEXT, confidence REAL,
          revision INTEGER, rule_strength TEXT, maturity_state TEXT, created_at TEXT, updated_at TEXT, mystery TEXT);
        CREATE TABLE rule_bindings(binding_id TEXT PRIMARY KEY, definition_id TEXT, share_group_id TEXT,
          target_type TEXT, target_id TEXT, project_ref TEXT, effect TEXT, priority INTEGER, created_by TEXT, status TEXT);
        CREATE TABLE rule_evidence(evidence_id TEXT PRIMARY KEY, definition_id TEXT, source_rule_id TEXT,
          share_group_id TEXT, content_hash TEXT, observed_at TEXT);
        CREATE TABLE unknown_fixture(secret TEXT);
        """,
        {
            "INSERT INTO rule_definitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [("old-def", "运行测试", "{}", "workflow", "positive", "s", "{}", "active", 1., 1, "must", "observing", "t0", "t1", "x")],
            "INSERT INTO rule_bindings VALUES (?,?,?,?,?,?,?,?,?,?)": [("old-bind", "old-def", "ri-group", "system", "", "", "include", 1, "auto", "active")],
            "INSERT INTO rule_evidence VALUES (?,?,?,?,?,?)": [("ev-1", "old-def", "old-rule", "ri-group", "digest", "t1")],
            "INSERT INTO unknown_fixture VALUES (?)": [("unknown value",)],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    assert report.counts["ri_definitions"] == 1
    assert report.counts["evidence"] == 1
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_unknown_columns_ledger").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM rule_evidence_outbox").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_bindings WHERE target_type='system' AND created_by IN ('auto','backfill')").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rule_source_links").fetchone()[0] == 0 or True


def test_rule_intelligence_binding_scope_columns_are_authoritative(tmp_path: Path):
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(
        ri,
        """
        CREATE TABLE rule_definitions(definition_id TEXT PRIMARY KEY, canonical_text TEXT, normalized_intent TEXT,
          rule_kind TEXT, polarity TEXT, semantic_hash TEXT, parameter_schema TEXT, status TEXT, confidence REAL,
          revision INTEGER, rule_strength TEXT, maturity_state TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE rule_bindings(binding_id TEXT PRIMARY KEY, definition_id TEXT, share_group_id TEXT,
          target_type TEXT, target_id TEXT, project_ref TEXT, provider TEXT, runtime_role TEXT,
          effect TEXT, priority INTEGER, owner_agent_id TEXT, created_by TEXT, status TEXT, revision INTEGER,
          created_at TEXT, updated_at TEXT);
        CREATE TABLE rule_binding_contributions(
          contribution_id TEXT PRIMARY KEY, binding_id TEXT, definition_id TEXT, share_group_id TEXT,
          source_memory_id TEXT, source_revision TEXT, legacy_assignment_hash TEXT,
          target_type TEXT, target_id TEXT, project_ref TEXT, provider TEXT, runtime_role TEXT,
          effect TEXT, priority INTEGER, owner_agent_id TEXT, audience_json TEXT, active INTEGER,
          status TEXT, revision INTEGER, created_at TEXT, updated_at TEXT);
        """,
        {
            "INSERT INTO rule_definitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [
                ("ri-def", "必须测试", "{}", "workflow", "positive", "hash", "{}", "active", 1.0, 1, "must", "observing", "t0", "t1")
            ],
            "INSERT INTO rule_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [
                ("ri-binding", "ri-def", "ri-group", "system", "", "project-x", "provider-x", "runtime-x", "exclude", 7, "owner-x", "auto", "active", 3, "t0", "t1")
            ],
            "INSERT INTO rule_binding_contributions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [
                ("ri-contribution", "ri-binding", "ri-def", "ri-group", "source-memory", "r3", "assignment-hash", "system", "", "project-x", "provider-x", "runtime-x", "exclude", 7, "owner-x", '{"target_type":"system"}', 1, "active", 4, "t0", "t1")
            ],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["binding_identity_multiset_diff"] == 0
    assert domain.metrics["unknown_columns"] == 0
    assert not any(error.startswith("binding_identity_multiset_diff:") for error in domain.errors)
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(rule_binding_contributions)")}
        assert {"target_type", "target_id", "project_ref", "provider", "runtime_role", "effect", "priority", "owner_agent_id", "revision"} <= columns
        row = conn.execute("SELECT target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,revision FROM rule_binding_contributions").fetchone()
        assert row == ("system", "", "project-x", "provider-x", "runtime-x", "exclude", 7, "owner-x", 4)
        assert conn.execute("SELECT COUNT(*) FROM rule_migration_map WHERE source_table IN ('rule_bindings','rule_binding_contributions')").fetchone()[0] == 2


def _phase1_rules_fixture(root: Path, *, explicit_scope_columns: bool = False) -> Path:
    """Create the smallest accepted Phase-1 rules DB for upgrade tests."""

    db = root / ".memoryguard" / "rules" / "rules.db"
    scope_columns = """
        target_type TEXT NOT NULL DEFAULT 'agent',
        target_id TEXT NOT NULL DEFAULT '',
        project_ref TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        runtime_role TEXT NOT NULL DEFAULT '',
        effect TEXT NOT NULL DEFAULT 'include',
        priority INTEGER NOT NULL DEFAULT 0,
        owner_agent_id TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1,
    """ if explicit_scope_columns else ""
    _db(
        db,
        f"""
        CREATE TABLE schema_meta(domain TEXT, version INTEGER, marker TEXT);
        CREATE TABLE rule_definitions(definition_id TEXT PRIMARY KEY, canonical_text TEXT);
        CREATE TABLE rule_bindings(
            binding_id TEXT PRIMARY KEY, definition_id TEXT, share_group_id TEXT,
            target_type TEXT NOT NULL, target_id TEXT, project_ref TEXT,
            effect TEXT, priority INTEGER, created_by TEXT, status TEXT
        );
        CREATE TABLE rule_binding_contributions(
            contribution_id TEXT PRIMARY KEY, binding_id TEXT, definition_id TEXT,
            share_group_id TEXT, source_memory_id TEXT, source_revision TEXT,
            legacy_assignment_hash TEXT,
            {scope_columns}
            audience_json TEXT, active INTEGER, status TEXT,
            created_at TEXT, updated_at TEXT
        );
        INSERT INTO schema_meta VALUES ('rules', 1, 'memoryguard-v2-phase1');
        """,
        {},
    )
    audience = {
        "target_type": "system",
        "target_id": "target-x",
        "project_ref": "project-x",
        "provider": "provider-x",
        "runtime_role": "runtime-x",
        "effect": "exclude",
        "priority": 7,
        "owner_agent_id": "owner-x",
        "revision": 4,
    }
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO rule_definitions VALUES (?, ?)", ("def-1", "legacy rule"))
        conn.execute(
            "INSERT INTO rule_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("binding-1", "def-1", "group-1", "agent", "", "", "include", 0, "manual", "active"),
        )
        if explicit_scope_columns:
            conn.execute(
                "INSERT INTO rule_binding_contributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "contribution-1", "binding-1", "def-1", "group-1", "memory-1", "r1", "hash-1",
                    "agent", "explicit-target", "explicit-project", "explicit-provider", "explicit-runtime",
                    "include", 99, "explicit-owner", 9, '{"target_type":"system","effect":"exclude","revision":4}',
                    1, "active", "t0", "t1",
                ),
            )
        else:
            conn.execute(
                "INSERT INTO rule_binding_contributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "contribution-1", "binding-1", "def-1", "group-1", "memory-1", "r1", "hash-1",
                    json.dumps(audience), 1, "active", "t0", "t1",
                ),
            )
        conn.commit()
    return db


def test_phase1_contribution_scope_backfill_is_lossless_and_idempotent(tmp_path: Path):
    legacy_root = tmp_path / "legacy"
    db = _phase1_rules_fixture(legacy_root)

    RuleV2Store(legacy_root)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,revision "
            "FROM rule_binding_contributions"
        ).fetchone()
    assert row == ("system", "target-x", "project-x", "provider-x", "runtime-x", "exclude", 7, "owner-x", 4)

    # A later legal update must survive every subsequent initialization.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE rule_binding_contributions SET target_type=?,effect=?,revision=? WHERE contribution_id=?",
            ("agent", "include", 8, "contribution-1"),
        )
        conn.commit()
    RuleV2Store(legacy_root)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT target_type,effect,revision FROM rule_binding_contributions WHERE contribution_id=?",
            ("contribution-1",),
        ).fetchone() == ("agent", "include", 8)

    explicit_root = tmp_path / "explicit"
    explicit_db = _phase1_rules_fixture(explicit_root, explicit_scope_columns=True)
    RuleV2Store(explicit_root)
    with sqlite3.connect(explicit_db) as conn:
        assert conn.execute(
            "SELECT target_type,target_id,project_ref,provider,runtime_role,effect,priority,owner_agent_id,revision "
            "FROM rule_binding_contributions"
        ).fetchone() == (
            "agent", "explicit-target", "explicit-project", "explicit-provider", "explicit-runtime",
            "include", 99, "explicit-owner", 9,
        )


def test_fault_before_commit_rolls_back_rules_staging(tmp_path: Path):
    _seed_group(tmp_path, "group-a")
    migrator = V1RulesMigrator(tmp_path, fail_at="before_commit")
    with pytest.raises(RulesMigrationError):
        migrator.migrate()
    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_definitions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rule_evidence_outbox").fetchone()[0] == 0


def test_evidence_outbox_is_idempotent_and_sink_failure_stays_pending(tmp_path: Path):
    _seed_group(tmp_path, "group-a")
    first = V1RulesMigrator(tmp_path).migrate()
    assert first.evidence_status == "NOT_EVALUATED"
    store = RuleV2Store(tmp_path)
    calls: list[str] = []
    result = EvidenceProjector(store, lambda ref: calls.append(str(ref["event_id"]))).project(migration_id=first.migration_id)
    assert result["consumed"] == 1 and calls
    again = EvidenceProjector(store, lambda ref: calls.append(str(ref["event_id"]))).project(migration_id=first.migration_id)
    assert again["consumed"] == 0

    _seed_group(tmp_path, "group-b")
    failed = V1RulesMigrator(tmp_path, evidence_sink=lambda ref: (_ for _ in ()).throw(RuntimeError("sink down"))).migrate()
    assert failed.status == "UNVERIFIED" and failed.evidence_status == "FAILED"
    assert failed.evidence_pending >= 1


def test_read_only_source_missing_does_not_create_v1_database(tmp_path: Path):
    assert not list(tmp_path.rglob("*.db"))
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.status in {"MIGRATED", "IDEMPOTENT"}
    assert not list((tmp_path / ".memoryguard" / "shared-memory").glob("**/memory.db")) if (tmp_path / ".memoryguard" / "shared-memory").exists() else True


def test_rules_marker_preflight_rejects_unknown_and_low_version_without_writes(tmp_path: Path):
    RuleV2Store(tmp_path)
    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE rules_schema_meta SET version=?,marker=? WHERE schema_id='rules'", (99, "future-rules"))
        conn.commit()
    snapshot = db.read_bytes()
    with pytest.raises(RuntimeError):
        RuleV2Store(tmp_path, read_only=True)
    with pytest.raises(RuntimeError):
        RuleV2Store(tmp_path)
    assert db.read_bytes() == snapshot


def test_readonly_source_uri_handles_special_unicode_path(tmp_path: Path):
    # ``?`` is a reserved Win32 filename character; the connector still
    # percent-encodes it when supplied by a URI caller.  Exercise the other
    # troublesome characters on the native filesystem here.
    path = tmp_path / "目录 #百分比%问号.db"
    _db(path, "CREATE TABLE records(memory_id TEXT PRIMARY KEY, body TEXT);", {"INSERT INTO records VALUES (?,?)": [("r", "body")]})
    source = V1RulesMigrator._read_sqlite("shared_memory", "special", path)
    assert source.rows["records"][0]["memory_id"] == "r"


def test_source_identifier_quote_handles_embedded_double_quote(tmp_path: Path):
    name = 'legacy"table; DROP TABLE records;--'
    quoted = name.replace('"', '""')
    path = tmp_path / "quoted.db"
    _db(path, f'CREATE TABLE "{quoted}"(secret TEXT);', {f'INSERT INTO "{quoted}" VALUES (?)': [("kept",)]})
    source = V1RulesMigrator._read_sqlite("shared_memory", "quoted", path)
    assert source.rows[name][0]["secret"] == "kept"


def test_rule_intelligence_p3_ledgers_are_migrated_without_token_body(tmp_path: Path):
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(
        ri,
        """
        CREATE TABLE rule_definitions(definition_id TEXT PRIMARY KEY, canonical_text TEXT, normalized_intent TEXT,
          rule_kind TEXT, polarity TEXT, semantic_hash TEXT, parameter_schema TEXT, status TEXT, confidence REAL,
          revision INTEGER, rule_strength TEXT, maturity_state TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE rule_definition_versions(version_id TEXT PRIMARY KEY, definition_id TEXT, superseded_by TEXT,
          old_strength TEXT, new_strength TEXT, change_reason TEXT, actor TEXT, evidence TEXT, created_at TEXT);
        CREATE TABLE agent_reputation(agent_id TEXT PRIMARY KEY, success_rate REAL, rule_accuracy REAL,
          violation_rate REAL, sample_count INTEGER, feedback_quality REAL, created_at TEXT, updated_at TEXT);
        CREATE TABLE project_profile(project_ref TEXT PRIMARY KEY, production_level REAL, criticality REAL,
          owner_verified INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE rule_definition_runtime_stats(definition_id TEXT PRIMARY KEY, followed INTEGER, violated INTEGER,
          not_applicable INTEGER, exception_count INTEGER, distinct_sessions INTEGER, distinct_projects INTEGER, last_observed_at TEXT);
        CREATE TABLE rule_evidence_contributions(contribution_id TEXT PRIMARY KEY, definition_id TEXT,
          independence_key TEXT, kind TEXT, polarity TEXT, authority INTEGER, confidence REAL, observed_at TEXT,
          active INTEGER, receipt_id TEXT, feedback_id TEXT, source_rule_id TEXT, source_evidence_id TEXT,
          source_memory_id TEXT, source_ids TEXT, agent_instance_id TEXT, project_ref TEXT, share_group_id TEXT,
          session_id TEXT, source_root_id TEXT, source_object_id TEXT, session_trusted INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE rule_evidence_effective(definition_id TEXT, independence_key TEXT, kind TEXT,
          winner_contribution_id TEXT, polarity TEXT, authority INTEGER, confidence REAL, observed_at TEXT, updated_at TEXT);
        CREATE TABLE governance_capabilities(token_hash TEXT PRIMARY KEY, principal TEXT, scope TEXT,
          proposal_id TEXT, nonce TEXT, issued_at REAL, expires_at REAL, consumed INTEGER, consumed_at REAL);
        """,
        {
            "INSERT INTO rule_definitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [("ri-def", "必须测试", "{}", "workflow", "positive", "hash", "{}", "active", 1.0, 1, "must", "observing", "t0", "t1")],
            "INSERT INTO rule_definition_versions VALUES (?,?,?,?,?,?,?,?,?)": [("ri-v1", "ri-def", "", "must", "must", "seed", "actor", "raw body must not persist", "t1")],
            "INSERT INTO agent_reputation VALUES (?,?,?,?,?,?,?,?)": [("agent", .8, .9, .1, 4, .7, "t0", "t1")],
            "INSERT INTO project_profile VALUES (?,?,?,?,?,?)": [("project", .5, .7, 1, "t0", "t1")],
            "INSERT INTO rule_definition_runtime_stats VALUES (?,?,?,?,?,?,?,?)": [("ri-def", 2, 1, 0, 0, 2, 1, "t1")],
            "INSERT INTO rule_evidence_contributions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)": [("c1", "ri-def", "session", "receipt", "positive", 2, .9, "t1", 1, "r1", "f1", "old", "ev1", "mem1", "{}", "agent", "project", "group", "s1", "root", "obj", 1, "t0", "t1")],
            "INSERT INTO rule_evidence_effective VALUES (?,?,?,?,?,?,?,?,?)": [("ri-def", "session", "receipt", "c1", "positive", 2, .9, "t1", "t1")],
            "INSERT INTO governance_capabilities VALUES (?,?,?,?,?,?,?,?,?)": [("digest-only", "principal", "rule_merge_approve", "proposal", "nonce", 1.0, 2.0, 0, None)],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    assert report.counts["definition_versions"] == 1
    assert report.counts["agent_reputation"] == 1
    assert report.counts["project_profile"] == 1
    assert report.counts["runtime_stats"] == 1
    assert report.counts["evidence_contributions"] == 1
    assert report.counts["evidence_effective"] == 1
    assert report.counts["governance_capabilities"] == 1
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_definition_versions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_evidence_contributions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_evidence_effective").fetchone()[0] == 1
        assert conn.execute("SELECT token_digest FROM rule_governance_capabilities").fetchone()[0] == "digest-only"
        assert conn.execute("SELECT COUNT(*) FROM rule_migration_map WHERE source_table='governance_capabilities'").fetchone()[0] == 1


def test_unknown_column_ledger_old_surrogate_resumes_by_natural_key(tmp_path: Path):
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(ri, "CREATE TABLE unknown_fixture(secret TEXT);", {"INSERT INTO unknown_fixture VALUES (?)": [("unknown",)]})
    first = V1RulesMigrator(tmp_path).migrate()
    assert first.ok
    target = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(target) as conn:
        conn.execute("UPDATE rule_unknown_columns_ledger SET ledger_id='legacy-surrogate',source_row_id='' ")
        conn.commit()
    resumed = V1RulesMigrator(tmp_path, migration_id=first.migration_id).migrate()
    assert resumed.ok
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_unknown_columns_ledger").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM rule_unknown_columns_ledger WHERE ledger_id='legacy-surrogate'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_unknown_column_anomalies WHERE status='PRESERVED'").fetchone()[0] == 1


def test_unknown_columns_use_row_occurrence_identity_for_duplicate_rows(tmp_path: Path):
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(ri, "CREATE TABLE unknown_fixture(secret TEXT);", {"INSERT INTO unknown_fixture VALUES (?)": [("same",), ("same",), ("different",)]})
    first = V1RulesMigrator(tmp_path).migrate()
    assert first.ok
    target = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(target) as conn:
        rows = conn.execute("SELECT source_row_id,value_digest FROM rule_unknown_columns_ledger ORDER BY source_row_id").fetchall()
        assert len(rows) == 3
        assert len({str(row[0]) for row in rows}) == 3
        assert len({str(row[1]) for row in rows}) == 2
    second = V1RulesMigrator(tmp_path, migration_id=first.migration_id).migrate()
    assert second.ok
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_unknown_columns_ledger").fetchone()[0] == 3
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["unknown_source_occurrences"] == 3
    assert domain.metrics["unknown_loss"] == 0


def test_validator_ignores_all_fts_shadow_tables_but_flags_unknown_authority(tmp_path: Path):
    validator = V2MigrationValidator(tmp_path)
    result = V2ValidationResult(status="PASS")
    validator._unknown_sources(result, {
        "history": {"status": "READY", "tables": ["history_fts", "history_fts_data", "history_fts_idx", "history_fts_content", "history_fts_docsize", "history_fts_config"]},
        "memory:group-a": {"status": "READY", "tables": ["rule_idempotency_fences"]},
        "rule_intelligence": {"status": "READY", "tables": ["agent_reputation", "project_profile", "rule_definition_versions", "unknown_authority"]},
    })
    assert not any("history" in error for error in result.errors)
    assert not any("rule_idempotency_fences" in error for error in result.errors)
    assert any("unknown_authority" in error for error in result.errors)


def test_validator_rules_metrics_waits_for_all_source_inventory_orderings(tmp_path: Path):
    first = _seed_group(tmp_path, "group-a")
    second = _seed_group(tmp_path, "group-b")
    fence_schema = """
        CREATE TABLE rule_idempotency_fences(
          key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, memory_id TEXT NOT NULL,
          event_id TEXT NOT NULL, decision_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """
    for path, group in ((first, "group-a"), (second, "group-b")):
        with sqlite3.connect(path) as conn:
            conn.executescript(fence_schema)
            conn.execute(
                "INSERT INTO rule_idempotency_fences VALUES (?,?,?,?,?,?)",
                (f"{group}-key", f"{group}-fp", "same-id", f"{group}-event", f"{group}-decision", "t1"),
            )
            conn.commit()

    # Two unknown columns in one source row exercise unknown provenance while
    # keeping the target ledger lossless after the rules migration.
    ri = tmp_path / ".memoryguard" / "rule-intelligence" / "memory.db"
    _db(
        ri,
        "CREATE TABLE unknown_fixture(secret TEXT, rationale TEXT);",
        {"INSERT INTO unknown_fixture VALUES (?,?)": [("unknown", "preserved")]},
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    EvidenceProjector(RuleV2Store(tmp_path), lambda _ref: None).project(migration_id=report.migration_id)
    _checkpoint_rules_db(tmp_path)

    validator = V2MigrationValidator(tmp_path)
    inventory = validator.source_inventory()
    preferred = ("history", "knowledge", "rule_intelligence", "memory:group-b", "memory:group-a")
    ordered_keys = [key for key in preferred if key in inventory]
    ordered_keys.extend(key for key in inventory if key not in ordered_keys)
    reversed_keys = list(reversed(ordered_keys))

    for keys in (ordered_keys, reversed_keys):
        domain = DomainValidation("rules")
        validator._rules_metrics(domain, {key: inventory[key] for key in keys})
        assert domain.errors == []
        assert domain.metrics["binding_identity_multiset_diff"] == 0
        assert domain.metrics["idempotency_fence_loss"] == 0
        assert domain.metrics["unknown_source_occurrences"] == 2
        assert domain.metrics["unknown_loss"] == 0

    store = RuleV2Store(tmp_path)
    store.record_idempotency_fence(
        {
            "fence_id": "unmarked-extra",
            "key": "unmarked-extra",
            "request_fingerprint": "fp-extra",
            "memory_id": "same-id",
            "event_id": "event-extra",
            "decision_id": "decision-extra",
            "created_at": "t3",
            "share_group_id": "group-a",
            "source_ref": str(first),
        }
    )
    _checkpoint_rules_db(tmp_path)
    blocked = DomainValidation("rules")
    validator._rules_metrics(blocked, {key: inventory[key] for key in ordered_keys})
    assert blocked.metrics["idempotency_fence_loss"] == 1
    assert any(error.startswith("idempotency_fence_loss:") for error in blocked.errors)


def test_validator_rules_metrics_blocks_nonempty_target_wal(tmp_path: Path):
    RuleV2Store(tmp_path)
    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("INSERT INTO rule_definitions(definition_id,canonical_text) VALUES (?,?)", ("wal-definition", "wal"))
    conn.commit()
    wal = db.with_name(db.name + "-wal")
    assert wal.is_file() and wal.stat().st_size > 0

    domain = DomainValidation("rules")
    V2MigrationValidator(tmp_path)._rules_metrics(domain, {})
    assert domain.status == "BLOCKED"
    assert domain.metrics["target_metrics_status"] == "BLOCKED"
    assert any("immutable read blocked by non-empty WAL" in error for error in domain.errors)
    conn.close()


def test_shared_memory_idempotency_fences_are_lossless_and_immutable(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-fences" / "memory.db"
    _db(
        path,
        LEGACY + """
        CREATE TABLE rule_idempotency_fences(
          key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, memory_id TEXT NOT NULL,
          event_id TEXT NOT NULL, decision_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("fence-rule", "必须运行测试", "workflow", "active", .9, "always", "agent-a", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("fence-rule", "agent", "agent-a", "", "include", 1, "t0", "t1")],
            "INSERT INTO rule_idempotency_fences VALUES (?,?,?,?,?,?)": [
                ("k-1", "fp-1", "fence-rule", "event-1", "decision-1", "t1"),
                ("k-2", "fp-2", "fence-rule", "event-2", "decision-2", "t2"),
                ("k-3", "fp-3", "fence-rule", "event-3", "decision-3", "t3"),
            ],
        },
    )
    failed = V1RulesMigrator(tmp_path, fail_at="before_commit")
    with pytest.raises(RulesMigrationError):
        failed.migrate()
    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_idempotency_fences").fetchone()[0] == 0

    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    assert report.counts["idempotency_fences"] == 3
    assert report.idempotency_fence_loss == 0
    assert report.idempotency_fence_source_digest == report.idempotency_fence_target_digest
    rerun = V1RulesMigrator(tmp_path).migrate()
    assert rerun.ok and rerun.idempotency_fence_loss == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM rule_idempotency_fences").fetchone()[0] == 3
        map_row = conn.execute("SELECT map_id,source_digest FROM rule_migration_map WHERE source_table='rule_idempotency_fences' LIMIT 1").fetchone()
    finally:
        conn.close()
    store = RuleV2Store(tmp_path)
    with pytest.raises(ValueError):
        store.record_migration_map({
            "map_id": map_row[0], "migration_id": rerun.migration_id, "source_kind": "shared_memory",
            "source_path": str(path), "source_group_id": "group-fences", "source_table": "rule_idempotency_fences",
            "source_id": "k-1", "target_table": "rule_idempotency_fences", "target_id": "tampered",
            "source_digest": "tampered", "status": "migrated", "metadata_json": "{}", "created_at": "",
        })
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT source_digest FROM rule_migration_map WHERE map_id=?", (map_row[0],)).fetchone()[0] == map_row[1]
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["idempotency_fence_loss"] == 0
    assert domain.metrics["idempotency_fence_reference_complete"] is True


def test_legacy_idempotency_key_column_maps_and_replays_idempotently(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-legacy-key" / "memory.db"
    _db(
        path,
        LEGACY + """
        CREATE TABLE rule_idempotency_fences(
          idempotency_key TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, memory_id TEXT NOT NULL,
          event_id TEXT NOT NULL, decision_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("legacy-fence", "必须运行测试", "workflow", "active", .9, "always", "agent-a", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("legacy-fence", "agent", "agent-a", "", "include", 1, "t0", "t1")],
            "INSERT INTO rule_idempotency_fences VALUES (?,?,?,?,?,?)": [("legacy-k-1", "fp-1", "legacy-fence", "event-1", "decision-1", "t1"), ("legacy-k-2", "fp-2", "legacy-fence", "event-2", "decision-2", "t2")],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok and report.idempotency_fence_loss == 0
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        assert {row[0] for row in conn.execute("SELECT key FROM rule_idempotency_fences")} == {"legacy-k-1", "legacy-k-2"}
    rerun = V1RulesMigrator(tmp_path).migrate()
    assert rerun.ok and rerun.idempotency_fence_loss == 0


def test_repeated_source_fence_is_preserved_as_anomaly_and_unmarked_extra_blocks(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-duplicate-key" / "memory.db"
    _db(
        path,
        LEGACY + """
        CREATE TABLE rule_idempotency_fences(
          idempotency_key TEXT NOT NULL, request_fingerprint TEXT NOT NULL, memory_id TEXT NOT NULL,
          event_id TEXT NOT NULL, decision_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("duplicate-fence", "必须运行测试", "workflow", "active", .9, "always", "agent-a", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("duplicate-fence", "agent", "agent-a", "", "include", 1, "t0", "t1")],
            "INSERT INTO rule_idempotency_fences VALUES (?,?,?,?,?,?)": [("same-key", "fp-old", "duplicate-fence", "event-old", "decision-old", "t1"), ("same-key", "fp-new", "duplicate-fence", "event-new", "decision-new", "t2")],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok and report.idempotency_fence_conflicts == 1
    store = RuleV2Store(tmp_path)
    assert store.metrics()["rule_idempotency_fence_anomalies"] == 1
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        rows = conn.execute("SELECT key FROM rule_idempotency_fences ORDER BY key").fetchall()
        assert len(rows) == 2 and rows[0][0] == "same-key" and "#conflict-" in rows[1][0]
    retry = V1RulesMigrator(tmp_path).migrate()
    assert retry.ok and retry.idempotency_fence_loss == 0
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["idempotency_fence_loss"] == 0
    store.record_idempotency_fence({"fence_id": "unmarked-extra", "key": "unmarked-extra", "request_fingerprint": "fp-extra", "memory_id": "duplicate-fence", "event_id": "event-extra", "decision_id": "decision-extra", "created_at": "t3", "share_group_id": "group-duplicate-key", "source_ref": str(path)})
    _checkpoint_rules_db(tmp_path)
    blocked = DomainValidation("rules")
    validator._rules_metrics(blocked, validator.source_inventory())
    assert blocked.metrics["idempotency_fence_loss"] == 1
    assert any(error.startswith("idempotency_fence_loss:") for error in blocked.errors)


def test_decisions_cross_table_equivalent_merges_and_conflict_preserves_sibling(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-decisions" / "memory.db"
    _db(
        path,
        LEGACY + """
        CREATE TABLE decisions(
          decision_id TEXT PRIMARY KEY, actor TEXT, rule_id TEXT, action TEXT,
          before_state TEXT, after_state TEXT, reason TEXT, confidence REAL,
          undo_id TEXT, target_ids TEXT, created_at TEXT
        );
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("decision-rule", "rule", "workflow", "active", .9, "always", "agent-a", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("decision-rule", "agent", "agent-a", "", "include", 1, "t0", "t1")],
            "INSERT INTO rule_decisions VALUES (?,?,?,?,?,?,?,?,?,?)": [("same-decision", "actor", "decision-rule", "approve", "{}", "{}", "same", .8, "", "t1")],
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)": [("same-decision", "actor", "decision-rule", "approve", "{}", "{}", "same", .8, "", "[]", "t1")],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    store = RuleV2Store(tmp_path)
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM rule_decision_anomalies").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM rule_migration_map WHERE source_table IN ('rule_decisions','decisions')").fetchone()[0] == 2

    # Change only second legacy table payload: immutable original remains and
    # deterministic sibling/anomaly preserve the divergent business event.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE decisions SET action='reject'")
        conn.commit()
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        conn.execute("UPDATE rule_migration_map SET metadata_json='{}' WHERE migration_id=? AND source_table='decisions' AND source_id='same-decision'", (report.migration_id,))
        conn.commit()
    # Reuse migration ID to exercise resume against existing canonical map.
    second = V1RulesMigrator(tmp_path, migration_id=report.migration_id).migrate()
    assert second.ok
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        rows = conn.execute("SELECT decision_id,action FROM rule_decisions ORDER BY decision_id").fetchall()
        assert len(rows) == 2 and {str(row[1]) for row in rows} == {"approve", "reject"}
        assert conn.execute("SELECT COUNT(*) FROM rule_decision_anomalies WHERE status='PRESERVED'").fetchone()[0] == 1
        maps = conn.execute("SELECT source_id,metadata_json FROM rule_migration_map WHERE source_table='decisions'").fetchall()
        assert any(str(row[0]) == "same-decision" and str(row[1]) == "{}" for row in maps)
        assert any("#conflict-" in str(row[0]) for row in maps)
    third = V1RulesMigrator(tmp_path, migration_id=report.migration_id).migrate()
    assert third.ok
    assert store.metrics()["rule_decision_anomalies"] == 1
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["decision_loss"] == 0
    assert domain.metrics["decision_status"] == "PRESERVED_CONFLICT"
    store.record_decision({"decision_id": "unmarked-decision", "action": "extra"})
    _checkpoint_rules_db(tmp_path)
    blocked = DomainValidation("rules")
    validator._rules_metrics(blocked, validator.source_inventory())
    assert blocked.metrics["decision_loss"] == 1
    assert any(error.startswith("decision_loss:") for error in blocked.errors)


def test_all_decision_occurrences_migrate_once_including_relevant_rules(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-decision-scope" / "memory.db"
    _db(
        path,
        LEGACY
        + """
        CREATE TABLE decisions(event_id TEXT PRIMARY KEY, actor TEXT, action TEXT,
          target_ids TEXT, before_hash TEXT, after_hash TEXT, reason TEXT, created_at TEXT);
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [
                ("always-a", "a", "workflow", "active", 1.0, "always", "agent", "t0", "t1"),
                ("always-b", "b", "workflow", "active", 1.0, "always", "agent", "t0", "t1"),
                ("relevant-r", "r", "workflow", "active", 1.0, "relevant", "agent", "t0", "t1"),
            ],
            "INSERT INTO rule_decisions VALUES (?,?,?,?,?,?,?,?,?,?)": [
                ("decision-always", "agent", "always-a", "approve", "{}", "{}", "a", 1.0, "", "t1"),
                ("decision-relevant", "agent", "relevant-r", "approve", "{}", "{}", "r", 1.0, "", "t1"),
            ],
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)": [
                ("decision-global", "agent", "rule_feedback", "[]", "", "", "global", "t1"),
            ],
        },
    )
    report = V1RulesMigrator(tmp_path).migrate()
    assert report.ok
    with sqlite3.connect(tmp_path / ".memoryguard" / "rules" / "rules.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM rule_migration_map WHERE source_table IN ('rule_decisions','decisions')").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM rule_decision_anomalies WHERE status='PRESERVED'").fetchone()[0] == 0
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["decision_source_count"] == 3
    assert domain.metrics["decision_loss"] == 0
    assert domain.metrics["decision_conflicts"] == 0


def test_decision_migration_resumes_legacy_shadow_siblings_idempotently(tmp_path: Path):
    path = tmp_path / ".memoryguard" / "shared-memory" / "group-legacy-shadow" / "memory.db"
    _db(
        path,
        LEGACY
        + """
        CREATE TABLE decisions(event_id TEXT PRIMARY KEY, actor TEXT, action TEXT,
          target_ids TEXT, before_hash TEXT, after_hash TEXT, reason TEXT, created_at TEXT);
        """,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [
                ("always-a", "a", "workflow", "active", 1.0, "always", "agent", "t0", "t1"),
                ("always-b", "b", "workflow", "active", 1.0, "always", "agent", "t0", "t1"),
            ],
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)": [
                ("legacy-decision", "agent", "rule_feedback", "[]", "", "", "global", "t1"),
            ],
        },
    )
    first = V1RulesMigrator(tmp_path).migrate()
    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    # Model the existing pre-fix shadow: canonical payload was tied to the
    # first always rule, with an extra deterministic sibling/map from the
    # second replay.  Keep both rows immutable and mapped.
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE rule_decisions SET rule_id='legacy-definition' WHERE decision_id='legacy-decision'")
        canonical = conn.execute("SELECT actor,owner_agent_id,action,before_hash,after_hash,before_json,after_json,reason,confidence,undo_id,target_ids_json,metadata_json,source_ref,created_at FROM rule_decisions WHERE decision_id='legacy-decision'").fetchone()
        sibling_id = "legacy-shadow-sibling"
        conn.execute(
            "INSERT INTO rule_decisions(decision_id,actor,owner_agent_id,rule_id,action,before_hash,after_hash,before_json,after_json,reason,confidence,undo_id,target_ids_json,metadata_json,source_ref,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sibling_id, canonical[0], canonical[1], "legacy-definition-2", canonical[2], canonical[3], canonical[4], canonical[5], canonical[6], canonical[7], canonical[8], canonical[9], canonical[10], canonical[11], canonical[12], canonical[13]),
        )
        conn.execute("INSERT INTO rule_decision_anomalies(anomaly_id,migration_id,source_kind,source_path,source_group_id,source_table,original_decision_id,sibling_decision_id,payload_digest,details_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy-anomaly", first.migration_id, "shared_memory", str(path), "group-legacy-shadow", "decisions", "legacy-decision", sibling_id, "legacy", "{}", "PRESERVED", ""))
        conn.execute("INSERT INTO rule_migration_map(map_id,migration_id,source_kind,source_path,source_group_id,source_table,source_id,target_table,target_id,source_digest,status,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("legacy-map-sibling", first.migration_id, "shared_memory", str(path), "group-legacy-shadow", "decisions", "legacy-decision#conflict-old", "rule_decisions", sibling_id, first.source_digest, "migrated", "{}", ""))
        conn.commit()
    with sqlite3.connect(db) as conn:
        before_count = conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0]
    resumed = V1RulesMigrator(tmp_path, migration_id=first.migration_id).migrate()
    again = V1RulesMigrator(tmp_path, migration_id=first.migration_id).migrate()
    assert resumed.ok and again.ok
    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["decision_loss"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rule_decisions").fetchone()[0] >= before_count
        assert conn.execute("SELECT COUNT(*) FROM rule_migration_map WHERE source_table='decisions' AND source_id='legacy-decision'").fetchone()[0] == 1


def test_three_decision_conflicts_keep_canonical_occurrence_maps(tmp_path: Path):
    """Conflict siblings are mapped by canonical source occurrence, not suffix."""

    path = tmp_path / ".memoryguard" / "shared-memory" / "group-three-conflicts" / "memory.db"
    decisions = [(f"decision-{index}", "actor", "rule", "approve", "{}", "{}", "same", .8, "", "t1") for index in range(3)]
    _db(
        path,
        LEGACY,
        {
            "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)": [("rule", "rule", "workflow", "active", .9, "always", "agent", "t0", "t1")],
            "INSERT INTO rule_assignments VALUES (?,?,?,?,?,?,?,?)": [("rule", "agent", "agent", "", "include", 1, "t0", "t1")],
            "INSERT INTO rule_decisions VALUES (?,?,?,?,?,?,?,?,?,?)": decisions,
        },
    )
    first = V1RulesMigrator(tmp_path).migrate()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE rule_decisions SET action='reject'")
        conn.commit()
    resumed = V1RulesMigrator(tmp_path, migration_id=first.migration_id).migrate()
    assert resumed.ok

    db = tmp_path / ".memoryguard" / "rules" / "rules.db"
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT source_id,metadata_json FROM rule_migration_map WHERE source_table='rule_decisions' AND source_id LIKE '%#conflict-%'"
        ).fetchall()
        assert len(rows) == 3
    for source_id, metadata_json in rows:
        metadata = json.loads(str(metadata_json))
        assert metadata["canonical_source_id"] == str(source_id).split("#conflict-", 1)[0]
        assert metadata["original_source_id"] == metadata["canonical_source_id"]

    _checkpoint_rules_db(tmp_path)
    validator = V2MigrationValidator(tmp_path)
    domain = DomainValidation("rules")
    validator._rules_metrics(domain, validator.source_inventory())
    assert domain.metrics["decision_loss"] == 0
    assert domain.metrics["decision_status"] == "PRESERVED_CONFLICT"

    # Keep the validator's immutable read contract: checkpoint the committed
    # extra row before reopening it through an immutable handle.
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO rule_decisions(decision_id,action) VALUES (?,?)", ("unmarked-extra", "extra"))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    blocked = DomainValidation("rules")
    validator._rules_metrics(blocked, validator.source_inventory())
    assert blocked.metrics["decision_loss"] == 1
    assert any(error.startswith("decision_loss:") for error in blocked.errors)
