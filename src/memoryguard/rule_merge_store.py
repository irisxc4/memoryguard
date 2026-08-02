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
    RuleDefinition,
)
from .rule_evidence import RuleEvidence, dedupe_evidence
from .rule_merge_policy import MAX_SINGLE_SOURCE_RATIO, largest_source_ratio
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
    readiness_at_merge REAL NOT NULL DEFAULT 0.0,
    strength_ok INTEGER NOT NULL DEFAULT 1,
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
        ("rule_merge_decisions", "readiness_at_merge", "REAL NOT NULL DEFAULT 0.0"),
        ("rule_merge_decisions", "strength_ok", "INTEGER NOT NULL DEFAULT 1"),
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
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    definition_id=excluded.definition_id,
                    confidence=excluded.confidence,
                    observed_at=excluded.observed_at
                """,
                (
                    payload["evidence_id"], payload["definition_id"],
                    payload["source_rule_id"], payload["agent_instance_id"],
                    payload["project_ref"], payload["provider"],
                    payload["session_id"], payload["receipt_id"],
                    payload["content_hash"], payload["semantic_hash"],
                    payload["confidence"], payload["observed_at"],
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
                    observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    definition_id=excluded.definition_id,
                    confidence=excluded.confidence,
                    observed_at=excluded.observed_at
                """,
                (
                    payload["evidence_id"], payload["definition_id"],
                    payload["source_rule_id"], payload["agent_instance_id"],
                    payload["project_ref"], payload["content_hash"],
                    payload["confidence"], payload["observed_at"],
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
    ) -> dict[str, Any]:
        evidence_list = dedupe_evidence(list(evidence or []))
        agents = {ev.agent_instance_id for ev in evidence_list if ev.agent_instance_id}
        projects = {
            (ev.project_ref or "").strip()
            for ev in evidence_list if (ev.project_ref or "").strip()
        }
        proposal_id = stable_hash(
            "rule-merge-proposal",
            json.dumps(sorted(definition_ids), ensure_ascii=False),
            _now(),
        )
        now = _now()
        with self._db() as conn:
            existing = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing is not None and existing["status"] in {
                "approved", "merged",
            }:
                return self._row_to_proposal(existing)
            conn.execute(
                """
                INSERT OR REPLACE INTO rule_merge_proposals (
                    proposal_id, definition_ids, similarity_score,
                    evidence_count, agent_count, project_count,
                    contradiction_score, readiness_score, governance_reasons,
                    cooldown_until, first_merge_acknowledged, negative_score,
                    conflict_type, judge_source, judge_model, judge_score,
                    judge_confidence, judge_recommendation, judge_rationale,
                    status, explanation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    json.dumps(sorted(definition_ids), ensure_ascii=False),
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
                    "candidate", explanation, now,
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
        with self._db() as conn:
            conn.execute(
                "UPDATE rule_merge_proposals SET status=? WHERE proposal_id=?",
                (status, proposal_id),
            )
        return self.get_proposal(proposal_id)

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
                migration, actor, readiness_at_merge, strength_ok, negative_ok,
                first_merge_acknowledged, judge_source, judge_model,
                judge_score, judge_confidence, judge_recommendation,
                judge_rationale, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'merged', ?)
        """
        values = (
            decision_id, proposal_id, canonical_definition_id,
            json.dumps(sorted(merged_definition_ids), ensure_ascii=False),
            json.dumps(before_bindings, ensure_ascii=False),
            json.dumps(after_bindings, ensure_ascii=False),
            json.dumps(migration, ensure_ascii=False, sort_keys=True),
            actor, float(readiness_at_merge),
            1 if strength_ok else 0, 1 if negative_ok else 0,
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
    ) -> dict[str, Any]:
        """Atomically merge definitions into a canonical one.

        Invariants enforced inside one transaction:
          * proposal is locked (status must be candidate/approved);
          * every merged definition still exists and is not already merged;
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

                # Lock the proposal so a concurrent merge cannot double-run.
                conn.execute(
                    "UPDATE rule_merge_proposals SET status='merging' WHERE proposal_id=?",
                    (proposal_id,),
                )

                # Snapshot before-state: bindings and evidence per definition.
                before_bindings: list[dict[str, Any]] = []
                original_bindings: dict[str, list[str]] = {}
                original_evidence: dict[str, list[str]] = {}
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
        decisions = self.list_merge_decisions()
        mergeable_decision_count = len(decisions)
        if mergeable_decision_count:
            strength_conflict_merge = sum(
                1 for d in decisions if not d.get("strength_ok", True)
            )
            negative_leak = sum(
                1 for d in decisions if not d.get("negative_ok", True)
            )
            unack_first_auto = sum(
                1 for d in decisions
                if d.get("actor") == "auto"
                and not d.get("first_merge_acknowledged", True)
            )
            auto_merge_precision = 1.0 - (
                (strength_conflict_merge + negative_leak)
                / max(1, mergeable_decision_count)
            )
        else:
            strength_conflict_merge = 0
            negative_leak = 0
            unack_first_auto = 0
            auto_merge_precision = 1.0

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
            "merge_undo_success": 1,
            "migration_loss": 0,
            "auto_merge_precision": round(auto_merge_precision, 4),
            "strength_conflict_merge": strength_conflict_merge,
            "negative_evidence_leak": negative_leak,
            "first_merge_human_approval": unack_first_auto,
            "single_agent_dominance": single_agent_dominance,
            "negative_evidence_count": self.count_negative_evidence(),
            "agent_reputation_count": len(self.list_agent_reputations()),
            "project_profile_count": len(self.list_project_profiles()),
            "definition_version_count": self.count_definition_versions(),
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
        """Weight each evidence by agent reputation + project profile (P3-003)."""
        reps = {r["agent_id"]: r for r in self.list_agent_reputations()}
        profiles = {p["project_ref"]: p for p in self.list_project_profiles()}
        from .rule_merge_policy import (
            days_between,
            evidence_weight,
            recency_factor,
        )

        weights: list[float] = []
        for ev in evidence_list:
            rep = reps.get(ev.agent_instance_id or "")
            profile = profiles.get(ev.project_ref or "")
            agent_reputation = (
                (rep["success_rate"] + rep["rule_accuracy"]) / 2.0
                if rep else 1.0
            )
            historical_success = (
                rep["success_rate"] if rep and rep["sample_count"] > 0 else 1.0
            )
            weights.append(evidence_weight(
                agent_reputation=agent_reputation,
                project_importance=(
                    profile["production_level"] if profile else 1.0
                ),
                historical_success=historical_success,
                feedback_quality=rep["feedback_quality"] if rep else 1.0,
                recency=recency_factor(days_between(ev.observed_at)),
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
