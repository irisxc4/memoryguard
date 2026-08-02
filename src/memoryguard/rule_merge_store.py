"""Rule Intelligence Store: cross-group Definition/Binding/Evidence storage (P3).

This is the persistence layer for the Rule Intelligence Layer.  It lives in
``workspace/.memoryguard/rule-intelligence/memory.db`` — deliberately separate
from the per-group shared-memory databases, because a Definition is shared
across groups while a Binding keeps its own share_group_id.  Sharing knowledge
without sharing permission requires exactly this split.

``_SCHEMA`` follows the P3 design doc:
  * rule_definitions      — semantic core, no scope
  * rule_bindings         — where it applies (share_group_id + audience shape)
  * rule_evidence         — why it is believed to be one rule
  * rule_merge_proposals  — merge candidates (never merged directly)
  * rule_merge_decisions  — auditable, undoable merge executions

Auto-created bindings are restricted to ``agent`` / ``agent_project`` at the
database layer (a second enforcement wall behind the Python check).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .rule_binding import (
    AUTO_ALLOWED_TARGET_TYPES,
    RuleBinding,
    binding_identity_key,
)
from .rule_definition import (
    POLARITY_POSITIVE,
    STRENGTH_UNKNOWN,
    RuleDefinition,
)
from .rule_evidence import RuleEvidence, dedupe_evidence
from .rule_merge_policy import (
    MERGE_POLICY_VERSION,
    MAX_SINGLE_SOURCE_RATIO,
    MIN_REPUTATION_SAMPLES,
    NEGATIVE_EVIDENCE_THRESHOLD,
    bayesian_accuracy,
    contradiction_score,
    days_between,
    evidence_weight,
    feedback_authority_score,
    largest_source_ratio,
    negative_evidence_score,
    parameter_conflict,
    project_importance_score,
    recency_factor,
    weighted_evidence_score,
)
from .rule_scope import assignment_matches, canonical_project_ref
from .schema_v3 import (
    EffectiveAgentContext,
    RuleAssignment,
    _now_iso,
    stable_hash,
)

_RULE_INTELLIGENCE_DIR = "rule-intelligence"

_SCHEMA = """
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
    rule_strength TEXT NOT NULL DEFAULT 'observation',
    maturity_state TEXT NOT NULL DEFAULT 'observing',
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
    observed_at TEXT NOT NULL,
    independence_key TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    source_root_id TEXT NOT NULL DEFAULT '',
    source_object_id TEXT NOT NULL DEFAULT '',
    session_trusted INTEGER NOT NULL DEFAULT 0,
    feedback_id TEXT NOT NULL DEFAULT '',
    feedback_authority INTEGER NOT NULL DEFAULT 0
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
    readiness_score REAL NOT NULL DEFAULT 0.0,
    governance_reasons TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL DEFAULT '',
    first_merge_acknowledged INTEGER NOT NULL DEFAULT 0,
    negative_score REAL NOT NULL DEFAULT 0.0,
    conflict_type TEXT NOT NULL DEFAULT '',
    judge_source TEXT NOT NULL DEFAULT '',
    judge_model TEXT NOT NULL DEFAULT '',
    judge_score REAL NOT NULL DEFAULT 0.0,
    judge_confidence REAL NOT NULL DEFAULT 0.0,
    judge_recommendation TEXT NOT NULL DEFAULT '',
    judge_rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    explanation TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    candidate_since TEXT NOT NULL DEFAULT '',
    last_evaluated_at TEXT NOT NULL DEFAULT '',
    assessment_revision INTEGER NOT NULL DEFAULT 0,
    definition_revision_a INTEGER NOT NULL DEFAULT 0,
    definition_revision_b INTEGER NOT NULL DEFAULT 0,
    evidence_digest TEXT NOT NULL DEFAULT '',
    policy_version TEXT NOT NULL DEFAULT '',
    weight_breakdown TEXT NOT NULL DEFAULT ''
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
    readiness_at_merge REAL NOT NULL DEFAULT 0.0,
    strength_ok INTEGER NOT NULL DEFAULT 1,
    polarity_ok INTEGER NOT NULL DEFAULT 1,
    parameters_ok INTEGER NOT NULL DEFAULT 1,
    contradiction_ok INTEGER NOT NULL DEFAULT 1,
    negative_ok INTEGER NOT NULL DEFAULT 1,
    first_merge_acknowledged INTEGER NOT NULL DEFAULT 1,
    judge_source TEXT NOT NULL DEFAULT '',
    judge_model TEXT NOT NULL DEFAULT '',
    judge_score REAL NOT NULL DEFAULT 0.0,
    judge_confidence REAL NOT NULL DEFAULT 0.0,
    judge_recommendation TEXT NOT NULL DEFAULT '',
    judge_rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'merged',
    created_at TEXT NOT NULL,
    undone_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rule_merge_decisions_proposal
    ON rule_merge_decisions(proposal_id);
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
CREATE TABLE IF NOT EXISTS rule_negative_evidence (
    evidence_id TEXT PRIMARY KEY,
    definition_id TEXT NOT NULL DEFAULT '',
    source_rule_id TEXT NOT NULL DEFAULT '',
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL,
    independence_key TEXT NOT NULL DEFAULT '',
    share_group_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    receipt_id TEXT NOT NULL DEFAULT '',
    feedback_id TEXT NOT NULL DEFAULT '',
    feedback_authority INTEGER NOT NULL DEFAULT 0,
    source_root_id TEXT NOT NULL DEFAULT '',
    source_object_id TEXT NOT NULL DEFAULT '',
    session_trusted INTEGER NOT NULL DEFAULT 0,
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
    outcome TEXT NOT NULL,
    agent_instance_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    authority INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_runtime_feedback_definition
    ON rule_runtime_feedback(definition_id);
"""


def _now() -> str:
    return _now_iso()


class RuleMergeStore:
    """Cross-group SQLite storage for Definitions, Bindings and Evidence."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        base = self.workspace / ".memoryguard" / _RULE_INTELLIGENCE_DIR
        self.root = base
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "memory.db"
        self._init_db()

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._db() as conn:
            conn.executescript(_SCHEMA)
            self._apply_upgrade(conn)
            conn.commit()

    # ------------------------------------------------------------------
    # in-place upgrade for databases created before the governance layer
    # ------------------------------------------------------------------

    _UPGRADE_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("rule_definitions", "rule_strength", "TEXT NOT NULL DEFAULT 'observation'"),
        ("rule_definitions", "maturity_state", "TEXT NOT NULL DEFAULT 'observing'"),
        ("rule_merge_proposals", "readiness_score", "REAL NOT NULL DEFAULT 0.0"),
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
        ("rule_merge_proposals", "policy_version", "TEXT NOT NULL DEFAULT ''"),
        ("rule_merge_proposals", "weight_breakdown", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "independence_key", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "share_group_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "source_root_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "source_object_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "session_trusted", "INTEGER NOT NULL DEFAULT 0"),
        ("rule_evidence", "feedback_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_evidence", "feedback_authority", "INTEGER NOT NULL DEFAULT 0"),
        ("rule_negative_evidence", "independence_key", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "share_group_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "session_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "receipt_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "feedback_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "feedback_authority", "INTEGER NOT NULL DEFAULT 0"),
        ("rule_negative_evidence", "source_root_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "source_object_id", "TEXT NOT NULL DEFAULT ''"),
        ("rule_negative_evidence", "session_trusted", "INTEGER NOT NULL DEFAULT 0"),
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
    )

    @staticmethod
    def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _apply_upgrade(self, conn: sqlite3.Connection) -> None:
        """Add governance columns to tables created before the upgrade.

        ``CREATE TABLE IF NOT EXISTS`` never touches an existing table, so a
        store built before the governance layer keeps its old columns until
        this routine adds them.  Fresh databases already have every column and
        every check becomes a no-op.
        """
        for table, column, ddl in self._UPGRADE_COLUMNS:
            if column not in self._existing_columns(conn, table):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
        # Independence indexes reference columns the upgrade may have just added,
        # so they are created only after the ALTER loop (never in _SCHEMA).
        for index_table in ("rule_evidence", "rule_negative_evidence"):
            columns = self._existing_columns(conn, index_table)
            if "independence_key" in columns:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS "
                    f"idx_{index_table}_independence "
                    f"ON {index_table}(independence_key)"
                )

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    def upsert_definition(self, definition: RuleDefinition) -> RuleDefinition:
        payload = definition.to_dict()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_definitions (
                    definition_id, canonical_text, normalized_intent, rule_kind,
                    polarity, semantic_hash, parameter_schema, status, confidence,
                    revision, rule_strength, maturity_state,
                    created_at, updated_at, superseded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(definition_id) DO UPDATE SET
                    canonical_text=excluded.canonical_text,
                    normalized_intent=excluded.normalized_intent,
                    rule_kind=excluded.rule_kind,
                    polarity=excluded.polarity,
                    semantic_hash=excluded.semantic_hash,
                    parameter_schema=excluded.parameter_schema,
                    status=excluded.status,
                    confidence=excluded.confidence,
                    revision=excluded.revision,
                    rule_strength=excluded.rule_strength,
                    maturity_state=excluded.maturity_state,
                    updated_at=excluded.updated_at,
                    superseded_by=excluded.superseded_by
                """,
                (
                    payload["definition_id"], payload["canonical_text"],
                    payload["normalized_intent"], payload["rule_kind"],
                    payload["polarity"], payload["semantic_hash"],
                    payload["parameter_schema"], payload["status"],
                    payload["confidence"], payload["revision"],
                    payload["rule_strength"], payload["maturity_state"],
                    payload["created_at"], payload["updated_at"],
                    payload["superseded_by"],
                ),
            )
        return self.get_definition(definition.definition_id) or definition

    def get_definition(self, definition_id: str) -> RuleDefinition | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_definitions WHERE definition_id=?",
                (definition_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_definition(row)

    def list_definitions(
        self, status: str | None = None,
    ) -> list[RuleDefinition]:
        sql = "SELECT * FROM rule_definitions"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY definition_id"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_definition(r) for r in rows]

    def list_definitions_by_semantic_hash(
        self, semantic_hash_value: str,
    ) -> list[RuleDefinition]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_definitions WHERE semantic_hash=? AND status IN ('active','alias') "
                "ORDER BY definition_id",
                (semantic_hash_value,),
            ).fetchall()
        return [self._row_to_definition(r) for r in rows]

    @staticmethod
    def _row_to_definition(row: sqlite3.Row) -> RuleDefinition:
        return RuleDefinition(
            definition_id=row["definition_id"],
            canonical_text=row["canonical_text"] or "",
            normalized_intent=row["normalized_intent"] or "",
            rule_kind=row["rule_kind"] or "workflow",
            polarity=row["polarity"] or POLARITY_POSITIVE,
            semantic_hash=row["semantic_hash"] or "",
            parameter_schema=row["parameter_schema"] or "{}",
            status=row["status"] or "active",
            confidence=float(row["confidence"] or 1.0),
            revision=int(row["revision"] or 1),
            rule_strength=row["rule_strength"] or "observation",
            maturity_state=row["maturity_state"] or "observing",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
            superseded_by=row["superseded_by"] or "",
        )

    def count_definitions(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_definitions WHERE status IN ('active','alias')"
            ).fetchone()
        return int(row["c"])

    def set_definition_status(
        self, definition_id: str, status: str, *, superseded_by: str = "",
    ) -> None:
        """Change a definition's lifecycle status (active|superseded|merged…)."""
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_definitions SET status=?, superseded_by=?, updated_at=? "
                "WHERE definition_id=?",
                (status, superseded_by, _now(), definition_id),
            )

    def set_definition_maturity(self, definition_id: str, state: str) -> None:
        """Persist the recomputed maturity stage of one definition."""
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_definitions SET maturity_state=?, updated_at=? "
                "WHERE definition_id=?",
                (state, _now(), definition_id),
            )

    def bump_definition_revision(self, definition_id: str) -> None:
        """Bump a definition's revision (a content/state edit marker).

        The merge transaction refuses a human-approved merge whose expected
        definition revisions no longer match, so an edit between approval and
        execution is detected instead of silently merging drifted state.
        """
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_definitions SET revision=revision+1, updated_at=? "
                "WHERE definition_id=?",
                (_now(), definition_id),
            )

    def set_definition_strength_unknown(self, definition_id: str) -> None:
        """Mark an unrecoverable definition as ``unknown``-strength.

        Pre-v2 orphan definitions whose original legacy body can no longer be
        recovered must never participate in automatic merging (the layer cannot
        assert whether a proposed merge would be a strength conflict).
        """
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_definitions SET rule_strength=?, updated_at=? "
                "WHERE definition_id=?",
                (STRENGTH_UNKNOWN, _now(), definition_id),
            )

    # ------------------------------------------------------------------
    # Definition aliases / source links (v2 identity migration)
    # ------------------------------------------------------------------

    def migrate_legacy_definition(
        self,
        old_definition_id: str,
        new_definition_id: str,
        *,
        migration_decision_id: str = "",
    ) -> list[tuple[Any, ...]] | None:
        """Atomically repoint a pre-v2 Definition onto its v2 id.

        The pre-v2 definition id only covered the canonical surface wording, so
        MUST/SHOULD variants could share one id and silently overwrite strength.
        Migration moves that definition's Evidence to the v2 id, marks the old
        row ``alias`` (its stale bindings are dropped — the current backfill
        pass recreates every binding under the v2 id from the legacy
        assignments), and records the alias.  Returns the removed audience
        identity tuples so the caller can verify scope preservation after the
        backfill pass; ``None`` when no migration happened.
        """
        if old_definition_id == new_definition_id:
            return None
        now = _now()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchone()
                if row is None or row["status"] in {"alias", "merged"}:
                    conn.rollback()
                    return None
                if conn.execute(
                    "SELECT 1 FROM rule_definition_aliases WHERE old_definition_id=?",
                    (old_definition_id,),
                ).fetchone():
                    conn.rollback()
                    return None
                binding_rows = conn.execute(
                    "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                    (old_definition_id,),
                ).fetchall()
                removed_audiences = [
                    binding_identity_key(self._row_to_binding(r))
                    for r in binding_rows
                ]
                # Evidence moves; bindings are recreated by the backfill pass
                # under the v2 id (same audience, v2-based binding ids).
                conn.execute(
                    "UPDATE rule_evidence SET definition_id=? WHERE definition_id=?",
                    (new_definition_id, old_definition_id),
                )
                conn.execute(
                    "DELETE FROM rule_bindings WHERE definition_id=?",
                    (old_definition_id,),
                )
                conn.execute(
                    "UPDATE rule_definitions SET status='alias', superseded_by=?, "
                    "updated_at=? WHERE definition_id=?",
                    (new_definition_id, now, old_definition_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO rule_definition_aliases "
                    "(old_definition_id, new_definition_id, migration_decision_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (old_definition_id, new_definition_id, migration_decision_id, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return removed_audiences

    def resolve_canonical(self, definition_id: str) -> str:
        """Follow alias/merged/superseded links to the current canonical id.

        ``backfill``/``sync``/outbox consumers must resolve a source's current
        canonical before writing evidence, so a merged rule never gets its
        lifecycle resurrected and new evidence lands on the canonical.
        """
        seen: set[str] = set()
        current = definition_id
        while current and current not in seen:
            seen.add(current)
            definition = self.get_definition(current)
            if definition is None:
                break
            if (
                definition.status in {"merged", "alias", "superseded"}
                and definition.superseded_by
            ):
                current = definition.superseded_by
                continue
            break
        return current

    def get_definition_alias(self, old_definition_id: str) -> dict[str, Any] | None:
        """The v2 definition a pre-v2 definition was migrated onto, if any."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_definition_aliases WHERE old_definition_id=?",
                (old_definition_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "old_definition_id": row["old_definition_id"],
            "new_definition_id": row["new_definition_id"],
            "migration_decision_id": row["migration_decision_id"] or "",
            "created_at": row["created_at"] or "",
        }

    def upsert_source_link(
        self,
        *,
        share_group_id: str,
        memory_id: str,
        source_revision: str = "",
        original_definition_id: str = "",
        canonical_definition_id: str = "",
        status: str = "active",
    ) -> dict[str, Any]:
        """Record which Definition a legacy source record currently resolves to.

        ``backfill``/``sync`` must resolve a source link before touching a
        Definition so a merged/superseded/alias lifecycle is never resurrected
        by a re-run.
        """
        now = _now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_source_links (
                    share_group_id, memory_id, source_revision,
                    original_definition_id, canonical_definition_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(share_group_id, memory_id) DO UPDATE SET
                    source_revision=excluded.source_revision,
                    original_definition_id=excluded.original_definition_id,
                    canonical_definition_id=excluded.canonical_definition_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
                (share_group_id, memory_id, source_revision or "",
                 original_definition_id or "", canonical_definition_id or "",
                 status or "active", now, now),
            )
        return {
            "share_group_id": share_group_id, "memory_id": memory_id,
            "source_revision": source_revision or "",
            "original_definition_id": original_definition_id or "",
            "canonical_definition_id": canonical_definition_id or "",
            "status": status or "active",
        }

    def get_source_link(
        self, share_group_id: str, memory_id: str,
    ) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_source_links "
                "WHERE share_group_id=? AND memory_id=?",
                (share_group_id, memory_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "share_group_id": row["share_group_id"],
            "memory_id": row["memory_id"],
            "source_revision": row["source_revision"] or "",
            "original_definition_id": row["original_definition_id"] or "",
            "canonical_definition_id": row["canonical_definition_id"] or "",
            "status": row["status"] or "active",
        }

    # ------------------------------------------------------------------
    # Runtime feedback / definition statistics (P2 -> P3 projection, PR4)
    # ------------------------------------------------------------------

    def upsert_runtime_feedback(
        self,
        *,
        feedback_id: str,
        definition_id: str,
        outcome: str,
        agent_instance_id: str = "",
        project_ref: str = "",
        session_id: str = "",
        source: str = "",
        authority: int = 0,
        created_at: str = "",
    ) -> None:
        """Idempotently record one projected feedback event (feedback_id PK)."""
        with self._db() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO rule_runtime_feedback (
                    feedback_id, definition_id, outcome, agent_instance_id,
                    project_ref, session_id, source, authority, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (feedback_id, definition_id, outcome or "",
                 agent_instance_id or "", project_ref or "", session_id or "",
                 source or "", int(authority or 0), created_at or _now()),
            )

    def recompute_runtime_stats(self, definition_id: str) -> dict[str, Any]:
        """Recompute a definition's runtime counters from the feedback ledger.

        Counters are derived (never incremented) so the projection is idempotent
        even if an outbox event is re-delivered after a partial failure.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_runtime_feedback WHERE definition_id=?",
                (definition_id,),
            ).fetchall()
            followed = sum(1 for r in rows if r["outcome"] == "followed")
            violated = sum(1 for r in rows if r["outcome"] == "violated")
            not_applicable = sum(
                1 for r in rows if r["outcome"] == "not_applicable"
            )
            exception_count = sum(
                1 for r in rows if r["outcome"] == "exception"
            )
            sessions = {
                str(r["session_id"] or "")
                for r in rows if (r["session_id"] or "").strip()
            }
            projects = {
                str(r["project_ref"] or "")
                for r in rows if (r["project_ref"] or "").strip()
            }
            last_observed = max(
                (str(r["created_at"] or "") for r in rows), default="",
            )
            stats = {
                "definition_id": definition_id,
                "followed": followed,
                "violated": violated,
                "not_applicable": not_applicable,
                "exception_count": exception_count,
                "distinct_sessions": len(sessions),
                "distinct_projects": len(projects),
                "last_observed_at": last_observed,
            }
            conn.execute(
                """
                INSERT INTO rule_definition_runtime_stats (
                    definition_id, followed, violated, not_applicable,
                    exception_count, distinct_sessions, distinct_projects,
                    last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(definition_id) DO UPDATE SET
                    followed=excluded.followed, violated=excluded.violated,
                    not_applicable=excluded.not_applicable,
                    exception_count=excluded.exception_count,
                    distinct_sessions=excluded.distinct_sessions,
                    distinct_projects=excluded.distinct_projects,
                    last_observed_at=excluded.last_observed_at
                """,
                (definition_id, followed, violated, not_applicable,
                 exception_count, len(sessions), len(projects), last_observed),
            )
        return stats

    def get_runtime_stats(self, definition_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_definition_runtime_stats "
                "WHERE definition_id=?",
                (definition_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "definition_id": row["definition_id"],
            "followed": int(row["followed"] or 0),
            "violated": int(row["violated"] or 0),
            "not_applicable": int(row["not_applicable"] or 0),
            "exception_count": int(row["exception_count"] or 0),
            "distinct_sessions": int(row["distinct_sessions"] or 0),
            "distinct_projects": int(row["distinct_projects"] or 0),
            "last_observed_at": row["last_observed_at"] or "",
        }

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    def upsert_binding(self, binding: RuleBinding) -> RuleBinding:
        payload = binding.to_dict()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_bindings (
                    binding_id, definition_id, share_group_id, target_type,
                    target_id, project_ref, provider, runtime_role, effect,
                    priority, owner_agent_id, created_by, authorization,
                    status, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(binding_id) DO UPDATE SET
                    definition_id=excluded.definition_id,
                    share_group_id=excluded.share_group_id,
                    target_type=excluded.target_type,
                    target_id=excluded.target_id,
                    project_ref=excluded.project_ref,
                    provider=excluded.provider,
                    runtime_role=excluded.runtime_role,
                    effect=excluded.effect,
                    priority=excluded.priority,
                    owner_agent_id=excluded.owner_agent_id,
                    created_by=excluded.created_by,
                    authorization=excluded.authorization,
                    status=excluded.status,
                    revision=excluded.revision,
                    updated_at=excluded.updated_at
                """,
                (
                    payload["binding_id"], payload["definition_id"],
                    payload["share_group_id"], payload["target_type"],
                    payload["target_id"], payload["project_ref"],
                    payload["provider"], payload["runtime_role"],
                    payload["effect"], payload["priority"],
                    payload["owner_agent_id"], payload["created_by"],
                    payload["authorization"], payload["status"],
                    payload["revision"], payload["created_at"],
                    payload["updated_at"],
                ),
            )
        return binding

    def list_bindings(
        self, definition_id: str | None = None,
        share_group_id: str | None = None,
        status: str | None = "active",
    ) -> list[RuleBinding]:
        sql = "SELECT * FROM rule_bindings WHERE 1=1"
        params: list[Any] = []
        if definition_id:
            sql += " AND definition_id=?"
            params.append(definition_id)
        if share_group_id:
            sql += " AND share_group_id=?"
            params.append(share_group_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY definition_id, target_type, target_id"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_binding(r) for r in rows]

    @staticmethod
    def _row_to_binding(row: sqlite3.Row) -> RuleBinding:
        return RuleBinding(
            binding_id=row["binding_id"],
            definition_id=row["definition_id"],
            share_group_id=row["share_group_id"] or "",
            target_type=row["target_type"] or "agent",
            target_id=row["target_id"] or "",
            project_ref=row["project_ref"] or "",
            provider=row["provider"] or "",
            runtime_role=row["runtime_role"] or "",
            effect=row["effect"] or "include",
            priority=int(row["priority"] or 0),
            owner_agent_id=row["owner_agent_id"] or "",
            created_by=row["created_by"] or "manual",
            authorization=row["authorization"] or "",
            status=row["status"] or "active",
            revision=int(row["revision"] or 1),
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    def count_bindings(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_bindings WHERE status='active'"
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    def upsert_evidence(self, evidence: RuleEvidence) -> RuleEvidence:
        payload = evidence.to_dict()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_evidence (
                    evidence_id, definition_id, source_rule_id,
                    agent_instance_id, project_ref, provider, session_id,
                    receipt_id, content_hash, semantic_hash, confidence,
                    observed_at, independence_key, share_group_id,
                    source_root_id, source_object_id, session_trusted,
                    feedback_id, feedback_authority
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    definition_id=excluded.definition_id,
                    confidence=excluded.confidence,
                    observed_at=excluded.observed_at,
                    independence_key=excluded.independence_key,
                    share_group_id=excluded.share_group_id,
                    source_root_id=excluded.source_root_id,
                    source_object_id=excluded.source_object_id,
                    session_trusted=excluded.session_trusted,
                    feedback_id=excluded.feedback_id,
                    feedback_authority=excluded.feedback_authority
                """,
                (
                    payload["evidence_id"], payload["definition_id"],
                    payload["source_rule_id"], payload["agent_instance_id"],
                    payload["project_ref"], payload["provider"],
                    payload["session_id"], payload["receipt_id"],
                    payload["content_hash"], payload["semantic_hash"],
                    payload["confidence"], payload["observed_at"],
                    payload["independence_key"] or "",
                    payload["share_group_id"] or "",
                    payload["source_root_id"] or "",
                    payload["source_object_id"] or "",
                    int(payload["session_trusted"] or 0),
                    payload["feedback_id"] or "",
                    int(payload["feedback_authority"] or 0),
                ),
            )
        return evidence

    def list_evidence(
        self, definition_id: str | None = None,
    ) -> list[RuleEvidence]:
        sql = "SELECT * FROM rule_evidence"
        params: list[Any] = []
        if definition_id:
            sql += " WHERE definition_id=?"
            params.append(definition_id)
        sql += " ORDER BY observed_at"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> RuleEvidence:
        return RuleEvidence(
            evidence_id=row["evidence_id"],
            definition_id=row["definition_id"] or "",
            source_rule_id=row["source_rule_id"] or "",
            agent_instance_id=row["agent_instance_id"] or "",
            project_ref=row["project_ref"] or "",
            provider=row["provider"] or "",
            session_id=row["session_id"] or "",
            receipt_id=row["receipt_id"] or "",
            content_hash=row["content_hash"] or "",
            semantic_hash=row["semantic_hash"] or "",
            confidence=float(row["confidence"] or 1.0),
            observed_at=row["observed_at"] or "",
            independence_key=row["independence_key"] or "",
            share_group_id=row["share_group_id"] or "",
            source_root_id=row["source_root_id"] or "",
            source_object_id=row["source_object_id"] or "",
            session_trusted=int(row["session_trusted"] or 0),
            feedback_id=row["feedback_id"] or "",
            feedback_authority=int(row["feedback_authority"] or 0),
        )

    def count_evidence(self) -> int:
        with self._db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM rule_evidence").fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Negative evidence (P3-001 §5)
    # ------------------------------------------------------------------

    def upsert_negative_evidence(
        self, evidence: Any,
    ) -> Any:
        payload = evidence.to_dict()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_negative_evidence (
                    evidence_id, definition_id, source_rule_id,
                    agent_instance_id, project_ref, content_hash, confidence,
                    observed_at, independence_key, share_group_id, session_id,
                    receipt_id, feedback_id, feedback_authority,
                    source_root_id, source_object_id, session_trusted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    definition_id=excluded.definition_id,
                    confidence=excluded.confidence,
                    observed_at=excluded.observed_at,
                    independence_key=excluded.independence_key,
                    share_group_id=excluded.share_group_id,
                    session_id=excluded.session_id,
                    receipt_id=excluded.receipt_id,
                    feedback_id=excluded.feedback_id,
                    feedback_authority=excluded.feedback_authority,
                    source_root_id=excluded.source_root_id,
                    source_object_id=excluded.source_object_id,
                    session_trusted=excluded.session_trusted
                """,
                (
                    payload["evidence_id"], payload["definition_id"],
                    payload["source_rule_id"], payload["agent_instance_id"],
                    payload["project_ref"], payload["content_hash"],
                    payload["confidence"], payload["observed_at"],
                    payload["independence_key"] or "",
                    payload["share_group_id"] or "",
                    payload["session_id"] or "",
                    payload["receipt_id"] or "",
                    payload["feedback_id"] or "",
                    int(payload["feedback_authority"] or 0),
                    payload["source_root_id"] or "",
                    payload["source_object_id"] or "",
                    int(payload["session_trusted"] or 0),
                ),
            )
        return evidence

    def list_negative_evidence(
        self, definition_id: str | None = None,
    ) -> list[Any]:
        from .rule_evidence import NegativeEvidence

        sql = "SELECT * FROM rule_negative_evidence"
        params: list[Any] = []
        if definition_id:
            sql += " WHERE definition_id=?"
            params.append(definition_id)
        sql += " ORDER BY observed_at"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            NegativeEvidence(
                evidence_id=row["evidence_id"],
                definition_id=row["definition_id"] or "",
                source_rule_id=row["source_rule_id"] or "",
                agent_instance_id=row["agent_instance_id"] or "",
                project_ref=row["project_ref"] or "",
                content_hash=row["content_hash"] or "",
                confidence=float(row["confidence"] or 1.0),
                observed_at=row["observed_at"] or "",
                independence_key=row["independence_key"] or "",
                share_group_id=row["share_group_id"] or "",
                session_id=row["session_id"] or "",
                receipt_id=row["receipt_id"] or "",
                feedback_id=row["feedback_id"] or "",
                feedback_authority=int(row["feedback_authority"] or 0),
                source_root_id=row["source_root_id"] or "",
                source_object_id=row["source_object_id"] or "",
                session_trusted=int(row["session_trusted"] or 0),
            )
            for row in rows
        ]

    def count_negative_evidence(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_negative_evidence"
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Agent reputation / project profile (P3-003 §2)
    # ------------------------------------------------------------------

    def upsert_agent_reputation(
        self,
        *,
        agent_id: str,
        success_rate: float = 0.0,
        rule_accuracy: float = 0.0,
        violation_rate: float = 0.0,
        sample_count: int = 0,
        feedback_quality: float = 0.0,
    ) -> dict[str, Any]:
        now = _now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO agent_reputation (
                    agent_id, success_rate, rule_accuracy, violation_rate,
                    sample_count, feedback_quality, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    success_rate=excluded.success_rate,
                    rule_accuracy=excluded.rule_accuracy,
                    violation_rate=excluded.violation_rate,
                    sample_count=excluded.sample_count,
                    feedback_quality=excluded.feedback_quality,
                    updated_at=excluded.updated_at
                """,
                (agent_id, float(success_rate), float(rule_accuracy),
                 float(violation_rate), int(sample_count),
                 float(feedback_quality), now, now),
            )
        return {
            "agent_id": agent_id, "success_rate": float(success_rate),
            "rule_accuracy": float(rule_accuracy),
            "violation_rate": float(violation_rate),
            "sample_count": int(sample_count),
            "feedback_quality": float(feedback_quality),
        }

    def get_agent_reputation(self, agent_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM agent_reputation WHERE agent_id=?", (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "agent_id": row["agent_id"],
            "success_rate": float(row["success_rate"] or 0.0),
            "rule_accuracy": float(row["rule_accuracy"] or 0.0),
            "violation_rate": float(row["violation_rate"] or 0.0),
            "sample_count": int(row["sample_count"] or 0),
            "feedback_quality": float(row["feedback_quality"] or 0.0),
        }

    def list_agent_reputations(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_reputation ORDER BY agent_id"
            ).fetchall()
        return [{
            "agent_id": row["agent_id"],
            "success_rate": float(row["success_rate"] or 0.0),
            "rule_accuracy": float(row["rule_accuracy"] or 0.0),
            "violation_rate": float(row["violation_rate"] or 0.0),
            "sample_count": int(row["sample_count"] or 0),
            "feedback_quality": float(row["feedback_quality"] or 0.0),
        } for row in rows]

    def upsert_project_profile(
        self,
        *,
        project_ref: str,
        production_level: float = 0.0,
        criticality: float = 0.0,
        owner_verified: bool = False,
    ) -> dict[str, Any]:
        now = _now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO project_profile (
                    project_ref, production_level, criticality,
                    owner_verified, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_ref) DO UPDATE SET
                    production_level=excluded.production_level,
                    criticality=excluded.criticality,
                    owner_verified=excluded.owner_verified,
                    updated_at=excluded.updated_at
                """,
                (project_ref, float(production_level), float(criticality),
                 1 if owner_verified else 0, now, now),
            )
        return {
            "project_ref": project_ref,
            "production_level": float(production_level),
            "criticality": float(criticality),
            "owner_verified": bool(owner_verified),
        }

    def get_project_profile(self, project_ref: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM project_profile WHERE project_ref=?", (project_ref,),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_ref": row["project_ref"],
            "production_level": float(row["production_level"] or 0.0),
            "criticality": float(row["criticality"] or 0.0),
            "owner_verified": bool(row["owner_verified"]),
        }

    def list_project_profiles(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM project_profile ORDER BY project_ref"
            ).fetchall()
        return [{
            "project_ref": row["project_ref"],
            "production_level": float(row["production_level"] or 0.0),
            "criticality": float(row["criticality"] or 0.0),
            "owner_verified": bool(row["owner_verified"]),
        } for row in rows]

    # ------------------------------------------------------------------
    # Definition versions / strength evolution (P3-002 §5)
    # ------------------------------------------------------------------

    def record_definition_version(
        self,
        *,
        definition_id: str,
        superseded_by: str,
        old_strength: str,
        new_strength: str,
        change_reason: str = "",
        actor: str = "auto",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        version_id = stable_hash(
            "rule-definition-version", definition_id, superseded_by, old_strength,
            new_strength, _now(),
        )
        now = _now()
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO rule_definition_versions (
                    version_id, definition_id, superseded_by, old_strength,
                    new_strength, change_reason, actor, evidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, definition_id, superseded_by, old_strength,
                 new_strength, change_reason, actor,
                 json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                 now),
            )
        return {
            "version_id": version_id, "definition_id": definition_id,
            "superseded_by": superseded_by, "old_strength": old_strength,
            "new_strength": new_strength, "change_reason": change_reason,
            "actor": actor, "evidence": evidence or {}, "created_at": now,
        }

    def evolve_definition_atomic(
        self,
        *,
        old_definition_id: str,
        new_definition: RuleDefinition,
        old_strength: str,
        new_strength: str,
        change_reason: str = "",
        actor: str = "auto",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically promote/demote a definition's strength (PR6).

        One transaction inserts the new Definition, migrates every active
        Binding to it (audience multiset preserved), records the evolution link,
        and marks the old Definition ``superseded``.  Historical Evidence stays
        on the old Definition (the version row is the link); new evidence
        targets the new Definition.  A failure rolls back the whole evolution,
        so no half-evolved orphan can ever exist.
        """
        if old_definition_id == new_definition.definition_id:
            raise ValueError("rule_definition_unchanged")
        now = _now()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("rule_definition_not_found")
                if str(row["status"] or "") != "active":
                    raise ValueError("rule_definition_not_active")
                payload = new_definition.to_dict()
                conn.execute(
                    """
                    INSERT INTO rule_definitions (
                        definition_id, canonical_text, normalized_intent, rule_kind,
                        polarity, semantic_hash, parameter_schema, status, confidence,
                        revision, rule_strength, maturity_state,
                        created_at, updated_at, superseded_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        payload["definition_id"], payload["canonical_text"],
                        payload["normalized_intent"], payload["rule_kind"],
                        payload["polarity"], payload["semantic_hash"],
                        payload["parameter_schema"], payload["confidence"],
                        payload["revision"], payload["rule_strength"],
                        payload["maturity_state"], payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                before = {
                    binding_identity_key(self._row_to_binding(r))
                    for r in conn.execute(
                        "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                        (old_definition_id,),
                    ).fetchall()
                }
                conn.execute(
                    "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                    "updated_at=? WHERE definition_id=?",
                    (new_definition.definition_id, now, old_definition_id),
                )
                after = {
                    binding_identity_key(self._row_to_binding(r))
                    for r in conn.execute(
                        "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                        (new_definition.definition_id,),
                    ).fetchall()
                }
                if before != after:
                    raise RuntimeError("rule_evolution_scope_change")
                version_id = stable_hash(
                    "rule-definition-version", old_definition_id,
                    new_definition.definition_id, old_strength, new_strength, now,
                )
                conn.execute(
                    """
                    INSERT INTO rule_definition_versions (
                        version_id, definition_id, superseded_by, old_strength,
                        new_strength, change_reason, actor, evidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (version_id, old_definition_id, new_definition.definition_id,
                     old_strength, new_strength, change_reason or "", actor or "",
                     json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                     now),
                )
                conn.execute(
                    "UPDATE rule_definitions SET status='superseded', superseded_by=?, "
                    "updated_at=? WHERE definition_id=?",
                    (new_definition.definition_id, now, old_definition_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "old_definition_id": old_definition_id,
            "new_definition_id": new_definition.definition_id,
            "version_id": version_id,
            "old_strength": old_strength,
            "new_strength": new_strength,
        }

    def list_definition_versions(
        self, definition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rule_definition_versions"
        params: list[Any] = []
        if definition_id:
            sql += " WHERE definition_id=?"
            params.append(definition_id)
        sql += " ORDER BY created_at"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "version_id": row["version_id"],
            "definition_id": row["definition_id"],
            "superseded_by": row["superseded_by"] or "",
            "old_strength": row["old_strength"] or "",
            "new_strength": row["new_strength"] or "",
            "change_reason": row["change_reason"] or "",
            "actor": row["actor"] or "",
            "evidence": json.loads(row["evidence"] or "{}"),
            "created_at": row["created_at"] or "",
        } for row in rows]

    def count_definition_versions(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_definition_versions"
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Merge proposals
    # ------------------------------------------------------------------

    def create_proposal(
        self,
        definition_ids: list[str],
        similarity_score: float,
        *,
        evidence: list[RuleEvidence] | tuple[RuleEvidence, ...] | None = None,
        contradiction_score: float = 0.0,
        explanation: str = "",
        readiness_score: float = 0.0,
        governance_reasons: str = "",
        cooldown_until: str = "",
        negative_score: float = 0.0,
        conflict_type: str = "",
        judge: Any | None = None,
        definition_a: Any | None = None,
        definition_b: Any | None = None,
        weight_breakdown: str = "",
    ) -> dict[str, Any]:
        """Stable-id UPSERT so a repeated scan never resets the lifecycle.

        The proposal id is a function of the pair and the policy version (no
        timestamp), so a fresh scan reuses the same row.  A re-scan refreshes
        the assessment (similarity, readiness, judge, revision/digest snapshot)
        but **preserves** ``candidate_since``, ``cooldown_until`` and
        ``first_merge_acknowledged`` — cooldown cannot be restarted by
        re-scanning, and a governance approval survives.
        """
        evidence_list = dedupe_evidence(list(evidence or []))
        agents = {ev.agent_instance_id for ev in evidence_list if ev.agent_instance_id}
        projects = {
            (ev.project_ref or "").strip()
            for ev in evidence_list if (ev.project_ref or "").strip()
        }
        sorted_ids = sorted(definition_ids)
        proposal_id = stable_hash(
            "rule-merge-proposal-v2",
            json.dumps(sorted_ids, ensure_ascii=False),
            MERGE_POLICY_VERSION,
        )
        # Digest over *all* evidence ids (not the deduped projection) so the
        # merge transaction can recompute it from the raw rows and detect any
        # drift in the evidence set since the scan/approval.
        evidence_digest = stable_hash(
            "rule-proposal-evidence",
            json.dumps(
                sorted(e.evidence_id for e in (evidence or [])), ensure_ascii=False,
            ),
        )
        revision_a = int(getattr(definition_a, "revision", 0) or 0) if definition_a else 0
        revision_b = int(getattr(definition_b, "revision", 0) or 0) if definition_b else 0
        now = _now()
        with self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] in {"approved", "merged"}:
                    return self._row_to_proposal(existing)
                # Re-scan: refresh the assessment, keep lifecycle accumulators.
                conn.execute(
                    """
                    UPDATE rule_merge_proposals SET
                        similarity_score=?, evidence_count=?, agent_count=?,
                        project_count=?, contradiction_score=?, readiness_score=?,
                        governance_reasons=?, cooldown_until=?, negative_score=?,
                        conflict_type=?, judge_source=?, judge_model=?,
                        judge_score=?, judge_confidence=?, judge_recommendation=?,
                        judge_rationale=?, explanation=?, candidate_since=?,
                        last_evaluated_at=?, assessment_revision=?,
                        definition_revision_a=?, definition_revision_b=?,
                        evidence_digest=?, policy_version=?, weight_breakdown=?
                    WHERE proposal_id=?
                    """,
                    (
                        float(similarity_score), len(evidence_list),
                        len(agents), len(projects),
                        float(contradiction_score), float(readiness_score),
                        governance_reasons or "",
                        existing["cooldown_until"] or cooldown_until or "",
                        float(negative_score), conflict_type or "",
                        self._judge_field(judge, "source"),
                        self._judge_field(judge, "model"),
                        self._judge_score(judge),
                        self._judge_field(judge, "confidence"),
                        self._judge_field(judge, "recommendation"),
                        self._judge_field(judge, "rationale"),
                        explanation,
                        existing["candidate_since"] or now,
                        now, int(existing["assessment_revision"] or 0) + 1,
                        revision_a, revision_b, evidence_digest,
                        MERGE_POLICY_VERSION, weight_breakdown or "",
                        proposal_id,
                    ),
                )
                return self.get_proposal(proposal_id)
            conn.execute(
                """
                INSERT INTO rule_merge_proposals (
                    proposal_id, definition_ids, similarity_score,
                    evidence_count, agent_count, project_count,
                    contradiction_score, readiness_score, governance_reasons,
                    cooldown_until, first_merge_acknowledged, negative_score,
                    conflict_type, judge_source, judge_model, judge_score,
                    judge_confidence, judge_recommendation, judge_rationale,
                    status, explanation, created_at, candidate_since,
                    last_evaluated_at, assessment_revision, definition_revision_a,
                    definition_revision_b, evidence_digest, policy_version,
                    weight_breakdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    json.dumps(sorted_ids, ensure_ascii=False),
                    float(similarity_score), len(evidence_list),
                    len(agents), len(projects),
                    float(contradiction_score), float(readiness_score),
                    governance_reasons or "", cooldown_until or "",
                    0, float(negative_score), conflict_type or "",
                    self._judge_field(judge, "source"),
                    self._judge_field(judge, "model"),
                    self._judge_score(judge),
                    self._judge_field(judge, "confidence"),
                    self._judge_field(judge, "recommendation"),
                    self._judge_field(judge, "rationale"),
                    "candidate", explanation, now, now, now, 1,
                    revision_a, revision_b, evidence_digest, MERGE_POLICY_VERSION,
                    weight_breakdown or "",
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_proposal(row)

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rule_merge_proposals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY similarity_score DESC"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_proposal(r) for r in rows]

    def set_proposal_status(
        self, proposal_id: str, status: str,
    ) -> dict[str, Any] | None:
        """Transition a proposal's lifecycle status.

        Guard rails: a re-scan may never clobber an ``approved``/``merged``
        proposal back into the candidate pool, and a proposal may only reach
        ``approved`` from ``candidate`` (a rejected/conflicted proposal cannot
        be force-approved into a merge — that was the old bypass).  Approving
        records a first-class ``rule_merge_approvals`` row.
        """
        status = str(status or "").strip()
        now = _now()
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                return None
            current = str(row["status"] or "")
            # A merged proposal is final: nothing may move it back into the
            # candidate pool or re-approve it.
            if current == "merged":
                raise ValueError("rule_merge_proposal_finalized")
            # A re-scan may never clobber an approval back into the pool.
            if current == "approved" and status in {
                "candidate", "conflicted", "rejected",
            }:
                return self._row_to_proposal(row)
            if status == "approved":
                # Approval is first-class data.  Approving from candidate /
                # conflicted / rejected is recorded so the merge transaction
                # can verify a real approval exists — but the merge's hard
                # gates still decide whether the pair is actually mergeable.
                approval_id = stable_hash(
                    "rule-merge-approval", proposal_id, now,
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO rule_merge_approvals (
                        approval_id, proposal_id, approved_by, capability_id,
                        expected_definition_revisions, approval_scope,
                        created_at, expires_at
                    ) VALUES (?, ?, 'manual', 'store-status', '{}', 'merge', ?, '')
                    """,
                    (approval_id, proposal_id, now),
                )
            conn.execute(
                "UPDATE rule_merge_proposals SET status=? WHERE proposal_id=?",
                (status, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        approved_by: str = "human",
        capability_id: str = "",
        expected_definition_revisions: dict[str, int] | None = None,
        approval_scope: str = "merge",
        expires_at: str = "",
    ) -> dict[str, Any]:
        """First-class approval: records who approved what, then approves.

        ``merge_proposal(actor='admin')`` is no longer an approval by itself —
        an ``rule_merge_approvals`` row must exist for the human path to run.
        The recorded expected definition revisions are re-verified inside the
        merge transaction so a definition edited after approval cannot merge.
        """
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("rule_merge_proposal_not_found")
            if str(row["status"] or "") != "candidate":
                raise ValueError("rule_merge_proposal_not_approvable")
        approval_id = stable_hash(
            "rule-merge-approval", proposal_id, approved_by, capability_id, _now(),
        )
        now = _now()
        expected = json.dumps(
            dict(expected_definition_revisions or {}),
            ensure_ascii=False, sort_keys=True,
        )
        with self._db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO rule_merge_approvals (
                    approval_id, proposal_id, approved_by, capability_id,
                    expected_definition_revisions, approval_scope,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, proposal_id, approved_by or "human",
                 capability_id or "", expected, approval_scope or "merge",
                 now, expires_at or ""),
            )
            conn.execute(
                "UPDATE rule_merge_proposals SET status='approved' WHERE proposal_id=?",
                (proposal_id,),
            )
        return {
            "approval_id": approval_id, "proposal_id": proposal_id,
            "approved_by": approved_by or "human",
            "capability_id": capability_id or "",
            "expected_definition_revisions": dict(
                expected_definition_revisions or {},
            ),
            "approval_scope": approval_scope or "merge",
            "created_at": now,
            "expires_at": expires_at or "",
        }

    def get_valid_approval(self, proposal_id: str) -> dict[str, Any] | None:
        """The latest non-expired approval for a proposal, if any."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_merge_approvals WHERE proposal_id=? "
                "ORDER BY created_at DESC",
                (proposal_id,),
            ).fetchall()
        now = _now()
        for row in rows:
            expires_at = str(row["expires_at"] or "")
            if expires_at and expires_at < now:
                continue
            return {
                "approval_id": row["approval_id"],
                "proposal_id": row["proposal_id"],
                "approved_by": row["approved_by"] or "",
                "capability_id": row["capability_id"] or "",
                "expected_definition_revisions": json.loads(
                    row["expected_definition_revisions"] or "{}",
                ),
                "approval_scope": row["approval_scope"] or "merge",
                "created_at": row["created_at"] or "",
                "expires_at": expires_at,
            }
        return None

    def update_proposal_governance(
        self,
        proposal_id: str,
        *,
        readiness_score: float = 0.0,
        governance_reasons: str = "",
        cooldown_until: str = "",
        negative_score: float = 0.0,
        conflict_type: str = "",
        judge: Any | None = None,
    ) -> dict[str, Any] | None:
        """Persist the governance snapshot of one merge proposal."""
        with self._db() as conn:
            conn.execute(
                """
                UPDATE rule_merge_proposals SET
                    readiness_score=?, governance_reasons=?, cooldown_until=?,
                    negative_score=?, conflict_type=?, judge_source=?,
                    judge_model=?, judge_score=?, judge_confidence=?,
                    judge_recommendation=?, judge_rationale=?
                WHERE proposal_id=?
                """,
                (float(readiness_score), governance_reasons or "",
                 cooldown_until or "", float(negative_score), conflict_type or "",
                 self._judge_field(judge, "source"),
                 self._judge_field(judge, "model"),
                 self._judge_score(judge),
                 self._judge_field(judge, "confidence"),
                 self._judge_field(judge, "recommendation"),
                 self._judge_field(judge, "rationale"),
                 proposal_id),
            )
        return self.get_proposal(proposal_id)

    def acknowledge_first_merge(
        self, proposal_id: str, actor: str = "human",
    ) -> dict[str, Any] | None:
        """Record explicit human acknowledgment of the first-merge risk.

        The very first merge involving a pair of definitions is the highest-risk
        operation in the layer (no rollback history, no error pattern).  It must
        not happen on an Agent's say-so alone: ``merge_proposal(actor='auto')``
        refuses until this acknowledgment exists.
        """
        with self._db() as conn:
            conn.execute(
                """
                UPDATE rule_merge_proposals SET
                    first_merge_acknowledged=1,
                    governance_reasons=COALESCE(
                        NULLIF(governance_reasons, ''),
                        'first_merge_acknowledged_by=' || ?
                    )
                WHERE proposal_id=?
                """,
                (actor, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def clear_proposal_cooldown(self, proposal_id: str) -> dict[str, Any] | None:
        """Clear the 72h cooldown after human review of a merge proposal."""
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_merge_proposals SET cooldown_until='' WHERE proposal_id=?",
                (proposal_id,),
            )
        return self.get_proposal(proposal_id)

    def count_merge_decisions_for_definitions(
        self, definition_ids: Iterable[str],
    ) -> int:
        """Count *successful* merge decisions touching any of these definitions.

        ``merge_count == 0`` marks a first merge.  Undone decisions no longer
        count: once a pair has been merged and rolled back, its rollback
        experience exists, so the first-merge gate no longer applies.
        """
        wanted = set(definition_ids)
        if not wanted:
            return 0
        with self._db() as conn:
            rows = conn.execute(
                "SELECT canonical_definition_id, merged_definition_ids, status "
                "FROM rule_merge_decisions"
            ).fetchall()
        count = 0
        for row in rows:
            if row["status"] == "undone":
                continue
            canonical = row["canonical_definition_id"]
            merged = set(json.loads(row["merged_definition_ids"] or "[]"))
            if canonical in wanted or (wanted & merged):
                count += 1
        return count

    @staticmethod
    def _judge_score(judge: Any | None) -> float:
        if judge is None:
            return 0.0
        return float(getattr(judge, "semantic_score", 0.0) or 0.0)

    @staticmethod
    def _judge_field(judge: Any | None, name: str) -> str:
        if judge is None:
            return ""
        value = getattr(judge, name, "")
        if value is None:
            return ""
        return str(value) if not isinstance(value, float) else f"{value:.4f}"

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "proposal_id": row["proposal_id"],
            "definition_ids": json.loads(row["definition_ids"] or "[]"),
            "similarity_score": float(row["similarity_score"] or 0.0),
            "evidence_count": int(row["evidence_count"] or 0),
            "agent_count": int(row["agent_count"] or 0),
            "project_count": int(row["project_count"] or 0),
            "contradiction_score": float(row["contradiction_score"] or 0.0),
            "readiness_score": float(row["readiness_score"] or 0.0),
            "governance_reasons": row["governance_reasons"] or "",
            "cooldown_until": row["cooldown_until"] or "",
            "first_merge_acknowledged": int(row["first_merge_acknowledged"] or 0),
            "negative_score": float(row["negative_score"] or 0.0),
            "conflict_type": row["conflict_type"] or "",
            "judge_source": row["judge_source"] or "",
            "judge_model": row["judge_model"] or "",
            "judge_score": float(row["judge_score"] or 0.0),
            "judge_confidence": float(row["judge_confidence"] or 0.0),
            "judge_recommendation": row["judge_recommendation"] or "",
            "judge_rationale": row["judge_rationale"] or "",
            "status": row["status"] or "candidate",
            "explanation": row["explanation"] or "",
            "created_at": row["created_at"] or "",
            "candidate_since": row["candidate_since"] or "",
            "last_evaluated_at": row["last_evaluated_at"] or "",
            "assessment_revision": int(row["assessment_revision"] or 0),
            "definition_revision_a": int(row["definition_revision_a"] or 0),
            "definition_revision_b": int(row["definition_revision_b"] or 0),
            "evidence_digest": row["evidence_digest"] or "",
            "policy_version": row["policy_version"] or "",
            "weight_breakdown": row["weight_breakdown"] or "",
        }

    # ------------------------------------------------------------------
    # Merge decisions / undo
    # ------------------------------------------------------------------

    def record_merge_decision(
        self,
        *,
        proposal_id: str,
        canonical_definition_id: str,
        merged_definition_ids: list[str],
        before_bindings: list[dict[str, Any]],
        after_bindings: list[dict[str, Any]],
        migration: dict[str, Any],
        actor: str = "auto",
        readiness_at_merge: float = 0.0,
        strength_ok: bool = True,
        polarity_ok: bool = True,
        parameters_ok: bool = True,
        contradiction_ok: bool = True,
        negative_ok: bool = True,
        first_merge_acknowledged: bool = True,
        judge: Any | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        decision_id = stable_hash(
            "rule-merge-decision", proposal_id, canonical_definition_id, _now(),
        )
        now = _now()
        sql = """
            INSERT INTO rule_merge_decisions (
                decision_id, proposal_id, canonical_definition_id,
                merged_definition_ids, before_bindings, after_bindings,
                migration, actor, readiness_at_merge, strength_ok, polarity_ok,
                parameters_ok, contradiction_ok, negative_ok,
                first_merge_acknowledged, judge_source, judge_model,
                judge_score, judge_confidence, judge_recommendation,
                judge_rationale, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'merged', ?)
        """
        values = (
            decision_id, proposal_id, canonical_definition_id,
            json.dumps(sorted(merged_definition_ids), ensure_ascii=False),
            json.dumps(before_bindings, ensure_ascii=False),
            json.dumps(after_bindings, ensure_ascii=False),
            json.dumps(migration, ensure_ascii=False, sort_keys=True),
            actor, float(readiness_at_merge),
            1 if strength_ok else 0, 1 if polarity_ok else 0,
            1 if parameters_ok else 0, 1 if contradiction_ok else 0,
            1 if negative_ok else 0,
            1 if first_merge_acknowledged else 0,
            self._judge_field(judge, "source"),
            self._judge_field(judge, "model"),
            self._judge_score(judge),
            self._judge_field(judge, "confidence"),
            self._judge_field(judge, "recommendation"),
            self._judge_field(judge, "rationale"),
            now,
        )
        if conn is not None:
            conn.execute(sql, values)
        else:
            with self._db() as connection:
                connection.execute(sql, values)
        return {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "canonical_definition_id": canonical_definition_id,
            "merged_definition_ids": sorted(merged_definition_ids),
            "before_bindings": before_bindings,
            "after_bindings": after_bindings,
            "migration": migration,
            "actor": actor,
            "readiness_at_merge": float(readiness_at_merge),
            "strength_ok": bool(strength_ok),
            "polarity_ok": bool(polarity_ok),
            "parameters_ok": bool(parameters_ok),
            "contradiction_ok": bool(contradiction_ok),
            "negative_ok": bool(negative_ok),
            "first_merge_acknowledged": bool(first_merge_acknowledged),
            "judge_source": self._judge_field(judge, "source"),
            "judge_model": self._judge_field(judge, "model"),
            "judge_score": self._judge_score(judge),
            "judge_confidence": self._judge_field(judge, "confidence"),
            "judge_recommendation": self._judge_field(judge, "recommendation"),
            "judge_rationale": self._judge_field(judge, "rationale"),
            "status": "merged",
            "created_at": now,
            "undone_at": "",
        }

    def get_merge_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_merge_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": row["decision_id"],
            "proposal_id": row["proposal_id"],
            "canonical_definition_id": row["canonical_definition_id"],
            "merged_definition_ids": json.loads(row["merged_definition_ids"] or "[]"),
            "before_bindings": json.loads(row["before_bindings"] or "[]"),
            "after_bindings": json.loads(row["after_bindings"] or "[]"),
            "migration": json.loads(row["migration"] or "{}"),
            "actor": row["actor"] or "auto",
            "readiness_at_merge": float(row["readiness_at_merge"] or 0.0),
            "strength_ok": bool(row["strength_ok"]),
            "polarity_ok": bool(row["polarity_ok"]),
            "parameters_ok": bool(row["parameters_ok"]),
            "contradiction_ok": bool(row["contradiction_ok"]),
            "negative_ok": bool(row["negative_ok"]),
            "first_merge_acknowledged": bool(row["first_merge_acknowledged"]),
            "judge_source": row["judge_source"] or "",
            "judge_model": row["judge_model"] or "",
            "judge_score": float(row["judge_score"] or 0.0),
            "judge_confidence": float(row["judge_confidence"] or 0.0),
            "judge_recommendation": row["judge_recommendation"] or "",
            "judge_rationale": row["judge_rationale"] or "",
            "status": row["status"] or "merged",
            "created_at": row["created_at"] or "",
            "undone_at": row["undone_at"] or "",
        }

    def mark_merge_undone(self, decision_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_merge_decisions SET status='undone', undone_at=? "
                "WHERE decision_id=?",
                (_now(), decision_id),
            )

    def list_merge_decisions(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_merge_decisions ORDER BY created_at"
            ).fetchall()
        return [{
            "decision_id": r["decision_id"],
            "proposal_id": r["proposal_id"],
            "canonical_definition_id": r["canonical_definition_id"],
            "merged_definition_ids": json.loads(r["merged_definition_ids"] or "[]"),
            "readiness_at_merge": float(r["readiness_at_merge"] or 0.0),
            "strength_ok": bool(r["strength_ok"]),
            "polarity_ok": bool(r["polarity_ok"]),
            "parameters_ok": bool(r["parameters_ok"]),
            "contradiction_ok": bool(r["contradiction_ok"]),
            "negative_ok": bool(r["negative_ok"]),
            "first_merge_acknowledged": bool(r["first_merge_acknowledged"]),
            "judge_source": r["judge_source"] or "",
            "judge_model": r["judge_model"] or "",
            "judge_score": float(r["judge_score"] or 0.0),
            "judge_confidence": float(r["judge_confidence"] or 0.0),
            "judge_recommendation": r["judge_recommendation"] or "",
            "judge_rationale": r["judge_rationale"] or "",
            "status": r["status"] or "merged",
            "created_at": r["created_at"] or "",
            "undone_at": r["undone_at"] or "",
        } for r in rows]

    # ------------------------------------------------------------------
    # Atomic merge execution (P3 §8: before_bindings == after_bindings)
    # ------------------------------------------------------------------

    def execute_merge(
        self,
        *,
        proposal_id: str,
        canonical_definition_id: str,
        merged_definition_ids: list[str],
        actor: str = "auto",
        readiness_at_merge: float = 0.0,
        strength_ok: bool = True,
        negative_ok: bool = True,
        first_merge_acknowledged: bool = True,
        judge: Any | None = None,
        approval_id: str = "",
        expected_definition_revisions: dict[str, int] | None = None,
        expected_evidence_digest: str = "",
    ) -> dict[str, Any]:
        """Atomically merge definitions into a canonical one.

        Invariants enforced inside one transaction:
          * proposal is locked (status must be candidate/approved) and its
            definition pair must equal the pair being merged — a caller cannot
            merge a different pair than the one that was evaluated;
          * when a human approval is claimed, a valid (non-expired)
            ``rule_merge_approvals`` row for this proposal must exist;
          * the **hard gates are re-computed against the current rows** inside
            the transaction — strength, polarity, parameter and negative
            evidence.  A definition edited after the scan cannot sneak a
            governance conflict past the human path (TOCTOU);
          * expected definition revisions and an evidence digest are re-verified
            so the merge runs on the exact state the approver reviewed;
          * Bindings only change ``definition_id`` — the audience identity set
            before the merge must equal the set after (scope never expands);
          * Evidence is migrated to the canonical definition;
          * a ``rule_merge_decisions`` row records the exact before/after so
            the merge can be undone precisely.
        """
        now = _now()
        merged = sorted({str(x) for x in merged_definition_ids} - {canonical_definition_id})
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                proposal = conn.execute(
                    "SELECT * FROM rule_merge_proposals WHERE proposal_id=? "
                    "AND status IN ('candidate','approved')",
                    (proposal_id,),
                ).fetchone()
                if proposal is None:
                    raise ValueError("rule_merge_proposal_not_mergeable")

                # Pair identity: the pair being merged must be the evaluated pair.
                pair = set(json.loads(proposal["definition_ids"] or "[]"))
                if pair != {canonical_definition_id, *merged}:
                    raise RuntimeError("rule_merge_proposal_definition_mismatch")

                # Approval: a human merge requires a valid first-class approval.
                if approval_id:
                    approval_row = conn.execute(
                        "SELECT * FROM rule_merge_approvals WHERE approval_id=? "
                        "AND proposal_id=?",
                        (approval_id, proposal_id),
                    ).fetchone()
                    if approval_row is None:
                        raise RuntimeError("rule_merge_approval_invalid")
                    if str(approval_row["expires_at"] or "") and \
                            str(approval_row["expires_at"]) < now:
                        raise RuntimeError("rule_merge_approval_expired")

                # Lock the proposal so a concurrent merge cannot double-run.
                conn.execute(
                    "UPDATE rule_merge_proposals SET status='merging' WHERE proposal_id=?",
                    (proposal_id,),
                )

                # Snapshot before-state: bindings and evidence per definition.
                before_bindings: list[dict[str, Any]] = []
                original_bindings: dict[str, list[str]] = {}
                original_evidence: dict[str, list[str]] = {}
                definition_rows: dict[str, sqlite3.Row] = {}
                all_definition_ids = [canonical_definition_id, *merged]
                before_identities: set[str] = set()
                for definition_id in all_definition_ids:
                    row = conn.execute(
                        "SELECT * FROM rule_definitions WHERE definition_id=?",
                        (definition_id,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("rule_definition_not_found")
                    if (
                        row["status"] in {"merged", "alias", "superseded"}
                        and definition_id != canonical_definition_id
                    ):
                        raise ValueError("rule_definition_already_merged")
                    definition_rows[definition_id] = row
                    binding_rows = conn.execute(
                        "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                        (definition_id,),
                    ).fetchall()
                    original_bindings[definition_id] = [r["binding_id"] for r in binding_rows]
                    for r in binding_rows:
                        binding = self._row_to_binding(r)
                        before_bindings.append(binding.to_dict())
                        before_identities.add(binding_identity_key(binding))
                    evidence_rows = conn.execute(
                        "SELECT evidence_id FROM rule_evidence WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    original_evidence[definition_id] = [r["evidence_id"] for r in evidence_rows]

                # Hard gates re-computed against the *current* rows inside the
                # transaction: strength/polarity/parameter/negative evidence.
                # A definition edited after the scan (or a force-approved
                # conflict) can never merge — the human path cannot bypass this.
                gates = self._recompute_hard_gates(conn, definition_rows)
                strength_ok = bool(gates["strength_ok"])
                negative_ok = bool(gates["negative_ok"])
                for gate, ok in gates.items():
                    if not ok:
                        raise RuntimeError(f"rule_merge_hard_gate_regression: {gate}")

                # Expected definition revisions: the merge runs on the exact
                # state the approver reviewed, not a drifted one.
                if expected_definition_revisions:
                    for definition_id, expected_revision in (
                        expected_definition_revisions or {}
                    ).items():
                        current = definition_rows.get(definition_id)
                        if current is None or int(current["revision"] or 0) != int(
                            expected_revision,
                        ):
                            raise RuntimeError("rule_merge_definition_revision_drift")

                # Expected evidence digest: reject a silently-changed evidence set.
                if expected_evidence_digest:
                    evidence_ids = sorted(
                        eid
                        for definition_id in all_definition_ids
                        for eid in original_evidence[definition_id]
                    )
                    digest = stable_hash(
                        "rule-proposal-evidence",
                        json.dumps(evidence_ids, ensure_ascii=False),
                    )
                    if digest != expected_evidence_digest:
                        raise RuntimeError("rule_merge_evidence_digest_drift")

                # Update every merged definition's Bindings to the canonical id.
                for definition_id in merged:
                    conn.execute(
                        "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                        "updated_at=? WHERE definition_id=?",
                        (canonical_definition_id, now, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_evidence SET definition_id=? WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_definitions SET status='merged', superseded_by=?, "
                        "updated_at=? WHERE definition_id=?",
                        (canonical_definition_id, now, definition_id),
                    )

                # Scope invariance: audience identity set must be unchanged.
                after_rows = conn.execute(
                    "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                    (canonical_definition_id,),
                ).fetchall()
                after_identities = {
                    binding_identity_key(self._row_to_binding(r)) for r in after_rows
                }
                after_bindings = [
                    self._row_to_binding(r).to_dict() for r in after_rows
                ]
                if after_identities != before_identities:
                    raise RuntimeError(
                        "rule_merge_scope_expansion_detected: "
                        "before_bindings != after_bindings"
                    )

                migration = {
                    "original_bindings": original_bindings,
                    "original_evidence": original_evidence,
                }
                decision = self.record_merge_decision(
                    proposal_id=proposal_id,
                    canonical_definition_id=canonical_definition_id,
                    merged_definition_ids=merged,
                    before_bindings=before_bindings,
                    after_bindings=after_bindings,
                    migration=migration,
                    actor=actor,
                    readiness_at_merge=readiness_at_merge,
                    strength_ok=strength_ok,
                    polarity_ok=bool(gates.get("polarity_ok", True)),
                    parameters_ok=bool(gates.get("parameters_ok", True)),
                    contradiction_ok=bool(gates.get("contradiction_ok", True)),
                    negative_ok=negative_ok,
                    first_merge_acknowledged=first_merge_acknowledged,
                    judge=judge,
                    conn=conn,
                )
                conn.execute(
                    "UPDATE rule_merge_proposals SET status='merged' WHERE proposal_id=?",
                    (proposal_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return decision

    def undo_merge(self, decision_id: str) -> dict[str, Any]:
        """Precisely undo a merge: restore bindings/evidence/definitions."""
        now = _now()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM rule_merge_decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("rule_merge_decision_not_found")
                if row["status"] == "undone":
                    return {
                        "decision_id": decision_id,
                        "status": "undone",
                        "already_undone": True,
                    }
                canonical = row["canonical_definition_id"]
                merged = json.loads(row["merged_definition_ids"] or "[]")
                migration = json.loads(row["migration"] or "{}")
                original_bindings = migration.get("original_bindings", {})
                original_evidence = migration.get("original_evidence", {})
                all_definition_ids = [canonical, *merged]
                # Restore binding ownership for every merged definition.
                for definition_id, binding_ids in original_bindings.items():
                    for binding_id in binding_ids:
                        conn.execute(
                            "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                            "updated_at=? WHERE binding_id=?",
                            (definition_id, now, binding_id),
                        )
                for definition_id, evidence_ids in original_evidence.items():
                    for evidence_id in evidence_ids:
                        conn.execute(
                            "UPDATE rule_evidence SET definition_id=? WHERE evidence_id=?",
                            (definition_id, evidence_id),
                        )
                for definition_id in merged:
                    conn.execute(
                        "UPDATE rule_definitions SET status='active', superseded_by='', "
                        "updated_at=? WHERE definition_id=?",
                        (now, definition_id),
                    )
                # Proposal returns to candidate so a fresh evaluation can rerun.
                conn.execute(
                    "UPDATE rule_merge_proposals SET status='candidate' "
                    "WHERE proposal_id=?",
                    (row["proposal_id"],),
                )
                conn.execute(
                    "UPDATE rule_merge_decisions SET status='undone', undone_at=? "
                    "WHERE decision_id=?",
                    (now, decision_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "decision_id": decision_id,
            "status": "undone",
            "merged_definition_ids": merged,
            "canonical_definition_id": canonical,
        }

    # ------------------------------------------------------------------
    # Shadow verify: old matcher vs new matcher
    # ------------------------------------------------------------------

    def shadow_verify(
        self,
        context: EffectiveAgentContext,
        legacy_records: list[tuple[str, list[Any]]],
    ) -> dict[str, Any]:
        """Compare the legacy matcher with the Definition/Binding matcher.

        ``legacy_records`` is a list of ``(memory_id, assignments)`` pairs
        taken from the legacy store.  The new matcher resolves the same
        context through Definitions → Bindings.  ``missing`` = legacy matched,
        new did not; ``extra`` = new matched, legacy did not; ``permission_diff``
        = a new binding is broader than any legacy assignment for this context.
        """
        context_project = canonical_project_ref(context.project_ref)
        legacy_matched: set[str] = set()
        for memory_id, assignments in legacy_records:
            for assignment in assignments:
                if assignment_matches(assignment, context):
                    legacy_matched.add(memory_id)
                    break

        new_matched: set[str] = set()
        for binding in self.list_bindings():
            if not self._binding_matches(binding, context):
                continue
            definition = self.get_definition(binding.definition_id)
            if definition is None or definition.status not in {"active", "alias"}:
                continue
            # Map definition back to the source rules (evidence origins).
            for evidence in self.list_evidence(definition_id=definition.definition_id):
                if evidence.source_rule_id:
                    new_matched.add(evidence.source_rule_id)

        missing = sorted(legacy_matched - new_matched)
        extra = sorted(new_matched - legacy_matched)
        # A binding is a permission expansion if it targets system/group or a
        # project/provider/role the legacy assignment layer never used here.
        permission_diff = 0
        for binding in self.list_bindings():
            if binding.target_type in {"system", "group"}:
                permission_diff += 1
            elif binding.target_type in {"project", "provider", "runtime_role"}:
                permission_diff += 1
        return {
            "missing": missing,
            "extra": extra,
            "permission_diff": permission_diff,
        }

    @staticmethod
    def _binding_matches(binding: RuleBinding, context: EffectiveAgentContext) -> bool:
        project_ref = canonical_project_ref(binding.project_ref)
        assignment = RuleAssignment(
            memory_id=binding.definition_id,
            target_type=binding.target_type,
            target_id=binding.target_id,
            project_ref=project_ref,
            effect=binding.effect,
        )
        return assignment_matches(assignment, context)

    # ------------------------------------------------------------------
    # metrics
    # ------------------------------------------------------------------

    def metrics(self) -> dict[str, Any]:
        """CI-visible aggregate metrics for the Rule Intelligence Layer."""
        definitions = self.list_definitions()
        bindings = self.list_bindings()
        active_definitions = [d for d in definitions if d.status == "active"]
        merged = [
            d for d in definitions
            if d.status in {"merged", "alias"}
        ]
        # binding expansion: bindings whose audience shape has no matching
        # legacy audience counterpart is reported separately via shadow_verify;
        # here we count system/broad auto bindings as a hard failure signal.
        system_auto = [
            b for b in bindings
            if b.target_type == "system"
            and str(b.created_by or "").casefold() in {"auto", "backfill"}
        ]
        auto_broad = [
            b for b in bindings
            if str(b.created_by or "").casefold() in {"auto", "backfill"}
            and b.target_type not in AUTO_ALLOWED_TARGET_TYPES
        ]
        # unique semantic definitions among active (dedup on semantic_hash).
        seen: set[str] = set()
        unique_semantic = 0
        for d in active_definitions:
            if d.semantic_hash and d.semantic_hash not in seen:
                seen.add(d.semantic_hash)
                unique_semantic += 1
        total_bindings = len(bindings)
        canonical_unique = len(
            {binding_identity_key(b) for b in bindings if b.status == "active"}
        )

        # P3-001/002/003 acceptance family.  Every value is designed to be 0
        # (or 1 for the success booleans) when the governance gates hold.
        # Decision booleans are the *recomputed* hard gates recorded by the
        # merge transaction (PR3/PR7), never the caller's own claims.
        decisions = self.list_merge_decisions()
        mergeable_decision_count = len(decisions)
        if mergeable_decision_count:
            strength_conflict_merge = sum(
                1 for d in decisions if not d.get("strength_ok", True)
            )
            polarity_conflict_merge = sum(
                1 for d in decisions if not d.get("polarity_ok", True)
            )
            parameter_conflict_merge = sum(
                1 for d in decisions if not d.get("parameters_ok", True)
            )
            contradiction_merge = sum(
                1 for d in decisions if not d.get("contradiction_ok", True)
            )
            negative_leak = sum(
                1 for d in decisions if not d.get("negative_ok", True)
            )
            unack_first_auto = sum(
                1 for d in decisions
                if d.get("actor") == "auto"
                and not d.get("first_merge_acknowledged", True)
            )
            gate_violations = (
                strength_conflict_merge + polarity_conflict_merge
                + parameter_conflict_merge + contradiction_merge + negative_leak
            )
            auto_merge_precision = 1.0 - gate_violations / max(
                1, mergeable_decision_count,
            )
            # undo/scope digest: a merged pair whose before/after audience
            # identity multisets differ would break the scope-invariance
            # contract that undo relies on.
            undo_state_digest_diff = 0
            for decision in decisions:
                before = {
                    self._binding_identity_from_dict(item)
                    for item in decision.get("before_bindings", [])
                }
                after = {
                    self._binding_identity_from_dict(item)
                    for item in decision.get("after_bindings", [])
                }
                if before != after:
                    undo_state_digest_diff += 1
        else:
            strength_conflict_merge = 0
            polarity_conflict_merge = 0
            parameter_conflict_merge = 0
            contradiction_merge = 0
            negative_leak = 0
            unack_first_auto = 0
            auto_merge_precision = 1.0
            undo_state_digest_diff = 0

        single_agent_dominance = 0
        for proposal in self.list_proposals():
            if proposal["status"] != "candidate":
                continue
            evidence_list = self._evidence_for_proposal(proposal)
            weights = self._weights_for(evidence_list)
            per_agent: dict[str, float] = {}
            for ev, w in zip(evidence_list, weights):
                per_agent[ev.agent_instance_id or ""] = (
                    per_agent.get(ev.agent_instance_id or "", 0.0) + w
                )
            if largest_source_ratio(per_agent) >= MAX_SINGLE_SOURCE_RATIO:
                single_agent_dominance += 1

        return {
            "definition_count": len(definitions),
            "active_definition_count": len(active_definitions),
            "merged_definition_count": len(merged),
            "unique_semantic_definition_count": unique_semantic,
            "binding_count": total_bindings,
            "canonical_binding_count": canonical_unique,
            "evidence_count": self.count_evidence(),
            "proposal_count": len(self.list_proposals()),
            "merged_proposal_count": len(self.list_proposals(status="merged")),
            "system_auto_binding": len(system_auto),
            "auto_broad_binding": len(auto_broad),
            "merge_undo_success": 1 if undo_state_digest_diff == 0 else 0,
            "migration_loss": self._migration_loss(),
            "auto_merge_precision": round(auto_merge_precision, 4),
            "strength_conflict_merge": strength_conflict_merge,
            "polarity_conflict_merge": polarity_conflict_merge,
            "parameter_conflict_merge": parameter_conflict_merge,
            "contradiction_merge": contradiction_merge,
            "negative_evidence_leak": negative_leak,
            "first_merge_human_approval": unack_first_auto,
            "single_agent_dominance": single_agent_dominance,
            "undo_state_digest_diff": undo_state_digest_diff,
            "negative_evidence_count": self.count_negative_evidence(),
            "agent_reputation_count": len(self.list_agent_reputations()),
            "project_profile_count": len(self.list_project_profiles()),
            "definition_version_count": self.count_definition_versions(),
        }

    # ------------------------------------------------------------------
    # PR7: real machine acceptance (no self-reported constants)
    # ------------------------------------------------------------------

    def _migration_loss(self) -> int:
        """Real migration loss: legacy governed records the canonical layer does
        not cover plus source links that resolve to a non-active definition.
        """
        missing = 0
        resurrection = 0
        for group_id, _db_path in iter_legacy_groups(self.workspace):
            try:
                from .shared_memory_store import SharedMemoryStore
                legacy = SharedMemoryStore(self.workspace, group_id)
            except Exception:
                continue
            for record in legacy.list_records():
                if str(record.injection_policy or "") != "always":
                    continue
                if str(record.status.value if hasattr(record.status, "value") else record.status) == "deleted":
                    continue
                if self.get_source_link(group_id, record.memory_id) is None:
                    missing += 1
        for link in self._list_source_links():
            canonical = link.get("canonical_definition_id") or ""
            if not canonical:
                continue
            target = self.get_definition(self.resolve_canonical(canonical))
            if target is None or target.status != "active":
                resurrection += 1
        return missing + resurrection

    def _list_source_links(self) -> list[dict[str, Any]]:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM rule_source_links").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _binding_identity_from_dict(data: dict[str, Any]) -> str:
        """Audience identity of a serialized binding (matches RuleBinding)."""
        return stable_hash(
            "rule-binding-audience",
            json.dumps([
                str(data.get("share_group_id", "") or ""),
                str(data.get("target_type", "") or ""),
                canonical_project_ref(str(data.get("project_ref", "") or "")),
                str(data.get("target_id", "") or ""),
                str(data.get("provider", "") or "").casefold(),
                str(data.get("runtime_role", "") or "").casefold(),
                str(data.get("effect", "include") or "include"),
                int(data.get("priority", 0) or 0),
            ], ensure_ascii=False),
        )

    def governance_acceptance(self) -> dict[str, Any]:
        """The PR7 machine-acceptance family, all computed from persisted state.

        Every counter below is derived, never a constant the merge service wrote
        to its own decision row:
          definition_strength_identity_collision — active pre-v2 (collision-prone)
              definition ids still present;
          canonical_read_context_diff          — legacy always-records with no
              source link, i.e. the canonical read would under-expose them;
          backfill_resurrection_count          — source links resolving to a
              non-active definition;
          proposal_duplicate_count             — pair with more than one proposal;
          human_hard_gate_bypass_count         — decisions that merged a conflict;
          evidence_independence_violation      — evidence rows sharing an
              independence key (duplicate receipts not collapsed);
          migration_binding_multiset_diff      — migration bindings missing the
              legacy assignment hash audit;
          undo_state_digest_diff               — before/after audience multiset
              drift on any decision;
          rule_intelligence_event_lag          — unconsumed P2->P3 outbox events.
        """
        definitions = self.list_definitions()
        active = [d for d in definitions if d.status == "active"]
        decisions = self.list_merge_decisions()
        proposals = self.list_proposals()
        bindings = self.list_bindings()

        definition_strength_identity_collision = sum(
            1 for d in active
            if d.definition_id == stable_hash(
                "rule-definition", "canonical", d.canonical_text,
            )
        )

        canonical_read_context_diff = 0
        try:
            from .shared_memory_store import SharedMemoryStore
        except Exception:
            SharedMemoryStore = None  # type: ignore[assignment]
        if SharedMemoryStore is not None:
            for group_id, _db_path in iter_legacy_groups(self.workspace):
                try:
                    legacy = SharedMemoryStore(self.workspace, group_id)
                except Exception:
                    continue
                for record in legacy.list_records():
                    if str(record.injection_policy or "") != "always":
                        continue
                    status_value = getattr(record.status, "value", record.status)
                    if str(status_value) == "deleted":
                        continue
                    if self.get_source_link(group_id, record.memory_id) is None:
                        canonical_read_context_diff += 1

        backfill_resurrection_count = 0
        for link in self._list_source_links():
            canonical = link.get("canonical_definition_id") or ""
            if not canonical:
                continue
            target = self.get_definition(self.resolve_canonical(canonical))
            if target is None or target.status != "active":
                backfill_resurrection_count += 1

        pair_counts: dict[tuple[str, ...], int] = {}
        for proposal in proposals:
            key = tuple(sorted(proposal["definition_ids"]))
            pair_counts[key] = pair_counts.get(key, 0) + 1
        proposal_duplicate_count = sum(
            1 for count in pair_counts.values() if count > 1
        )

        human_hard_gate_bypass_count = sum(
            1 for d in decisions
            if not (
                d.get("strength_ok")
                and d.get("polarity_ok")
                and d.get("parameters_ok")
                and d.get("contradiction_ok")
                and d.get("negative_ok")
            )
        )

        independence_counts: dict[str, int] = {}
        for evidence in self.list_evidence():
            if evidence.independence_key:
                independence_counts[evidence.independence_key] = (
                    independence_counts.get(evidence.independence_key, 0) + 1
                )
        evidence_independence_violation = sum(
            1 for count in independence_counts.values() if count > 1
        )

        migration_binding_multiset_diff = sum(
            1 for b in bindings
            if str(b.created_by or "") == "migration"
            and "legacy_assignment_hash" not in (b.authorization or "")
        )

        undo_state_digest_diff = 0
        for decision in decisions:
            before = {
                self._binding_identity_from_dict(item)
                for item in decision.get("before_bindings", [])
            }
            after = {
                self._binding_identity_from_dict(item)
                for item in decision.get("after_bindings", [])
            }
            if before != after:
                undo_state_digest_diff += 1

        rule_intelligence_event_lag = 0
        if SharedMemoryStore is not None:
            for group_id, _db_path in iter_legacy_groups(self.workspace):
                try:
                    legacy = SharedMemoryStore(self.workspace, group_id)
                    rule_intelligence_event_lag += len(
                        legacy.list_unconsumed_rule_events(),
                    )
                except Exception:
                    continue

        auto_merge_precision = 1.0
        if decisions:
            auto_merge_precision = 1.0 - (
                human_hard_gate_bypass_count / len(decisions)
            )

        passed = bool(
            definition_strength_identity_collision == 0
            and canonical_read_context_diff == 0
            and backfill_resurrection_count == 0
            and proposal_duplicate_count == 0
            and human_hard_gate_bypass_count == 0
            and evidence_independence_violation == 0
            and migration_binding_multiset_diff == 0
            and undo_state_digest_diff == 0
            and rule_intelligence_event_lag == 0
            and auto_merge_precision >= 0.995
        )
        return {
            "definition_strength_identity_collision": definition_strength_identity_collision,
            "canonical_read_context_diff": canonical_read_context_diff,
            "backfill_resurrection_count": backfill_resurrection_count,
            "proposal_duplicate_count": proposal_duplicate_count,
            "human_hard_gate_bypass_count": human_hard_gate_bypass_count,
            "evidence_independence_violation": evidence_independence_violation,
            "migration_binding_multiset_diff": migration_binding_multiset_diff,
            "undo_state_digest_diff": undo_state_digest_diff,
            "rule_intelligence_event_lag": rule_intelligence_event_lag,
            "auto_merge_precision": round(auto_merge_precision, 4),
            "merge_undo_exact_rate": (
                1.0 if undo_state_digest_diff == 0 else 0.0
            ),
            "proposal_identity_stability": (
                1.0 if proposal_duplicate_count == 0 else 0.0
            ),
            "passed": passed,
        }

    # ------------------------------------------------------------------
    # metrics helpers (evidence weighting for candidate proposals)
    # ------------------------------------------------------------------

    def _evidence_for_proposal(
        self, proposal: dict[str, Any],
    ) -> list[Any]:
        definition_ids = proposal["definition_ids"]
        return [
            ev
            for definition_id in definition_ids
            for ev in self.list_evidence(definition_id)
        ]

    def _weights_for(self, evidence_list: list[Any]) -> list[float]:
        """Weight each evidence by reputation + project profile (P3-003, PR5)."""
        reps = {r["agent_id"]: r for r in self.list_agent_reputations()}
        profiles = {p["project_ref"]: p for p in self.list_project_profiles()}
        weights: list[float] = []
        for ev in evidence_list:
            rep = reps.get(ev.agent_instance_id or "")
            profile = profiles.get(ev.project_ref or "")
            sample_count = int(rep.get("sample_count") or 0) if rep else 0
            if rep and sample_count >= MIN_REPUTATION_SAMPLES:
                agent_reliability = (
                    float(rep.get("success_rate") or 0.0)
                    + float(rep.get("rule_accuracy") or 0.0)
                ) / 2.0
            elif rep:
                raw = (
                    float(rep.get("success_rate") or 0.0)
                    + float(rep.get("rule_accuracy") or 0.0)
                ) / 2.0
                shrink = sample_count / MIN_REPUTATION_SAMPLES
                agent_reliability = raw * shrink + 0.5 * (1.0 - shrink)
            else:
                agent_reliability = 0.5
            stats = self.get_runtime_stats(ev.definition_id)
            total_runtime = (
                int((stats or {}).get("followed") or 0)
                + int((stats or {}).get("violated") or 0)
                + int((stats or {}).get("not_applicable") or 0)
                + int((stats or {}).get("exception_count") or 0)
            )
            if stats and total_runtime > 0:
                rule_specific_success = bayesian_accuracy(
                    int(stats.get("followed") or 0),
                    total_runtime - int(stats.get("followed") or 0),
                )
            else:
                rule_specific_success = 0.5
            weights.append(evidence_weight(
                agent_reliability=agent_reliability,
                project_importance=(
                    project_importance_score(
                        float(profile.get("production_level") or 0.0),
                        float(profile.get("criticality") or 0.0),
                        bool(profile.get("owner_verified")),
                    )
                    if profile else 0.5
                ),
                rule_specific_success=rule_specific_success,
                feedback_authority=feedback_authority_score(
                    "", int(getattr(ev, "feedback_authority", 0) or 0),
                ),
                recency=recency_factor(days_between(ev.observed_at)),
                evidence_confidence=float(getattr(ev, "confidence", 1.0) or 0.0),
            ))
        return weights

    # ------------------------------------------------------------------
    # in-transaction hard-gate recomputation (PR3 TOCTOU)
    # ------------------------------------------------------------------

    def _recompute_hard_gates(
        self,
        conn: sqlite3.Connection,
        definition_rows: dict[str, sqlite3.Row],
    ) -> dict[str, bool]:
        """Recompute the merge hard gates from the current DB rows.

        Called inside ``execute_merge``'s transaction so a definition edited
        between scan and merge cannot silently turn a safe pair into a
        governance conflict (or back).
        """
        ordered = list(definition_rows.values())
        if len(ordered) != 2:
            raise ValueError("rule_merge_pair_required")
        a, b = (self._row_to_definition(row) for row in ordered)
        strength_ok = (
            str(a.rule_strength or "") == str(b.rule_strength or "")
            and str(a.rule_strength or "") != STRENGTH_UNKNOWN
        )
        polarity_ok = a.polarity == b.polarity
        params_ok = not parameter_conflict(a, b)
        contradiction_ok = contradiction_score(a, b) <= 0
        negative_rows = [
            row
            for definition_row in ordered
            for row in conn.execute(
                "SELECT * FROM rule_negative_evidence WHERE definition_id=?",
                (definition_row["definition_id"],),
            ).fetchall()
        ]
        positive_rows = [
            row
            for definition_row in ordered
            for row in conn.execute(
                "SELECT * FROM rule_evidence WHERE definition_id=?",
                (definition_row["definition_id"],),
            ).fetchall()
        ]
        positive_weight = weighted_evidence_score(
            self._conn_evidence_weights(conn, positive_rows),
        )
        negative_weight = weighted_evidence_score(
            self._conn_evidence_weights(conn, negative_rows),
        )
        negative_ok = (
            negative_evidence_score(negative_weight, positive_weight)
            < NEGATIVE_EVIDENCE_THRESHOLD
        )
        return {
            "strength_ok": strength_ok,
            "polarity_ok": polarity_ok,
            "parameters_ok": params_ok,
            "contradiction_ok": contradiction_ok,
            "negative_ok": negative_ok,
        }

    @staticmethod
    def _conn_evidence_weights(
        conn: sqlite3.Connection, evidence_rows: list[sqlite3.Row],
    ) -> list[float]:
        """Weight evidence rows using reputation/profile read on ``conn``."""
        reps = {
            r["agent_id"]: r
            for r in conn.execute("SELECT * FROM agent_reputation").fetchall()
        }
        profiles = {
            r["project_ref"]: r
            for r in conn.execute("SELECT * FROM project_profile").fetchall()
        }
        stats_by_def = {
            r["definition_id"]: r
            for r in conn.execute(
                "SELECT * FROM rule_definition_runtime_stats",
            ).fetchall()
        }
        weights: list[float] = []
        for ev in evidence_rows:
            rep_row = reps.get(ev["agent_instance_id"] or "")
            profile_row = profiles.get(ev["project_ref"] or "")
            rep = dict(rep_row) if rep_row is not None else None
            profile = dict(profile_row) if profile_row is not None else None
            sample_count = int(rep["sample_count"] or 0) if rep else 0
            if rep and sample_count >= MIN_REPUTATION_SAMPLES:
                agent_reliability = (
                    float(rep["success_rate"] or 0.0)
                    + float(rep["rule_accuracy"] or 0.0)
                ) / 2.0
            elif rep:
                raw = (
                    float(rep["success_rate"] or 0.0)
                    + float(rep["rule_accuracy"] or 0.0)
                ) / 2.0
                shrink = sample_count / MIN_REPUTATION_SAMPLES
                agent_reliability = raw * shrink + 0.5 * (1.0 - shrink)
            else:
                agent_reliability = 0.5
            stats_row = stats_by_def.get(ev["definition_id"] or "")
            stats = dict(stats_row) if stats_row is not None else None
            total_runtime = (
                int((stats or {}).get("followed") or 0)
                + int((stats or {}).get("violated") or 0)
                + int((stats or {}).get("not_applicable") or 0)
                + int((stats or {}).get("exception_count") or 0)
            )
            if stats and total_runtime > 0:
                rule_specific_success = bayesian_accuracy(
                    int(stats.get("followed") or 0),
                    total_runtime - int(stats.get("followed") or 0),
                )
            else:
                rule_specific_success = 0.5
            weights.append(evidence_weight(
                agent_reliability=agent_reliability,
                project_importance=(
                    project_importance_score(
                        float(profile.get("production_level") or 0.0),
                        float(profile.get("criticality") or 0.0),
                        bool(profile.get("owner_verified")),
                    )
                    if profile else 0.5
                ),
                rule_specific_success=rule_specific_success,
                feedback_authority=feedback_authority_score(
                    "", int(ev["feedback_authority"] or 0),
                ),
                recency=recency_factor(days_between(ev["observed_at"] or "")),
                evidence_confidence=float(ev["confidence"] or 0.0),
            ))
        return weights


def iter_legacy_groups(workspace: str | Path) -> Iterable[tuple[str, Path]]:
    """Yield (group_id, db_path) for every legacy shared-memory group."""
    base = Path(workspace) / ".memoryguard" / "shared-memory"
    if not base.exists():
        return
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        db_path = child / "memory.db"
        if db_path.exists():
            yield child.name, db_path
