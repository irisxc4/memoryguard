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
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .rule_binding import (
    AUTO_ALLOWED_TARGET_TYPES,
    RuleBinding,
    binding_identity_key,
    build_binding,
)
from .rule_definition import (
    POLARITY_POSITIVE,
    STRENGTH_UNKNOWN,
    RuleDefinition,
    build_definition,
)
from .rule_evidence import RuleEvidence, dedupe_evidence
from .rule_evidence_ledger import (
    EVIDENCE_LEDGER_SCHEMA,
    EvidenceContribution,
    build_contribution,
    contribution_from_row,
    list_effective,
    rebuild_effective,
    upsert_contribution,
)
from .governance_lock import WorkspaceGovernanceLock
from .rule_merge_policy import (
    MERGE_POLICY_VERSION,
    AUTO_MERGE_SCORE,
    AUTO_READINESS_SCORE,
    INTENTION_MATCH_THRESHOLD,
    MAX_SINGLE_SOURCE_RATIO,
    MIN_REPUTATION_SAMPLES,
    NEGATIVE_EVIDENCE_THRESHOLD,
    bayesian_accuracy,
    contradiction_score,
    build_maturity_snapshot,
    build_readiness_snapshot,
    compute_layers,
    days_between,
    evidence_weight,
    feedback_authority_score,
    largest_source_ratio,
    maturity_score,
    merge_match_kind,
    negative_evidence_score,
    parameter_conflict,
    project_importance_score,
    recency_factor,
    weighted_evidence_score,
)
from .rule_scope import assignment_matches, canonical_project_ref, normalize_assignment
from .governance_capability import (
    GOVERNANCE_CAPABILITY_SCHEMA,
    CapabilityRecord,
    consume_capability_record,
    issue_capability,
)
from .access_context import AccessContext, session_trust_is_valid
from .schema_v3 import (
    EffectiveAgentContext,
    RuleAssignment,
    _now_iso,
    stable_hash,
)

_RULE_INTELLIGENCE_DIR = "rule-intelligence"
# Human review may relax evidence/readiness soft gates, never basic semantic
# identity.  This floor is deliberately above noise and below auto-merge.
HUMAN_MERGE_MIN_SIMILARITY = 0.70

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
CREATE TABLE IF NOT EXISTS rule_binding_contributions (
    contribution_id TEXT PRIMARY KEY,
    share_group_id TEXT NOT NULL,
    source_memory_id TEXT NOT NULL,
    source_revision TEXT NOT NULL DEFAULT '',
    legacy_assignment_hash TEXT NOT NULL DEFAULT '',
    definition_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    project_ref TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    runtime_role TEXT NOT NULL DEFAULT '',
    effect TEXT NOT NULL DEFAULT 'include',
    priority INTEGER NOT NULL DEFAULT 0,
    owner_agent_id TEXT NOT NULL DEFAULT '',
    audience TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (binding_id) REFERENCES rule_bindings(binding_id),
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id),
    UNIQUE (share_group_id, source_memory_id, legacy_assignment_hash)
);
CREATE INDEX IF NOT EXISTS idx_rule_binding_contributions_binding
    ON rule_binding_contributions(binding_id);
CREATE INDEX IF NOT EXISTS idx_rule_binding_contributions_source
    ON rule_binding_contributions(share_group_id, source_memory_id);
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
    feedback_authority INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
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
    readiness_components TEXT NOT NULL DEFAULT '{}',
    readiness_digest TEXT NOT NULL DEFAULT '',
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
    negative_digest TEXT NOT NULL DEFAULT '',
    binding_digest TEXT NOT NULL DEFAULT '',
    runtime_digest TEXT NOT NULL DEFAULT '',
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
    undone_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (proposal_id) REFERENCES rule_merge_proposals(proposal_id),
    FOREIGN KEY (canonical_definition_id) REFERENCES rule_definitions(definition_id)
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
    expires_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (proposal_id) REFERENCES rule_merge_proposals(proposal_id)
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
    active INTEGER NOT NULL DEFAULT 1,
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
    created_at TEXT NOT NULL,
    FOREIGN KEY (old_definition_id) REFERENCES rule_definitions(definition_id),
    FOREIGN KEY (new_definition_id) REFERENCES rule_definitions(definition_id)
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
    last_observed_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
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
    created_at TEXT NOT NULL,
    FOREIGN KEY (definition_id) REFERENCES rule_definitions(definition_id)
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
CREATE INDEX IF NOT EXISTS idx_rule_effective_feedback_definition
    ON rule_effective_feedback_projection(definition_id);
CREATE TABLE IF NOT EXISTS rule_projection_state (
    scope_id TEXT PRIMARY KEY,
    last_outbox_event_id TEXT NOT NULL DEFAULT '',
    last_projected_event_id TEXT NOT NULL DEFAULT '',
    projection_lag INTEGER NOT NULL DEFAULT 0,
    projection_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rule_reconciliation_jobs (
    job_id TEXT PRIMARY KEY,
    share_group_id TEXT NOT NULL,
    source_digest TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_model',
    phase TEXT NOT NULL DEFAULT 'model',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    model_mode TEXT NOT NULL DEFAULT '',
    result_json TEXT NOT NULL DEFAULT '',
    canonical_digest_before TEXT NOT NULL DEFAULT '',
    canonical_digest_after TEXT NOT NULL DEFAULT '',
    projection_version TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_reconciliation_jobs_group
    ON rule_reconciliation_jobs(share_group_id, status);
CREATE TABLE IF NOT EXISTS rule_canonical_state (
    share_group_id TEXT PRIMARY KEY,
    activation_status TEXT NOT NULL DEFAULT '',
    canonical_digest TEXT NOT NULL DEFAULT '',
    read_path TEXT NOT NULL DEFAULT 'legacy',
    activated_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

# Fresh RuleMergeStore databases must have the same append-only evidence ledger
# as v2 migrations.  Keep this fragment after the base definition tables: the
# ledger has a foreign key to rule_definitions.
_SCHEMA += EVIDENCE_LEDGER_SCHEMA
_SCHEMA += "\n" + GOVERNANCE_CAPABILITY_SCHEMA


def _now() -> str:
    return _now_iso()


def _execute_sql_script_atomic(conn: sqlite3.Connection, script: str) -> None:
    """Execute schema statements without executescript's implicit commit."""
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


class RuleMergeStore:
    """Cross-group SQLite storage for Definitions, Bindings and Evidence."""

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()
        base = self.workspace / ".memoryguard" / _RULE_INTELLIGENCE_DIR
        self.root = base
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "memory.db"
        # SharedMemoryStore and RuleMergeStore coordinate through this exact
        # workspace lock.  Re-entry is supported by WorkspaceGovernanceLock,
        # so higher-level lifecycle operations can compose safely.
        self._governance_lock = WorkspaceGovernanceLock(self.workspace)
        self._write_state = threading.local()
        self._init_db()
        self._bootstrap_pending_feedback_source_links()

    # ------------------------------------------------------------------
    # connection helpers
    # ------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _write_conn(self) -> Iterator[sqlite3.Connection]:
        """Yield one locked connection with one explicit write transaction."""
        active = getattr(self._write_state, "conn", None)
        if active is not None:
            # Nested store mutations on one thread share the caller's
            # transaction; a second BEGIN IMMEDIATE would split the atomic
            # unit and can self-deadlock on SQLite.
            yield active
            return
        with self._governance_lock:
            conn = self._db()
            self._write_state.conn = conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                if getattr(self._write_state, "conn", None) is conn:
                    del self._write_state.conn
                conn.close()

    def _active_write_conn(self) -> sqlite3.Connection | None:
        return getattr(self._write_state, "conn", None)

    @contextmanager
    def _read_conn(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection for reads that is safe inside an open write txn.

        A read on a *second* connection while the same thread holds ``BEGIN
        IMMEDIATE`` on the active write connection can raise
        ``OperationalError: database is locked`` against our own transaction —
        a self-lock, not external contention.  Reads that may run inside
        ``_write_conn()`` (backfill, sync, outbox, reconciliation) therefore
        reuse the active write connection; only when no write transaction is
        active is an independent read connection opened.  The DB is
        rollback-journal (not WAL), so opening a second connection here is
        never a workaround — this reuse is the fix.
        """
        active = getattr(self._write_state, "conn", None)
        if active is not None:
            yield active
            return
        conn = self._db()
        try:
            yield conn
        finally:
            conn.close()

    def governance_lock(
        self, *, timeout: float | None = None, poll_interval: float | None = None,
    ) -> WorkspaceGovernanceLock:
        """Expose the same re-entrant workspace lock used by Store writes."""
        if timeout is None and poll_interval is None:
            return self._governance_lock
        return WorkspaceGovernanceLock(
            self.workspace,
            timeout=(self._governance_lock.timeout if timeout is None else timeout),
            poll_interval=(
                self._governance_lock.poll_interval
                if poll_interval is None else poll_interval
            ),
        )

    @staticmethod
    def _trusted_principal(access_context: AccessContext) -> str:
        if not isinstance(access_context, AccessContext):
            raise ValueError("trusted AccessContext required")
        ok, error = access_context.require_capability_issue()
        if not ok:
            raise ValueError(error)
        return access_context.principal

    def issue_merge_capability(
        self,
        proposal_id: str,
        access_context: AccessContext,
        **kwargs: Any,
    ) -> str:
        """Issue a server-owned merge capability for one proposal.

        The raw bearer token is returned once.  Issuance is deliberately tied
        to a trusted admin ``AccessContext`` and the proposal row is checked
        while the same workspace/SQLite write transaction is held.
        """
        self._trusted_principal(access_context)
        with self._write_conn() as conn:
            row = conn.execute(
                "SELECT status FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("rule_merge_proposal_not_found")
            if str(row["status"] or "") != "candidate":
                raise ValueError("rule_merge_proposal_not_approvable")
            return issue_capability(conn, access_context, proposal_id, **kwargs)

    # Explicit server/API spellings; all route through the same guarded path.
    issue_capability = issue_merge_capability

    @staticmethod
    def _capability_expiry_text(record: CapabilityRecord) -> str:
        return datetime.fromtimestamp(
            record.expires_at, tz=timezone.utc,
        ).isoformat()

    def _init_db(self) -> None:
        with self._write_conn() as conn:
            _execute_sql_script_atomic(conn, _SCHEMA)
            self._apply_upgrade(conn)

    def _bootstrap_pending_feedback_source_links(self) -> None:
        """Materialize trusted MCP feedback ownership before service wrapping.

        The public MCP route creates a legacy feedback event before it creates
        a merge service.  The service intentionally exposes only committed
        source links, so this narrowly-scoped compatibility bridge must run at
        store construction time.  It is disabled outside strict binding mode;
        generic outbox consumers and migration/backfill remain fail-closed.
        """
        if os.environ.get("MEMORYGUARD_STRICT_BINDING", "") != "1":
            return
        try:
            from .shared_memory_store import SharedMemoryStore

            groups = iter_legacy_groups(self.workspace)
            for group_id, _db_path in groups:
                legacy = SharedMemoryStore(
                    self.workspace, group_id, must_exist=True,
                )
                memory_ids = {
                    str(event.get("memory_id") or "")
                    for event in legacy.list_unconsumed_rule_events()
                    if (
                        str(event.get("event_type") or "")
                        == "effective_rule_feedback_changed"
                        and str(event.get("memory_id") or "")
                    )
                }
                for memory_id in memory_ids:
                    record = legacy.get_record(memory_id)
                    if record is None:
                        continue
                    status = getattr(record.status, "value", record.status)
                    if (
                        str(status) != "active"
                        or str(record.injection_policy or "") != "always"
                    ):
                        continue
                    with self._db() as conn:
                        linked = conn.execute(
                            "SELECT 1 FROM rule_source_links "
                            "WHERE share_group_id=? AND memory_id=?",
                            (group_id, memory_id),
                        ).fetchone()
                    if linked is not None:
                        continue
                    definition = build_definition(
                        record.body,
                        kind=record.kind,
                        confidence=record.confidence,
                        created_at=record.created_at,
                    )
                    canonical_id = self.resolve_canonical(
                        definition.definition_id,
                    )
                    target = self.get_definition(canonical_id)
                    if target is None or target.status != "active":
                        canonical_id = definition.definition_id
                    self.upsert_source_link(
                        share_group_id=group_id,
                        memory_id=memory_id,
                        source_revision=(
                            record.updated_at or record.created_at or ""
                        ),
                        original_definition_id=definition.definition_id,
                        canonical_definition_id=canonical_id,
                        status="active",
                    )
        except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
            return

    # ------------------------------------------------------------------
    # in-place upgrade for databases created before the governance layer
    # ------------------------------------------------------------------

    _UPGRADE_COLUMNS: tuple[tuple[str, str, str], ...] = (
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

    @staticmethod
    def _validate_reference_integrity(conn: sqlite3.Connection) -> None:
        """Fail closed when legacy tables contain orphaned core references.

        Fresh databases enforce safe scalar references with SQLite foreign keys.
        SQLite cannot add a foreign key to an existing table with ``ALTER TABLE``
        without rebuilding it, and several historical columns intentionally use
        ``''`` as an unresolved compatibility value.  Upgrade therefore keeps
        old tables readable only when every non-empty reference resolves; an
        orphan aborts the whole transaction instead of being silently accepted.
        """
        checks = (
            (
                "rule_bindings.definition_id",
                """
                SELECT 1 FROM rule_bindings child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_binding_contributions.definition_id",
                """
                SELECT 1 FROM rule_binding_contributions child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_binding_contributions.binding_id",
                """
                SELECT 1 FROM rule_binding_contributions child
                LEFT JOIN rule_bindings parent
                  ON parent.binding_id=child.binding_id
                WHERE child.binding_id <> '' AND parent.binding_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_evidence.definition_id",
                """
                SELECT 1 FROM rule_evidence child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_negative_evidence.definition_id",
                """
                SELECT 1 FROM rule_negative_evidence child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_definition_versions.definition_id",
                """
                SELECT 1 FROM rule_definition_versions child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_definition_aliases.old_definition_id",
                """
                SELECT 1 FROM rule_definition_aliases child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.old_definition_id
                WHERE child.old_definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_definition_aliases.new_definition_id",
                """
                SELECT 1 FROM rule_definition_aliases child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.new_definition_id
                WHERE child.new_definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_definition_runtime_stats.definition_id",
                """
                SELECT 1 FROM rule_definition_runtime_stats child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_runtime_feedback.definition_id",
                """
                SELECT 1 FROM rule_runtime_feedback child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_effective_feedback_projection.definition_id",
                """
                SELECT 1 FROM rule_effective_feedback_projection child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_evidence_contributions.definition_id",
                """
                SELECT 1 FROM rule_evidence_contributions child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_evidence_effective.definition_id",
                """
                SELECT 1 FROM rule_evidence_effective child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.definition_id
                WHERE child.definition_id <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_evidence_effective.winner_contribution_id",
                """
                SELECT 1 FROM rule_evidence_effective child
                LEFT JOIN rule_evidence_contributions parent
                  ON parent.contribution_id=child.winner_contribution_id
                WHERE child.winner_contribution_id <> ''
                  AND parent.contribution_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_merge_decisions.proposal_id",
                """
                SELECT 1 FROM rule_merge_decisions child
                LEFT JOIN rule_merge_proposals parent
                  ON parent.proposal_id=child.proposal_id
                WHERE child.proposal_id <> '' AND parent.proposal_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_merge_decisions.canonical_definition_id",
                """
                SELECT 1 FROM rule_merge_decisions child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.canonical_definition_id
                WHERE child.canonical_definition_id <> ''
                  AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_merge_approvals.proposal_id",
                """
                SELECT 1 FROM rule_merge_approvals child
                LEFT JOIN rule_merge_proposals parent
                  ON parent.proposal_id=child.proposal_id
                WHERE child.proposal_id <> '' AND parent.proposal_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_source_links.canonical_definition_id",
                """
                SELECT 1 FROM rule_source_links child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.canonical_definition_id
                WHERE child.canonical_definition_id <> ''
                  AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
            (
                "rule_definitions.superseded_by",
                """
                SELECT 1 FROM rule_definitions child
                LEFT JOIN rule_definitions parent
                  ON parent.definition_id=child.superseded_by
                WHERE child.superseded_by <> '' AND parent.definition_id IS NULL
                LIMIT 1
                """,
            ),
        )
        for label, sql in checks:
            if conn.execute(sql).fetchone() is not None:
                raise sqlite3.IntegrityError(
                    f"rule_reference_integrity_failed:{label}"
                )

        definitions = {
            str(row["definition_id"])
            for row in conn.execute(
                "SELECT definition_id FROM rule_definitions"
            ).fetchall()
        }
        proposals = {
            str(row["proposal_id"])
            for row in conn.execute(
                "SELECT proposal_id FROM rule_merge_proposals"
            ).fetchall()
        }
        for table, column, known, label in (
            ("rule_merge_proposals", "definition_ids", definitions, "proposal.definition_ids"),
            ("rule_merge_decisions", "merged_definition_ids", definitions, "decision.merged_definition_ids"),
        ):
            for row in conn.execute(f"SELECT {column} FROM {table}").fetchall():
                try:
                    values = json.loads(row[column] or "[]")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise sqlite3.IntegrityError(
                        f"rule_reference_integrity_failed:{label}.json"
                    ) from exc
                if not isinstance(values, list) or any(
                    str(value) not in known for value in values
                ):
                    raise sqlite3.IntegrityError(
                        f"rule_reference_integrity_failed:{label}"
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
        # Also upgrades databases created before the standalone ledger was
        # introduced.  CREATE IF NOT EXISTS is safe inside caller transaction.
        _execute_sql_script_atomic(conn, EVIDENCE_LEDGER_SCHEMA)
        for table, column, ddl in self._UPGRADE_COLUMNS:
            if column not in self._existing_columns(conn, table):
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )
        self._upgrade_legacy_evidence_ledger(conn)
        ledger_definition_ids = {
            str(row["definition_id"] or "")
            for row in conn.execute(
                "SELECT DISTINCT definition_id "
                "FROM rule_evidence_contributions"
            ).fetchall()
            if str(row["definition_id"] or "")
        }
        self._materialize_evidence_compat_conn(conn, ledger_definition_ids)
        # Repair pre-ledger databases deterministically: an active materialized
        # binding without an active source contribution is a ghost and must
        # fail closed until its owner source is replayed.
        conn.execute(
            """
            UPDATE rule_bindings
            SET status='revoked', revision=revision+1, updated_at=?
            WHERE status='active'
              AND NOT EXISTS (
                  SELECT 1 FROM rule_binding_contributions c
                  WHERE c.binding_id=rule_bindings.binding_id
                    AND c.active=1 AND c.status='active'
              )
            """,
            (_now(),),
        )
        conn.execute(
            """
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
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS "
            "idx_rule_effective_feedback_definition "
            "ON rule_effective_feedback_projection(definition_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_projection_state (
                scope_id TEXT PRIMARY KEY,
                last_outbox_event_id TEXT NOT NULL DEFAULT '',
                last_projected_event_id TEXT NOT NULL DEFAULT '',
                projection_lag INTEGER NOT NULL DEFAULT 0,
                projection_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # Durable canonical-reconciliation jobs + group-level activation marker.
        # CREATE IF NOT EXISTS is safe inside the caller transaction for both
        # fresh and upgraded databases.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_reconciliation_jobs (
                job_id TEXT PRIMARY KEY,
                share_group_id TEXT NOT NULL,
                source_digest TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending_model',
                phase TEXT NOT NULL DEFAULT 'model',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                model_mode TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '',
                canonical_digest_before TEXT NOT NULL DEFAULT '',
                canonical_digest_after TEXT NOT NULL DEFAULT '',
                projection_version TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rule_reconciliation_jobs_group "
            "ON rule_reconciliation_jobs(share_group_id, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_canonical_state (
                share_group_id TEXT PRIMARY KEY,
                activation_status TEXT NOT NULL DEFAULT '',
                canonical_digest TEXT NOT NULL DEFAULT '',
                read_path TEXT NOT NULL DEFAULT 'legacy',
                activated_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
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
        self._dedupe_existing_independence_rows(conn, "rule_evidence")
        self._dedupe_existing_independence_rows(conn, "rule_negative_evidence")
        # Inactive compatibility rows remain as history; only active
        # materialized winners participate in the uniqueness wall.
        conn.execute("DROP INDEX IF EXISTS uq_rule_evidence_independent")
        conn.execute("DROP INDEX IF EXISTS uq_rule_negative_evidence_independent")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_evidence_independent "
            "ON rule_evidence(definition_id, independence_key) "
            "WHERE independence_key <> '' AND active=1"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_rule_negative_evidence_independent "
            "ON rule_negative_evidence(definition_id, independence_key) "
            "WHERE independence_key <> '' AND active=1"
        )
        self._validate_reference_integrity(conn)

    @staticmethod
    def _upgrade_legacy_evidence_ledger(conn: sqlite3.Connection) -> None:
        """Import every legacy evidence row into the append-only ledger.

        The compatibility tables may contain both active winners and inactive
        history from before the ledger existed.  Import both states first,
        then rebuild the effective projection so a later deactivation can
        restore the deterministic runner-up instead of losing history.
        """
        definitions: set[str] = set()
        for table, polarity in (
            ("rule_evidence", "positive"),
            ("rule_negative_evidence", "negative"),
        ):
            for row in conn.execute(
                f"SELECT * FROM {table} ORDER BY evidence_id"
            ).fetchall():
                evidence_id = str(row["evidence_id"] or "")
                if not evidence_id:
                    continue
                if conn.execute(
                    "SELECT 1 FROM rule_evidence_contributions "
                    "WHERE source_evidence_id=? AND polarity=? LIMIT 1",
                    (evidence_id, polarity),
                ).fetchone() is not None:
                    continue
                definition_id = str(row["definition_id"] or "")
                if not definition_id:
                    continue
                source_rule_id = str(row["source_rule_id"] or "")
                agent_instance_id = str(row["agent_instance_id"] or "")
                project_ref = str(row["project_ref"] or "")
                session_id = str(row["session_id"] or "")
                source_root_id = str(row["source_root_id"] or "")
                source_object_id = str(row["source_object_id"] or "")
                content_hash = str(row["content_hash"] or "")
                independence_key = str(row["independence_key"] or "")
                if not independence_key:
                    independence_key = stable_hash(
                        "rule-evidence-legacy-independence",
                        project_ref, agent_instance_id, source_root_id,
                        source_object_id or session_id, content_hash,
                    )
                source_ids = {
                    "evidence_id": evidence_id,
                    "source_rule_id": source_rule_id,
                    "receipt_id": str(row["receipt_id"] or ""),
                    "feedback_id": str(row["feedback_id"] or ""),
                    "content_hash": content_hash,
                    "semantic_hash": str(row["semantic_hash"] or ""),
                    "provider": str(row["provider"] or ""),
                    "source_root_id": source_root_id,
                    "source_object_id": source_object_id,
                }
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
                    source_ids=source_ids,
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

        definitions.update(
            str(row["definition_id"] or "")
            for row in conn.execute(
                "SELECT DISTINCT definition_id "
                "FROM rule_evidence_contributions"
            ).fetchall()
            if str(row["definition_id"] or "")
        )
        for definition_id in sorted(definitions):
            rebuild_effective(conn, definition_id=definition_id)

    @staticmethod
    def _dedupe_existing_independence_rows(
        conn: sqlite3.Connection, table: str,
    ) -> None:
        """Keep strongest/latest row before installing independence UNIQUE."""
        if table not in {"rule_evidence", "rule_negative_evidence"}:
            raise ValueError("invalid evidence table")
        rows = conn.execute(
            f"SELECT rowid, definition_id, independence_key, "
            f"feedback_authority, confidence, observed_at "
            f"FROM {table} WHERE independence_key <> '' "
            f"ORDER BY definition_id, independence_key, "
            f"feedback_authority DESC, confidence DESC, observed_at DESC, rowid DESC"
        ).fetchall()
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row["definition_id"] or ""), str(row["independence_key"] or ""))
            if key in seen:
                conn.execute(
                    f"UPDATE {table} SET active=0 WHERE rowid=?",
                    (row["rowid"],),
                )
            else:
                seen.add(key)

    @classmethod
    def _rehome_evidence_rows(
        cls,
        conn: sqlite3.Connection,
        table: str,
        old_definition_id: str,
        new_definition_id: str,
        *,
        source_rule_id: str = "",
    ) -> None:
        """Move evidence across Definition ids without losing a collision.

        V2 identity splits can encounter the same independent observation on
        both sides of the split.  A blind UPDATE violates the unique
        independence invariant; choose the deterministic winner first, then
        move only the surviving row inside the migration transaction.
        """
        if table not in {"rule_evidence", "rule_negative_evidence"}:
            raise ValueError("invalid evidence table")
        clauses = [
            "definition_id=?",
            "source_root_id <> 'ambiguous_migration_evidence'",
        ]
        params: list[Any] = [old_definition_id]
        if source_rule_id:
            clauses.append("source_rule_id=?")
            params.append(source_rule_id)
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
            + " ORDER BY rowid",
            params,
        ).fetchall()
        for row in rows:
            independence_key = str(row["independence_key"] or "")
            existing = None
            if independence_key:
                existing = conn.execute(
                    f"SELECT * FROM {table} WHERE definition_id=? "
                    "AND independence_key=?",
                    (new_definition_id, independence_key),
                ).fetchone()
            if existing is not None:
                candidate_wins = cls._evidence_payload_wins(
                    dict(row), existing,
                )
                if candidate_wins:
                    conn.execute(
                        f"DELETE FROM {table} WHERE evidence_id=?",
                        (existing["evidence_id"],),
                    )
                else:
                    conn.execute(
                        f"DELETE FROM {table} WHERE evidence_id=?",
                        (row["evidence_id"],),
                    )
                    continue
            conn.execute(
                f"UPDATE {table} SET definition_id=? WHERE evidence_id=?",
                (new_definition_id, row["evidence_id"]),
            )

    @staticmethod
    def _rehome_runtime_feedback_rows(
        conn: sqlite3.Connection,
        old_definition_id: str,
        new_definition_id: str,
    ) -> None:
        rows = conn.execute(
            "SELECT feedback_id FROM rule_runtime_feedback "
            "WHERE definition_id=? AND source<>? ORDER BY rowid",
            (old_definition_id, "ambiguous_migration"),
        ).fetchall()
        for row in rows:
            feedback_id = str(row["feedback_id"] or "")
            existing = conn.execute(
                "SELECT 1 FROM rule_runtime_feedback "
                "WHERE feedback_id=? AND definition_id<>?",
                (feedback_id, old_definition_id),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "DELETE FROM rule_runtime_feedback WHERE feedback_id=?",
                    (feedback_id,),
                )
                continue
            conn.execute(
                "UPDATE rule_runtime_feedback SET definition_id=? "
                "WHERE feedback_id=?",
                (new_definition_id, feedback_id),
            )

    # ------------------------------------------------------------------
    # Definitions
    # ------------------------------------------------------------------

    @staticmethod
    def _definition_identity_payload(definition: RuleDefinition) -> tuple[str, ...]:
        """Return the immutable semantic core used by one Definition id."""
        return (
            str(definition.canonical_text or ""),
            str(definition.normalized_intent or ""),
            str(definition.rule_kind or ""),
            str(definition.polarity or ""),
            str(definition.semantic_hash or ""),
            str(definition.parameter_schema or "{}"),
            str(definition.rule_strength or ""),
        )

    @staticmethod
    def _definition_identity_row(row: sqlite3.Row) -> tuple[str, ...]:
        return (
            str(row["canonical_text"] or ""),
            str(row["normalized_intent"] or ""),
            str(row["rule_kind"] or ""),
            str(row["polarity"] or ""),
            str(row["semantic_hash"] or ""),
            str(row["parameter_schema"] or "{}"),
            str(row["rule_strength"] or ""),
        )

    def upsert_definition(self, definition: RuleDefinition) -> RuleDefinition:
        payload = definition.to_dict()
        with self._write_conn() as conn:
            existing = conn.execute(
                "SELECT * FROM rule_definitions WHERE definition_id=?",
                (payload["definition_id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO rule_definitions (
                        definition_id, canonical_text, normalized_intent, rule_kind,
                        polarity, semantic_hash, parameter_schema, status, confidence,
                        revision, rule_strength, maturity_state,
                        created_at, updated_at, superseded_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["definition_id"], payload["canonical_text"],
                        payload["normalized_intent"], payload["rule_kind"],
                        payload["polarity"], payload["semantic_hash"],
                        payload["parameter_schema"], payload["status"],
                        payload["confidence"], payload["rule_strength"],
                        payload["maturity_state"], payload["created_at"],
                        payload["updated_at"], payload["superseded_by"],
                    ),
                )
            else:
                if self._definition_identity_row(existing) != (
                    self._definition_identity_payload(definition)
                ):
                    raise ValueError("definition_identity_mismatch")
                state_changed = any(
                    existing[name] != payload[name]
                    for name in ("status", "confidence", "maturity_state", "superseded_by")
                )
                if state_changed:
                    # The persisted revision is authoritative.  Never accept a
                    # caller-provided revision, including a stale lower value.
                    conn.execute(
                        """
                        UPDATE rule_definitions SET
                            status=?, confidence=?, maturity_state=?,
                            superseded_by=?, revision=revision+1, updated_at=?
                        WHERE definition_id=?
                        """,
                        (
                            payload["status"], payload["confidence"],
                            payload["maturity_state"], payload["superseded_by"],
                            _now(), payload["definition_id"],
                        ),
                    )
            row = conn.execute(
                "SELECT * FROM rule_definitions WHERE definition_id=?",
                (payload["definition_id"],),
            ).fetchone()
            assert row is not None
            return self._row_to_definition(row)

    def get_definition(self, definition_id: str) -> RuleDefinition | None:
        active = self._active_write_conn()
        if active is not None:
            row = active.execute(
                "SELECT * FROM rule_definitions WHERE definition_id=?",
                (definition_id,),
            ).fetchone()
        else:
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
            confidence=float(
                row["confidence"] if row["confidence"] is not None else 1.0
            ),
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
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE rule_definitions SET status=?, superseded_by=?, "
                "revision=revision+1, updated_at=? "
                "WHERE definition_id=?",
                (status, superseded_by, _now(), definition_id),
            )

    def set_definition_maturity(self, definition_id: str, state: str) -> None:
        """Persist the recomputed maturity stage of one definition."""
        with self._write_conn() as conn:
            conn.execute(
                "UPDATE rule_definitions SET maturity_state=?, revision=revision+1, "
                "updated_at=? "
                "WHERE definition_id=?",
                (state, _now(), definition_id),
            )

    def bump_definition_revision(self, definition_id: str) -> None:
        """Bump a definition's revision (a content/state edit marker).

        The merge transaction refuses a human-approved merge whose expected
        definition revisions no longer match, so an edit between approval and
        execution is detected instead of silently merging drifted state.
        """
        with self._write_conn() as conn:
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
        with self._write_conn() as conn:
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
        source_rule_id: str = "",
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
        with self._write_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchone()
                if row is None or row["status"] in {"alias", "merged"}:
                    return None
                if conn.execute(
                    "SELECT 1 FROM rule_definition_aliases WHERE old_definition_id=?",
                    (old_definition_id,),
                ).fetchone():
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
                self._rehome_evidence_rows(
                    conn, "rule_evidence", old_definition_id,
                    new_definition_id, source_rule_id=source_rule_id,
                )
                self._rehome_evidence_rows(
                    conn, "rule_negative_evidence", old_definition_id,
                    new_definition_id, source_rule_id=source_rule_id,
                )
                self._rehome_runtime_feedback_rows(
                    conn, old_definition_id, new_definition_id,
                )
                conn.execute(
                    "UPDATE rule_effective_feedback_projection "
                    "SET definition_id=? WHERE definition_id=?",
                    (new_definition_id, old_definition_id),
                )
                for binding_row in binding_rows:
                    duplicate = conn.execute(
                        "SELECT * FROM rule_bindings "
                        "WHERE definition_id=? AND status='active'",
                        (new_definition_id,),
                    ).fetchall()
                    old_binding = self._row_to_binding(binding_row)
                    if any(
                        binding_identity_key(self._row_to_binding(item))
                        == binding_identity_key(old_binding)
                        for item in duplicate
                    ):
                        target = next(
                            item for item in duplicate
                            if binding_identity_key(self._row_to_binding(item))
                            == binding_identity_key(old_binding)
                        )
                        conn.execute(
                            "UPDATE rule_binding_contributions SET "
                            "definition_id=?, binding_id=?, revision=revision+1, "
                            "updated_at=? WHERE binding_id=?",
                            (
                                new_definition_id, target["binding_id"], now,
                                binding_row["binding_id"],
                            ),
                        )
                        conn.execute(
                            "DELETE FROM rule_bindings WHERE binding_id=?",
                            (binding_row["binding_id"],),
                        )
                    else:
                        conn.execute(
                            "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                            "updated_at=? WHERE binding_id=?",
                            (new_definition_id, now, binding_row["binding_id"]),
                        )
                        conn.execute(
                            "UPDATE rule_binding_contributions SET definition_id=? "
                            "WHERE binding_id=?",
                            (new_definition_id, binding_row["binding_id"]),
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
            except Exception:
                raise
        return removed_audiences

    def split_legacy_evidence(
        self,
        old_definition_id: str,
        source_targets: dict[str, str],
    ) -> list[tuple[Any, ...]] | None:
        """Split v1 evidence by legacy source before aliasing the v1 row.

        A v1 id was surface-only, so MUST/SHOULD records could share it.  The
        old row must stay active while this transaction routes each known
        ``source_rule_id`` to its v2 definition; callers alias it only after
        rebuilding the source bindings.  Unknown rows receive an explicit
        migration marker and are conservatively handled by the finalizer.
        """
        targets = {
            str(source): str(target)
            for source, target in dict(source_targets or {}).items()
            if str(source) and str(target)
        }
        if not targets:
            return None
        now = _now()
        with self._write_conn() as conn:
            try:
                row = conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchone()
                if row is None or row["status"] in {"alias", "merged"}:
                    return None
                for target in set(targets.values()):
                    target_row = conn.execute(
                        "SELECT 1 FROM rule_definitions WHERE definition_id=?",
                        (target,),
                    ).fetchone()
                    if target_row is None:
                        raise ValueError("v2_migration_target_missing")
                binding_rows = conn.execute(
                    "SELECT * FROM rule_bindings WHERE definition_id=? AND status='active'",
                    (old_definition_id,),
                ).fetchall()
                removed_audiences = [
                    binding_identity_key(self._row_to_binding(item))
                    for item in binding_rows
                ]
                # A strength collision must route each legacy binding
                # contribution by source. Never move an unowned remainder to
                # the lexicographically first target; quarantine it instead.
                collision = len(set(targets.values())) > 1
                if collision:
                    original_audiences: list[str] = []
                    affected_binding_ids: set[str] = set()
                    for binding_row in binding_rows:
                        old_binding = self._row_to_binding(binding_row)
                        contribution_rows = conn.execute(
                            "SELECT * FROM rule_binding_contributions "
                            "WHERE binding_id=?",
                            (old_binding.binding_id,),
                        ).fetchall()
                        if any(
                            str(row["source_memory_id"] or "") in targets
                            for row in contribution_rows
                        ):
                            original_audiences.append(
                                binding_identity_key(old_binding)
                            )
                        routed = False
                        for contribution_row in contribution_rows:
                            source_id = str(
                                contribution_row["source_memory_id"] or ""
                            )
                            target = targets.get(source_id)
                            if target:
                                new_binding = build_binding(
                                    target,
                                    share_group_id=old_binding.share_group_id,
                                    target_type=old_binding.target_type,
                                    target_id=old_binding.target_id,
                                    project_ref=old_binding.project_ref,
                                    provider=old_binding.provider,
                                    runtime_role=old_binding.runtime_role,
                                    effect=old_binding.effect,
                                    priority=old_binding.priority,
                                    owner_agent_id=old_binding.owner_agent_id,
                                    created_by=old_binding.created_by,
                                    authorization=old_binding.authorization,
                                    created_at=old_binding.created_at,
                                )
                                self._upsert_binding_conn(conn, new_binding)
                                conn.execute(
                                    "UPDATE rule_binding_contributions SET "
                                    "definition_id=?, binding_id=?, updated_at=? "
                                    "WHERE contribution_id=?",
                                    (
                                        target, new_binding.binding_id, now,
                                        contribution_row["contribution_id"],
                                    ),
                                )
                                affected_binding_ids.add(new_binding.binding_id)
                                routed = True
                            else:
                                conn.execute(
                                    "UPDATE rule_binding_contributions SET active=0, "
                                    "status='quarantined', updated_at=? "
                                    "WHERE contribution_id=?",
                                    (now, contribution_row["contribution_id"]),
                                )
                        affected_binding_ids.add(old_binding.binding_id)
                        if not routed:
                            conn.execute(
                                "UPDATE rule_bindings SET status='revoked', "
                                "revision=revision+1, updated_at=? "
                                "WHERE binding_id=?",
                                (now, old_binding.binding_id),
                            )
                    self._materialize_affected_bindings_conn(
                        conn, affected_binding_ids,
                    )
                    remaining_audiences = [
                        binding_identity_key(self._row_to_binding(item))
                        for item in conn.execute(
                            "SELECT * FROM rule_bindings WHERE status='active' "
                            "AND definition_id IN ({})".format(
                                ",".join("?" for _ in set(targets.values()))
                            ),
                            list(set(targets.values())),
                        ).fetchall()
                    ]
                    if set(remaining_audiences) != set(original_audiences):
                        raise RuntimeError("v2_migration_binding_scope_loss")
                    # No single target owns the removed audience set; caller
                    # must not assume the primary target inherited it.
                    removed_audiences = []

                evidence_rows: list[sqlite3.Row] = []
                for table in ("rule_evidence", "rule_negative_evidence"):
                    evidence_rows.extend(conn.execute(
                        f"SELECT * FROM {table} WHERE definition_id=?",
                        (old_definition_id,),
                    ).fetchall())
                    for evidence_row in conn.execute(
                        f"SELECT * FROM {table} WHERE definition_id=?",
                        (old_definition_id,),
                    ).fetchall():
                        target = targets.get(str(evidence_row["source_rule_id"] or ""))
                        if target:
                            self._rehome_evidence_rows(
                                conn, table, old_definition_id, target,
                                source_rule_id=str(evidence_row["source_rule_id"] or ""),
                            )
                        else:
                            conn.execute(
                                f"UPDATE {table} SET source_root_id=?, active=0 "
                                "WHERE evidence_id=?",
                                (
                                    "ambiguous_migration_evidence",
                                    evidence_row["evidence_id"],
                                ),
                            )

                # Split the append-only evidence ledger by source too. The
                # effective table is only a projection: discard old-v1
                # winners and rebuild each target from its routed
                # contributions, never guess a winner for an unknown source.
                ledger_rows = conn.execute(
                    "SELECT * FROM rule_evidence_contributions "
                    "WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchall()
                target_definitions: set[str] = set()
                for ledger_row in ledger_rows:
                    source_id = str(
                        ledger_row["source_rule_id"]
                        or ledger_row["source_memory_id"]
                        or ""
                    )
                    target = targets.get(source_id)
                    if target:
                        conn.execute(
                            "UPDATE rule_evidence_contributions SET "
                            "definition_id=?, updated_at=? "
                            "WHERE contribution_id=?",
                            (target, now, ledger_row["contribution_id"]),
                        )
                        target_definitions.add(target)
                    else:
                        conn.execute(
                            "UPDATE rule_evidence_contributions SET active=0, "
                            "source_root_id='ambiguous_migration_evidence', "
                            "updated_at=? WHERE contribution_id=?",
                            (now, ledger_row["contribution_id"]),
                        )
                conn.execute(
                    "DELETE FROM rule_evidence_effective WHERE definition_id=?",
                    (old_definition_id,),
                )
                for target in sorted(target_definitions):
                    rebuild_effective(conn, definition_id=target)

                # Runtime/projection rows do not carry source_rule_id.  Resolve
                # them through the receipt/feedback identity of the evidence
                # that was just split; unknown rows remain on v1 for the
                # conservative finalizer instead of being guessed here.
                receipt_targets: dict[str, str] = {}
                feedback_targets: dict[str, str] = {}
                for evidence_row in evidence_rows:
                    target = targets.get(str(evidence_row["source_rule_id"] or ""))
                    if not target:
                        continue
                    receipt_id = str(evidence_row["receipt_id"] or "")
                    feedback_id = str(evidence_row["feedback_id"] or "")
                    if receipt_id:
                        receipt_targets[receipt_id] = target
                    if feedback_id:
                        feedback_targets[feedback_id] = target
                runtime_rows = conn.execute(
                    "SELECT * FROM rule_runtime_feedback WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchall()
                for runtime_row in runtime_rows:
                    target = (
                        feedback_targets.get(str(runtime_row["feedback_id"] or ""))
                        or receipt_targets.get(str(runtime_row["receipt_id"] or ""))
                    )
                    if target:
                        conn.execute(
                            "UPDATE rule_runtime_feedback SET definition_id=? "
                            "WHERE feedback_id=?",
                            (target, runtime_row["feedback_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE rule_runtime_feedback SET source=? "
                            "WHERE feedback_id=?",
                            ("ambiguous_migration", runtime_row["feedback_id"]),
                        )
                projection_rows = conn.execute(
                    "SELECT * FROM rule_effective_feedback_projection "
                    "WHERE definition_id=?",
                    (old_definition_id,),
                ).fetchall()
                for projection in projection_rows:
                    target = receipt_targets.get(str(projection["receipt_id"] or ""))
                    if target:
                        conn.execute(
                            "UPDATE rule_effective_feedback_projection SET definition_id=? "
                            "WHERE receipt_id=?",
                            (target, projection["receipt_id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE rule_effective_feedback_projection SET "
                            "effective_feedback_id='', outcome='tombstone' "
                            "WHERE receipt_id=?",
                            (projection["receipt_id"],),
                        )
                for source_id, target in targets.items():
                    conn.execute(
                        "UPDATE rule_source_links SET canonical_definition_id=?, "
                        "updated_at=? WHERE memory_id=? AND "
                        "canonical_definition_id=?",
                        (target, now, source_id, old_definition_id),
                    )
            except Exception:
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
        with self._write_conn() as conn:
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

    def _get_source_link_conn(
        self, conn: sqlite3.Connection, share_group_id: str, memory_id: str,
    ) -> dict[str, Any] | None:
        """Query one source link on an explicitly provided connection.

        This is the query core of :meth:`get_source_link`.  Callers that
        already hold a connection (a backfill/sync/reconciliation write
        transaction) pass it explicitly so the read never opens a second
        connection to the same database file while ``BEGIN IMMEDIATE`` is
        active.
        """
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

    def get_source_link(
        self, share_group_id: str, memory_id: str,
    ) -> dict[str, Any] | None:
        """Return the persisted source link, or None.

        This is a pure read: it never derives a Definition, never opens the
        legacy store and never writes.  Source ownership recovery is an
        explicit mutation performed by backfill/sync/outbox consumers under
        the workspace governance lock, never by a read path.

        The read goes through ``_read_conn()`` so it reuses an in-flight
        write transaction on this thread instead of opening a second
        connection (a second connection would self-lock against the active
        ``BEGIN IMMEDIATE``).
        """
        with self._read_conn() as conn:
            return self._get_source_link_conn(conn, share_group_id, memory_id)

    def list_source_links(
        self,
        *,
        share_group_id: str | None = None,
        status: str | None = None,
        canonical_definition_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pure-read listing of persisted source links.

        Source links are the fact table for Source -> canonical Definition
        ownership.  Evidence only carries belief, never ownership.  Reads via
        ``_read_conn()`` so a caller inside a write transaction never opens a
        second connection against the same database file.
        """
        sql = "SELECT * FROM rule_source_links WHERE 1=1"
        params: list[Any] = []
        if share_group_id is not None:
            sql += " AND share_group_id=?"
            params.append(share_group_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if canonical_definition_id is not None:
            sql += " AND canonical_definition_id=?"
            params.append(canonical_definition_id)
        sql += " ORDER BY share_group_id, memory_id"
        with self._read_conn() as conn:
            return [
                dict(row)
                for row in conn.execute(sql, params).fetchall()
            ]

    # ------------------------------------------------------------------
    # Runtime feedback / definition statistics (P2 -> P3 projection, PR4)
    # ------------------------------------------------------------------

    def upsert_runtime_feedback(
        self,
        *,
        feedback_id: str,
        definition_id: str,
        receipt_id: str = "",
        outcome: str,
        agent_instance_id: str = "",
        project_ref: str = "",
        session_id: str = "",
        source: str = "",
        authority: int = 0,
        session_trusted: int = 0,
        created_at: str = "",
    ) -> None:
        """Idempotently record one projected feedback event (feedback_id PK)."""
        with self._write_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO rule_runtime_feedback (
                    feedback_id, definition_id, receipt_id, outcome,
                    agent_instance_id, project_ref, session_id, source,
                    authority, session_trusted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (feedback_id, definition_id, receipt_id or "", outcome or "",
                 agent_instance_id or "", project_ref or "", session_id or "",
                 source or "", int(authority or 0), int(bool(session_trusted)),
                 created_at or _now()),
            )

    def delete_runtime_feedback(self, feedback_id: str) -> None:
        if not feedback_id:
            return
        with self._write_conn() as conn:
            conn.execute(
                "DELETE FROM rule_runtime_feedback WHERE feedback_id=?",
                (feedback_id,),
            )

    @staticmethod
    def _runtime_rows_for_definition(
        conn: sqlite3.Connection, definition_id: str,
    ) -> list[sqlite3.Row]:
        """Read runtime stats from the effective receipt projection.

        Direct runtime inserts remain supported for older callers, but once a
        definition has projection rows, the projection is authoritative: raw
        superseded feedback must not inflate counters or trusted-session
        diversity.
        """
        projection_exists = conn.execute(
            "SELECT 1 FROM rule_effective_feedback_projection "
            "WHERE definition_id=? LIMIT 1",
            (definition_id,),
        ).fetchone()
        if projection_exists is not None:
            return conn.execute(
                """
                SELECT r.*
                FROM rule_effective_feedback_projection p
                JOIN rule_runtime_feedback r
                  ON r.feedback_id=p.effective_feedback_id
                WHERE p.definition_id=?
                  AND p.effective_feedback_id<>''
                  AND p.outcome NOT IN ('ignored', 'unobserved')
                  AND r.source <> 'ambiguous_migration'
                ORDER BY r.created_at, r.feedback_id
                """,
                (definition_id,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM rule_runtime_feedback WHERE definition_id=? "
            "AND source <> 'ambiguous_migration'",
            (definition_id,),
        ).fetchall()

    def recompute_runtime_stats(self, definition_id: str) -> dict[str, Any]:
        """Recompute a definition's runtime counters from the feedback ledger.

        Counters are derived (never incremented) so the projection is idempotent
        even if an outbox event is re-delivered after a partial failure.
        """
        with self._write_conn() as conn:
            rows = self._runtime_rows_for_definition(conn, definition_id)
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
                for r in rows
                if (r["session_id"] or "").strip()
                and (r["agent_instance_id"] or "").strip()
                and (r["project_ref"] or "").strip()
                and int(r["session_trusted"] or 0) == 1
            }
            projects = {
                str(r["project_ref"] or "")
                for r in rows
                if (r["project_ref"] or "").strip()
                and (r["agent_instance_id"] or "").strip()
                and (r["session_id"] or "").strip()
                and int(r["session_trusted"] or 0) == 1
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

    def list_runtime_feedback(self, definition_id: str) -> list[dict[str, Any]]:
        """Return effective runtime observations for readiness/maturity policy."""
        with self._db() as conn:
            return [
                dict(row)
                for row in self._runtime_rows_for_definition(conn, definition_id)
            ]

    def get_runtime_stats(self, definition_id: str) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_definition_runtime_stats "
                "WHERE definition_id=?",
                (definition_id,),
            ).fetchone()
            runtime_rows = self._runtime_rows_for_definition(conn, definition_id)
        if row is None:
            return None
        trusted_rows = [
            item for item in runtime_rows
            if (item["session_id"] or "").strip()
            and (item["agent_instance_id"] or "").strip()
            and (item["project_ref"] or "").strip()
            and int(item["session_trusted"] or 0) == 1
        ]
        return {
            "definition_id": row["definition_id"],
            "followed": int(row["followed"] or 0),
            "violated": int(row["violated"] or 0),
            "not_applicable": int(row["not_applicable"] or 0),
            "exception_count": int(row["exception_count"] or 0),
            "distinct_sessions": int(row["distinct_sessions"] or 0),
            "distinct_projects": int(row["distinct_projects"] or 0),
            "last_observed_at": row["last_observed_at"] or "",
            "trusted_followed": sum(
                1 for item in trusted_rows if item["outcome"] == "followed"
            ),
            "trusted_total": len(trusted_rows),
            "trusted_sessions": len({
                str(item["session_id"] or "") for item in trusted_rows
            }),
            "trusted_agents": len({
                str(item["agent_instance_id"] or "") for item in trusted_rows
            }),
            "trusted_projects": len({
                str(item["project_ref"] or "") for item in trusted_rows
            }),
        }

    # ------------------------------------------------------------------
    # Bindings
    # ------------------------------------------------------------------

    @staticmethod
    def _upsert_binding_conn(
        conn: sqlite3.Connection, binding: RuleBinding,
    ) -> None:
        payload = binding.to_dict()
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

    def upsert_binding(self, binding: RuleBinding) -> RuleBinding:
        """Upsert binding and record an explicit manual contribution.

        Public direct binding writes must not create an active binding with no
        source contribution. Source-owned callers use the contribution APIs;
        direct callers are represented by a deterministic manual source.
        """
        with self._write_conn() as conn:
            self._upsert_binding_conn(conn, binding)
            payload = self._contribution_payload(
                binding,
                share_group_id=binding.share_group_id,
                source_memory_id=f"manual:{binding.binding_id}",
                source_revision="manual",
            )
            self._upsert_contribution_payload_conn(conn, payload)
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
    def _contribution_payload(
        item: RuleBinding | dict[str, Any],
        *,
        share_group_id: str,
        source_memory_id: str,
        source_revision: str,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = item if isinstance(item, dict) else {}
        raw_binding = metadata.get("binding", item)
        binding = (
            raw_binding
            if isinstance(raw_binding, RuleBinding)
            else RuleBinding.from_dict(raw_binding)
        )
        if binding.share_group_id != share_group_id:
            raise ValueError("contribution binding share_group_id does not match source")
        assignment_hash = str(
            metadata.get("legacy_assignment_hash", "") or ""
        )
        if not assignment_hash:
            assignment_hash = stable_hash(
                "rule-binding-assignment",
                binding.definition_id,
                binding.binding_id,
                json.dumps(binding.audience_identity(), ensure_ascii=False),
            )
        now = _now()
        return {
            "contribution_id": stable_hash(
                "rule-binding-contribution",
                share_group_id,
                source_memory_id,
                assignment_hash,
            ),
            "share_group_id": share_group_id,
            "source_memory_id": source_memory_id,
            "source_revision": str(metadata.get("source_revision", source_revision) or ""),
            "legacy_assignment_hash": assignment_hash,
            "definition_id": binding.definition_id,
            "binding_id": binding.binding_id,
            "target_type": binding.target_type,
            "target_id": binding.target_id,
            "project_ref": binding.project_ref,
            "provider": binding.provider,
            "runtime_role": binding.runtime_role,
            "effect": binding.effect,
            "priority": binding.priority,
            "owner_agent_id": binding.owner_agent_id,
            "audience": json.dumps(
                {
                    "share_group_id": binding.share_group_id,
                    "target_type": binding.target_type,
                    "target_id": binding.target_id,
                    "project_ref": binding.project_ref,
                    "provider": binding.provider,
                    "runtime_role": binding.runtime_role,
                    "effect": binding.effect,
                    "priority": binding.priority,
                    "owner_agent_id": binding.owner_agent_id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            "binding": binding,
            "created_at": binding.created_at or now,
            "updated_at": now,
        }

    @staticmethod
    def _row_to_contribution(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["active"] = bool(result.get("active"))
        try:
            result["audience"] = json.loads(result.get("audience") or "{}")
        except (TypeError, json.JSONDecodeError):
            result["audience"] = {}
        return result

    @staticmethod
    def _upsert_contribution_payload_conn(
        conn: sqlite3.Connection, payload: dict[str, Any],
    ) -> None:
        existing = conn.execute(
            "SELECT owner_agent_id FROM rule_binding_contributions "
            "WHERE share_group_id=? AND source_memory_id=? "
            "AND legacy_assignment_hash=?",
            (
                payload["share_group_id"], payload["source_memory_id"],
                payload["legacy_assignment_hash"],
            ),
        ).fetchone()
        if (
            existing is not None
            and str(existing["owner_agent_id"] or "")
            != str(payload["owner_agent_id"] or "")
        ):
            raise ValueError("source_contribution_owner_mismatch")
        conn.execute(
            """
            INSERT INTO rule_binding_contributions (
                contribution_id, share_group_id, source_memory_id,
                source_revision, legacy_assignment_hash, definition_id,
                binding_id, target_type, target_id, project_ref, provider,
                runtime_role, effect, priority, owner_agent_id, audience,
                active, status, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      1, 'active', 1, ?, ?)
            ON CONFLICT(share_group_id, source_memory_id,
                        legacy_assignment_hash) DO UPDATE SET
                contribution_id=excluded.contribution_id,
                source_revision=excluded.source_revision,
                definition_id=excluded.definition_id,
                binding_id=excluded.binding_id,
                target_type=excluded.target_type,
                target_id=excluded.target_id,
                project_ref=excluded.project_ref,
                provider=excluded.provider,
                runtime_role=excluded.runtime_role,
                effect=excluded.effect,
                priority=excluded.priority,
                owner_agent_id=excluded.owner_agent_id,
                audience=excluded.audience,
                active=1,
                status='active',
                revision=rule_binding_contributions.revision+1,
                updated_at=excluded.updated_at
            WHERE rule_binding_contributions.owner_agent_id
                  = excluded.owner_agent_id
            """,
            (
                payload["contribution_id"], payload["share_group_id"],
                payload["source_memory_id"], payload["source_revision"],
                payload["legacy_assignment_hash"], payload["definition_id"],
                payload["binding_id"], payload["target_type"],
                payload["target_id"], payload["project_ref"],
                payload["provider"], payload["runtime_role"],
                payload["effect"], payload["priority"],
                payload["owner_agent_id"], payload["audience"],
                payload["created_at"], payload["updated_at"],
            ),
        )

    def _upsert_source_contributions_conn(
        self,
        conn: sqlite3.Connection,
        payloads: list[dict[str, Any]],
        *,
        replace: bool,
        share_group_id: str,
        source_memory_id: str,
        owner_agent_id: str | None = None,
    ) -> list[RuleBinding]:
        payload_owners = {
            str(payload["owner_agent_id"] or "") for payload in payloads
        }
        if owner_agent_id is not None:
            owner = str(owner_agent_id or "")
            if payload_owners and payload_owners != {owner}:
                raise ValueError("source_contribution_owner_mismatch")
        elif len(payload_owners) > 1:
            raise ValueError("source_owner_required")
        elif payload_owners:
            owner = next(iter(payload_owners))
        else:
            owner_rows = conn.execute(
                "SELECT DISTINCT owner_agent_id "
                "FROM rule_binding_contributions "
                "WHERE share_group_id=? AND source_memory_id=? AND active=1",
                (share_group_id, source_memory_id),
            ).fetchall()
            owners = {str(row["owner_agent_id"] or "") for row in owner_rows}
            if len(owners) > 1:
                raise ValueError("source_owner_required")
            owner = next(iter(owners), "")
        old_rows = conn.execute(
            "SELECT binding_id FROM rule_binding_contributions "
            "WHERE share_group_id=? AND source_memory_id=? "
            "AND owner_agent_id=?",
            (share_group_id, source_memory_id, owner),
        ).fetchall()
        affected = {str(row["binding_id"]) for row in old_rows}
        affected.update(str(payload["binding_id"]) for payload in payloads)
        for payload in payloads:
            self._upsert_binding_conn(conn, payload["binding"])
            self._upsert_contribution_payload_conn(conn, payload)
        if replace:
            keep = {str(payload["legacy_assignment_hash"]) for payload in payloads}
            if keep:
                predicate = "legacy_assignment_hash NOT IN ({})".format(
                    ",".join("?" for _ in keep)
                )
            else:
                predicate = "1=1"
            stale = conn.execute(
                "SELECT contribution_id, binding_id FROM "
                "rule_binding_contributions WHERE share_group_id=? "
                "AND source_memory_id=? AND owner_agent_id=? AND " + predicate,
                [share_group_id, source_memory_id, owner, *keep]
                if keep else [share_group_id, source_memory_id, owner],
            ).fetchall()
            for row in stale:
                affected.add(str(row["binding_id"]))
                conn.execute(
                    "UPDATE rule_binding_contributions SET active=0, "
                    "status='revoked', revision=revision+1, updated_at=? "
                    "WHERE contribution_id=? AND owner_agent_id=?",
                    (_now(), row["contribution_id"], owner),
                )
        return self._materialize_affected_bindings_conn(conn, affected)

    def upsert_source_contributions(
        self,
        share_group_id: str,
        source_memory_id: str,
        contributions: Iterable[RuleBinding | dict[str, Any]],
        *,
        source_revision: str = "",
        owner_agent_id: str | None = None,
    ) -> list[RuleBinding]:
        """Upsert one source's contributions and materialize affected bindings."""
        payloads = [
            self._contribution_payload(
                item,
                share_group_id=share_group_id,
                source_memory_id=source_memory_id,
                source_revision=source_revision,
            )
            for item in contributions
        ]
        with self._write_conn() as conn:
            return self._upsert_source_contributions_conn(
                conn, payloads, replace=False,
                share_group_id=share_group_id,
                source_memory_id=source_memory_id,
                owner_agent_id=owner_agent_id,
            )

    def replace_source_contributions(
        self,
        share_group_id: str,
        source_memory_id: str,
        contributions: Iterable[RuleBinding | dict[str, Any]],
        *,
        source_revision: str = "",
        owner_agent_id: str | None = None,
    ) -> list[RuleBinding]:
        """Replace exactly one source; never touch another source's rows."""
        items = list(contributions)
        payloads = [
            self._contribution_payload(
                item,
                share_group_id=share_group_id,
                source_memory_id=source_memory_id,
                source_revision=source_revision,
            )
            for item in items
        ]
        with self._write_conn() as conn:
            return self._upsert_source_contributions_conn(
                conn, payloads, replace=True,
                share_group_id=share_group_id,
                source_memory_id=source_memory_id,
                owner_agent_id=owner_agent_id,
            )

    def deactivate_source_contributions(
        self,
        share_group_id: str,
        source_memory_id: str,
        *,
        owner_agent_id: str | None = None,
    ) -> list[RuleBinding]:
        """Deactivate one source and materialize only its affected bindings."""
        with self._write_conn() as conn:
            if owner_agent_id is None:
                owner_rows = conn.execute(
                    "SELECT DISTINCT owner_agent_id "
                    "FROM rule_binding_contributions "
                    "WHERE share_group_id=? AND source_memory_id=? AND active=1",
                    (share_group_id, source_memory_id),
                ).fetchall()
                owners = {
                    str(row["owner_agent_id"] or "") for row in owner_rows
                }
                if len(owners) > 1:
                    raise ValueError("source_owner_required")
                owner_agent_id = next(iter(owners), "")
            rows = conn.execute(
                "SELECT binding_id FROM rule_binding_contributions "
                "WHERE share_group_id=? AND source_memory_id=? "
                "AND owner_agent_id=? AND active=1",
                (share_group_id, source_memory_id, owner_agent_id or ""),
            ).fetchall()
            affected = {str(row["binding_id"]) for row in rows}
            if affected:
                conn.execute(
                    "UPDATE rule_binding_contributions SET active=0, status='revoked', "
                    "revision=revision+1, updated_at=? "
                    "WHERE share_group_id=? AND source_memory_id=? "
                    "AND owner_agent_id=? AND active=1",
                    (_now(), share_group_id, source_memory_id, owner_agent_id or ""),
                )
            return self._materialize_affected_bindings_conn(conn, affected)

    def list_binding_contributions(
        self,
        *,
        share_group_id: str | None = None,
        source_memory_id: str | None = None,
        binding_id: str | None = None,
        active: bool | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rule_binding_contributions WHERE 1=1"
        params: list[Any] = []
        for column, value in (
            ("share_group_id", share_group_id),
            ("source_memory_id", source_memory_id),
            ("binding_id", binding_id),
        ):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        if active is not None:
            sql += " AND active=?"
            params.append(int(active))
        sql += " ORDER BY share_group_id, source_memory_id, legacy_assignment_hash"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_contribution(row) for row in rows]

    def materialize_affected_bindings(
        self,
        binding_ids: Iterable[str],
    ) -> list[RuleBinding]:
        """Active iff at least one active contribution remains."""
        ids = tuple(dict.fromkeys(str(binding_id) for binding_id in binding_ids))
        if not ids:
            return []
        with self._write_conn() as conn:
            return self._materialize_affected_bindings_conn(conn, ids)

    @classmethod
    def _materialize_affected_bindings_conn(
        cls,
        conn: sqlite3.Connection,
        binding_ids: Iterable[str],
    ) -> list[RuleBinding]:
        ids = tuple(dict.fromkeys(str(binding_id) for binding_id in binding_ids))
        if not ids:
            return []
        now = _now()
        for binding_id in ids:
            row = conn.execute(
                "SELECT status FROM rule_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if row is None:
                continue
            active = conn.execute(
                "SELECT 1 FROM rule_binding_contributions "
                "WHERE binding_id=? AND active=1 AND status='active' LIMIT 1",
                (binding_id,),
            ).fetchone() is not None
            status = "active" if active else "revoked"
            if row["status"] != status:
                conn.execute(
                    "UPDATE rule_bindings SET status=?, revision=revision+1, "
                    "updated_at=? WHERE binding_id=?",
                    (status, now, binding_id),
                )
        rows = conn.execute(
            "SELECT * FROM rule_bindings WHERE binding_id IN ({})".format(
                ",".join("?" for _ in ids)
            ),
            ids,
        ).fetchall()
        return [cls._row_to_binding(row) for row in rows]

    @staticmethod
    def _binding_contribution_diff_conn(conn: sqlite3.Connection) -> int:
        materialized = {
            (str(row["binding_id"]), str(row["definition_id"]))
            for row in conn.execute(
                "SELECT binding_id, definition_id FROM rule_bindings "
                "WHERE status='active'"
            ).fetchall()
        }
        contributed = {
            (str(row["binding_id"]), str(row["definition_id"]))
            for row in conn.execute(
                "SELECT binding_id, definition_id "
                "FROM rule_binding_contributions "
                "WHERE active=1 AND status='active'"
            ).fetchall()
        }
        return len(materialized.symmetric_difference(contributed))

    @staticmethod
    def _binding_audience_multiset_conn(
        conn: sqlite3.Connection,
        definition_id: str,
        *,
        status: str = "active",
    ) -> Counter[tuple[Any, ...]]:
        rows = conn.execute(
            "SELECT * FROM rule_bindings WHERE definition_id=? AND status=?",
            (definition_id, status),
        ).fetchall()
        return Counter(
            RuleMergeStore._row_to_binding(row).audience_identity()
            for row in rows
        )

    @staticmethod
    def _binding_contribution_multiset_conn(
        conn: sqlite3.Connection,
        definition_id: str,
    ) -> Counter[tuple[Any, ...]]:
        rows = conn.execute(
            "SELECT * FROM rule_binding_contributions WHERE definition_id=?",
            (definition_id,),
        ).fetchall()
        return Counter(
            (
                str(row["share_group_id"] or ""),
                str(row["source_memory_id"] or ""),
                str(row["source_revision"] or ""),
                str(row["legacy_assignment_hash"] or ""),
                str(row["target_type"] or ""),
                str(row["target_id"] or ""),
                str(row["project_ref"] or ""),
                str(row["provider"] or ""),
                str(row["runtime_role"] or ""),
                str(row["effect"] or "include"),
                int(row["priority"] or 0),
                str(row["owner_agent_id"] or ""),
                str(row["audience"] or "{}"),
                int(row["active"] or 0),
                str(row["status"] or ""),
            )
            for row in rows
        )

    def _rehome_binding_contributions_conn(
        self,
        conn: sqlite3.Connection,
        old_definition_id: str,
        new_definition_id: str,
    ) -> list[RuleBinding]:
        """Rehome bindings/contributions inside caller's transaction.

        Binding identity is regenerated from the destination Definition and
        unchanged audience fields.  Contributions are the durable source of
        truth: they move first, then old materialized bindings are revoked by
        the normal materializer.  No binding definition column is mutated.
        """
        rows = conn.execute(
            """
            SELECT DISTINCT b.*
            FROM rule_binding_contributions c
            JOIN rule_bindings b ON b.binding_id=c.binding_id
            WHERE c.definition_id=? AND b.definition_id=?
            ORDER BY b.binding_id
            """,
            (old_definition_id, old_definition_id),
        ).fetchall()
        mapping: dict[str, RuleBinding] = {}
        for row in rows:
            old = self._row_to_binding(row)
            new = build_binding(
                new_definition_id,
                share_group_id=old.share_group_id,
                target_type=old.target_type,
                target_id=old.target_id,
                project_ref=old.project_ref,
                provider=old.provider,
                runtime_role=old.runtime_role,
                effect=old.effect,
                priority=old.priority,
                owner_agent_id=old.owner_agent_id,
                created_by=old.created_by,
                authorization=old.authorization,
                created_at=old.created_at,
            )
            self._upsert_binding_conn(conn, new)
            mapping[old.binding_id] = new

        affected = set(mapping)
        affected.update(binding.binding_id for binding in mapping.values())
        now = _now()
        for old_id, new in mapping.items():
            payload = new.to_dict()
            audience = json.dumps(
                {
                    key: payload[key]
                    for key in (
                        "share_group_id", "target_type", "target_id",
                        "project_ref", "provider", "runtime_role", "effect",
                        "priority", "owner_agent_id",
                    )
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            conn.execute(
                """
                UPDATE rule_binding_contributions SET
                    definition_id=?, binding_id=?, target_type=?, target_id=?,
                    project_ref=?, provider=?, runtime_role=?, effect=?,
                    priority=?, owner_agent_id=?, audience=?, revision=revision+1,
                    updated_at=? WHERE binding_id=? AND definition_id=?
                """,
                (
                    payload["definition_id"], payload["binding_id"],
                    payload["target_type"], payload["target_id"],
                    payload["project_ref"], payload["provider"],
                    payload["runtime_role"], payload["effect"],
                    payload["priority"], payload["owner_agent_id"], audience,
                    now, old_id, old_definition_id,
                ),
            )

        conn.execute(
            "UPDATE rule_source_links SET canonical_definition_id=?, "
            "updated_at=? WHERE canonical_definition_id=?",
            (new_definition_id, now, old_definition_id),
        )
        return self._materialize_affected_bindings_conn(conn, affected)

    def rehome_binding_contributions(
        self,
        old_definition_id: str,
        new_definition_id: str,
    ) -> list[RuleBinding]:
        """Move definition ownership while preserving every source row."""
        if old_definition_id == new_definition_id:
            return self.list_bindings(definition_id=new_definition_id, status=None)
        with self._write_conn() as conn:
            return self._rehome_binding_contributions_conn(
                conn, old_definition_id, new_definition_id,
            )

    def revoke_stale_source_bindings(
        self,
        definition_id: str,
        share_group_id: str,
        owner_agent_id: str,
        desired_identity_keys: set[str],
        source_memory_id: str | None = None,
    ) -> int:
        """Legacy compatibility shim; owner-only revocation is fail-closed."""
        if not source_memory_id:
            raise ValueError(
                "source_memory_id is required; use source-scoped contributions"
            )
        with self._write_conn() as conn:
            rows = conn.execute(
                "SELECT c.*, b.* FROM rule_binding_contributions c "
                "JOIN rule_bindings b ON b.binding_id=c.binding_id "
                "WHERE c.definition_id=? AND c.share_group_id=? "
                "AND c.source_memory_id=? AND c.owner_agent_id=? "
                "AND b.owner_agent_id=? AND c.active=1",
                (
                    definition_id, share_group_id, source_memory_id,
                    owner_agent_id, owner_agent_id,
                ),
            ).fetchall()
            revoked = 0
            affected: set[str] = set()
            for row in rows:
                binding = RuleBinding(
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
                if binding_identity_key(binding) in desired_identity_keys:
                    continue
                conn.execute(
                    "UPDATE rule_binding_contributions SET active=0, status='revoked', "
                    "revision=revision+1, updated_at=? "
                    "WHERE contribution_id=? AND owner_agent_id=?",
                    (_now(), row["contribution_id"], owner_agent_id),
                )
                affected.add(str(row["binding_id"]))
                revoked += 1
            self._materialize_affected_bindings_conn(conn, affected)
        return revoked

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
    # Evidence contribution ledger
    # ------------------------------------------------------------------

    @staticmethod
    def _compat_evidence_key(
        conn: sqlite3.Connection,
        definition_id: str,
        independence_key: str,
        polarity: str,
    ) -> str:
        """Choose a legacy compatibility key without colliding by polarity."""
        base = str(independence_key or "")
        table = "rule_evidence" if polarity == "positive" else "rule_negative_evidence"
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE definition_id=? "
            "AND independence_key=? AND active=1 LIMIT 1",
            (definition_id, base),
        ).fetchone()
        return base if row is None else stable_hash(
            "ledger-compat-independence", base, polarity,
        )

    def _materialize_evidence_compat_conn(
        self, conn: sqlite3.Connection, definition_ids: Iterable[str],
    ) -> None:
        """Materialize ledger winners into legacy evidence tables.

        ``rule_evidence`` and ``rule_negative_evidence`` are compatibility
        projections only. Inactive rows remain for audit/history; readers see
        active winners and runner-up contributions stay solely in the ledger.
        """
        for definition_id in sorted({str(item) for item in definition_ids if str(item)}):
            conn.execute(
                "UPDATE rule_evidence SET active=0 WHERE definition_id=?",
                (definition_id,),
            )
            conn.execute(
                "UPDATE rule_negative_evidence SET active=0 WHERE definition_id=?",
                (definition_id,),
            )
            effective_rows = list_effective(conn, definition_id=definition_id)
            for effective in effective_rows:
                row = conn.execute(
                    "SELECT * FROM rule_evidence_contributions "
                    "WHERE contribution_id=?",
                    (effective.winner_contribution_id,),
                ).fetchone()
                if row is None:
                    continue
                source_ids = row["source_ids"] or "{}"
                try:
                    source_ids = json.loads(source_ids)
                except (TypeError, json.JSONDecodeError):
                    source_ids = {}
                content_hash = str(source_ids.get("content_hash") or "")
                semantic_hash = str(source_ids.get("semantic_hash") or "")
                provider = str(source_ids.get("provider") or "")
                source_root_id = str(
                    row["source_root_id"] or source_ids.get("source_root_id") or ""
                )
                source_object_id = str(
                    row["source_object_id"] or source_ids.get("source_object_id") or ""
                )
                source_evidence_id = str(row["source_evidence_id"] or "")
                evidence_id = source_evidence_id or stable_hash(
                    "ledger-compat-evidence", row["contribution_id"],
                )
                independence_key = self._compat_evidence_key(
                    conn, definition_id, str(row["independence_key"] or ""),
                    str(row["polarity"] or "positive"),
                )
                if str(row["polarity"] or "positive") == "positive":
                    conn.execute(
                        """
                        INSERT INTO rule_evidence (
                            evidence_id, definition_id, source_rule_id,
                            agent_instance_id, project_ref, provider, session_id,
                            receipt_id, content_hash, semantic_hash, confidence,
                            observed_at, independence_key, share_group_id,
                            source_root_id, source_object_id, session_trusted,
                            feedback_id, feedback_authority, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, 1)
                        ON CONFLICT(evidence_id) DO UPDATE SET
                            definition_id=excluded.definition_id,
                            source_rule_id=excluded.source_rule_id,
                            agent_instance_id=excluded.agent_instance_id,
                            project_ref=excluded.project_ref,
                            provider=excluded.provider,
                            session_id=excluded.session_id,
                            receipt_id=excluded.receipt_id,
                            content_hash=excluded.content_hash,
                            semantic_hash=excluded.semantic_hash,
                            confidence=excluded.confidence,
                            observed_at=excluded.observed_at,
                            independence_key=excluded.independence_key,
                            share_group_id=excluded.share_group_id,
                            source_root_id=excluded.source_root_id,
                            source_object_id=excluded.source_object_id,
                            session_trusted=excluded.session_trusted,
                            feedback_id=excluded.feedback_id,
                            feedback_authority=excluded.feedback_authority,
                            active=1
                        """,
                        (
                            evidence_id, definition_id,
                            row["source_rule_id"] or source_ids.get("source_rule_id", ""),
                            row["agent_instance_id"] or "",
                            row["project_ref"] or "", provider,
                            row["session_id"] or "", row["receipt_id"] or "",
                            content_hash, semantic_hash, row["confidence"],
                            row["observed_at"] or "", independence_key,
                            row["share_group_id"] or "",
                            source_root_id, source_object_id,
                            int(row["session_trusted"] or 0),
                            row["feedback_id"] or "", int(row["authority"] or 0),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO rule_negative_evidence (
                            evidence_id, definition_id, source_rule_id,
                            agent_instance_id, project_ref, content_hash,
                            confidence, observed_at, independence_key,
                            share_group_id, session_id, receipt_id, feedback_id,
                            feedback_authority, source_root_id, source_object_id,
                            session_trusted, active
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, 1)
                        ON CONFLICT(evidence_id) DO UPDATE SET
                            definition_id=excluded.definition_id,
                            source_rule_id=excluded.source_rule_id,
                            agent_instance_id=excluded.agent_instance_id,
                            project_ref=excluded.project_ref,
                            content_hash=excluded.content_hash,
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
                            session_trusted=excluded.session_trusted,
                            active=1
                        """,
                        (
                            evidence_id, definition_id,
                            row["source_rule_id"] or source_ids.get("source_rule_id", ""),
                            row["agent_instance_id"] or "",
                            row["project_ref"] or "", content_hash, row["confidence"],
                            row["observed_at"] or "", independence_key,
                            row["share_group_id"] or "", row["session_id"] or "",
                            row["receipt_id"] or "", row["feedback_id"] or "",
                            int(row["authority"] or 0), source_root_id,
                            source_object_id, int(row["session_trusted"] or 0),
                        ),
                    )

    def upsert_evidence_contribution(
        self, contribution: EvidenceContribution,
    ) -> EvidenceContribution:
        """Write one append-only contribution and rebuild its winner."""
        with self._write_conn() as conn:
            item = upsert_contribution(conn, contribution)
            if item.share_group_id and item.source_memory_id:
                # Feedback can arrive through the public low-level receipt API
                # before lifecycle sync has written its source link.  Once the
                # contribution is accepted, persist ownership in the same
                # transaction; never replace original source identity.
                now = _now()
                conn.execute(
                    """
                    INSERT INTO rule_source_links (
                        share_group_id, memory_id, source_revision,
                        original_definition_id, canonical_definition_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, '', ?, ?, 'active', ?, ?)
                    ON CONFLICT(share_group_id, memory_id) DO UPDATE SET
                        canonical_definition_id=excluded.canonical_definition_id,
                        status='active', updated_at=excluded.updated_at
                    """,
                    (
                        item.share_group_id, item.source_memory_id,
                        item.definition_id, item.definition_id, now, now,
                    ),
                )
            rebuild_effective(conn, definition_id=item.definition_id)
            self._materialize_evidence_compat_conn(conn, [item.definition_id])
        return item

    def deactivate_evidence_contributions_for_receipt(
        self, receipt_id: str,
    ) -> set[str]:
        """Deactivate only one receipt, then restore its runner-up winners."""
        if not receipt_id:
            return set()
        with self._write_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT definition_id FROM rule_evidence_contributions "
                "WHERE receipt_id=? AND active=1",
                (receipt_id,),
            ).fetchall()
            definitions = {str(row["definition_id"]) for row in rows}
            feedback_ids = [
                str(row["feedback_id"] or "")
                for row in conn.execute(
                    "SELECT feedback_id FROM rule_evidence_contributions "
                    "WHERE receipt_id=? AND feedback_id<>''",
                    (receipt_id,),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE rule_evidence_contributions SET active=0, updated_at=? "
                "WHERE receipt_id=? AND active=1",
                (_now(), receipt_id),
            )
            for definition_id in definitions:
                rebuild_effective(conn, definition_id=definition_id)
            self._materialize_evidence_compat_conn(conn, definitions)
            if feedback_ids:
                placeholders = ",".join("?" for _ in feedback_ids)
                conn.execute(
                    "DELETE FROM rule_runtime_feedback WHERE feedback_id IN (" +
                    placeholders + ")",
                    feedback_ids,
                )
        return definitions

    @staticmethod
    def _source_evidence_owner_conn(
        conn: sqlite3.Connection,
        source_rule_id: str,
        owner_agent_id: str | None,
    ) -> str | None:
        if owner_agent_id is not None:
            return str(owner_agent_id or "")
        rows = conn.execute(
            """
            SELECT DISTINCT agent_instance_id FROM rule_evidence
            WHERE source_rule_id=? AND active=1
            UNION SELECT DISTINCT agent_instance_id FROM rule_negative_evidence
            WHERE source_rule_id=? AND active=1
            UNION SELECT DISTINCT agent_instance_id
            FROM rule_evidence_contributions
            WHERE source_rule_id=? AND active=1
            """,
            (source_rule_id, source_rule_id, source_rule_id),
        ).fetchall()
        owners = {str(row["agent_instance_id"] or "") for row in rows}
        if len(owners) > 1:
            raise ValueError("source_owner_required")
        return next(iter(owners), None)

    def deactivate_source_evidence(
        self,
        source_rule_id: str,
        owner_agent_id: str | None = None,
    ) -> set[str]:
        """Stop all evidence/runtime artifacts owned by one source record."""
        if not source_rule_id:
            return set()
        with self._write_conn() as conn:
            owner_agent_id = self._source_evidence_owner_conn(
                conn, source_rule_id, owner_agent_id,
            )
            if owner_agent_id is None:
                return set()
            definitions = {
                str(row["definition_id"])
                for row in conn.execute(
                    "SELECT DISTINCT definition_id FROM rule_evidence "
                    "WHERE source_rule_id=? AND agent_instance_id=? AND active=1 "
                    "UNION SELECT DISTINCT definition_id FROM "
                    "rule_negative_evidence WHERE source_rule_id=? "
                    "AND agent_instance_id=? AND active=1 "
                    "UNION SELECT DISTINCT definition_id FROM "
                    "rule_evidence_contributions WHERE source_rule_id=? "
                    "AND agent_instance_id=? AND active=1",
                    (
                        source_rule_id, owner_agent_id,
                        source_rule_id, owner_agent_id,
                        source_rule_id, owner_agent_id,
                    ),
                ).fetchall()
            }
            feedback_ids = {
                str(row["feedback_id"] or "")
                for row in conn.execute(
                    "SELECT feedback_id FROM rule_evidence_contributions "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND feedback_id<>''",
                    (source_rule_id, owner_agent_id),
                ).fetchall()
            }
            receipt_ids = {
                str(row["receipt_id"] or "")
                for row in conn.execute(
                    "SELECT receipt_id FROM rule_evidence_contributions "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND receipt_id<>'' "
                    "UNION SELECT receipt_id FROM rule_evidence "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND receipt_id<>'' "
                    "UNION SELECT receipt_id FROM rule_negative_evidence "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND receipt_id<>''",
                    (
                        source_rule_id, owner_agent_id,
                        source_rule_id, owner_agent_id,
                        source_rule_id, owner_agent_id,
                    ),
                ).fetchall()
            }
            feedback_ids.update(
                str(row["feedback_id"] or "")
                for row in conn.execute(
                    "SELECT feedback_id FROM rule_evidence "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND feedback_id<>'' "
                    "UNION SELECT feedback_id FROM rule_negative_evidence "
                    "WHERE source_rule_id=? AND agent_instance_id=? "
                    "AND feedback_id<>''",
                    (
                        source_rule_id, owner_agent_id,
                        source_rule_id, owner_agent_id,
                    ),
                ).fetchall()
            )
            conn.execute(
                "UPDATE rule_evidence SET active=0 "
                "WHERE source_rule_id=? AND agent_instance_id=?",
                (source_rule_id, owner_agent_id),
            )
            conn.execute(
                "UPDATE rule_negative_evidence SET active=0 "
                "WHERE source_rule_id=? AND agent_instance_id=?",
                (source_rule_id, owner_agent_id),
            )
            conn.execute(
                "UPDATE rule_evidence_contributions SET active=0, updated_at=? "
                "WHERE source_rule_id=? AND agent_instance_id=? AND active=1",
                (_now(), source_rule_id, owner_agent_id),
            )
            for definition_id in definitions:
                rebuild_effective(conn, definition_id=definition_id)
            self._materialize_evidence_compat_conn(conn, definitions)
            ids = sorted(item for item in feedback_ids if item)
            receipts = sorted(item for item in receipt_ids if item)
            if ids or receipts:
                clauses: list[str] = []
                params: list[str] = []
                if ids:
                    clauses.append(
                        "feedback_id IN (" + ",".join("?" for _ in ids) + ")"
                    )
                    params.extend(ids)
                if receipts:
                    clauses.append(
                        "receipt_id IN (" + ",".join("?" for _ in receipts) + ")"
                    )
                    params.extend(receipts)
                conn.execute(
                    "DELETE FROM rule_runtime_feedback WHERE "
                    + " OR ".join(clauses),
                    params,
                )
                conn.execute(
                    "UPDATE rule_effective_feedback_projection SET "
                    "effective_feedback_id='', outcome='tombstone' "
                    "WHERE receipt_id IN (" + ",".join("?" for _ in receipts) + ")",
                    receipts,
                ) if receipts else None
            for definition_id in definitions:
                self._recompute_runtime_stats_conn(conn, definition_id)
        return definitions

    # ------------------------------------------------------------------
    # Evidence
    # ------------------------------------------------------------------

    @staticmethod
    def _public_evidence_contribution(
        evidence: Any,
        *,
        polarity: str,
    ) -> EvidenceContribution:
        payload = evidence.to_dict()
        evidence_id = str(payload.get("evidence_id") or "")
        definition_id = str(payload.get("definition_id") or "")
        independence_key = str(payload.get("independence_key") or "")
        if not independence_key:
            independence_key = stable_hash(
                "rule-evidence-legacy-independence",
                str(payload.get("project_ref") or ""),
                str(payload.get("agent_instance_id") or ""),
                str(payload.get("source_root_id") or ""),
                str(
                    payload.get("source_object_id")
                    or payload.get("session_id")
                    or ""
                ),
                str(payload.get("content_hash") or ""),
            )
        source_rule_id = str(payload.get("source_rule_id") or "")
        receipt_id = str(payload.get("receipt_id") or "")
        feedback_id = str(payload.get("feedback_id") or "")
        source_ids = {
            "evidence_id": evidence_id,
            "source_rule_id": source_rule_id,
            "receipt_id": receipt_id,
            "feedback_id": feedback_id,
            "content_hash": str(payload.get("content_hash") or ""),
            "semantic_hash": str(payload.get("semantic_hash") or ""),
            "provider": str(payload.get("provider") or ""),
            "source_root_id": str(payload.get("source_root_id") or ""),
            "source_object_id": str(payload.get("source_object_id") or ""),
        }
        return build_contribution(
            contribution_id=stable_hash(
                "rule-evidence-contribution", polarity, evidence_id,
            ),
            definition_id=definition_id,
            independence_key=independence_key,
            kind="evidence",
            polarity=polarity,
            authority=int(payload.get("feedback_authority") or 0),
            confidence=(
                float(payload["confidence"])
                if payload.get("confidence") is not None else 1.0
            ),
            observed_at=str(payload.get("observed_at") or ""),
            active=True,
            receipt_id=receipt_id,
            feedback_id=feedback_id,
            source_rule_id=source_rule_id,
            source_evidence_id=evidence_id,
            source_memory_id=source_rule_id or evidence_id,
            source_ids=source_ids,
            agent_instance_id=str(payload.get("agent_instance_id") or ""),
            project_ref=str(payload.get("project_ref") or ""),
            share_group_id=str(payload.get("share_group_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            source_root_id=str(payload.get("source_root_id") or ""),
            source_object_id=str(payload.get("source_object_id") or ""),
            session_trusted=bool(payload.get("session_trusted")),
        )

    def upsert_evidence(self, evidence: RuleEvidence) -> RuleEvidence:
        contribution = self._public_evidence_contribution(
            evidence, polarity="positive",
        )
        with self._write_conn() as conn:
            upsert_contribution(conn, contribution)
            rebuild_effective(conn, definition_id=contribution.definition_id)
            self._materialize_evidence_compat_conn(
                conn, [contribution.definition_id],
            )
        return evidence

    @staticmethod
    def _evidence_payload_wins(
        payload: dict[str, Any], existing: sqlite3.Row,
    ) -> bool:
        """Choose one row for an independence key deterministically."""
        candidate = (
            int(payload.get("feedback_authority") or 0),
            float(
                payload["confidence"]
                if payload.get("confidence") is not None else 1.0
            ),
            str(payload.get("observed_at") or ""),
            str(payload.get("evidence_id") or ""),
        )
        current = (
            int(existing["feedback_authority"] or 0),
            float(
                existing["confidence"]
                if existing["confidence"] is not None else 1.0
            ),
            str(existing["observed_at"] or ""),
            str(existing["evidence_id"] or ""),
        )
        return candidate > current

    def list_evidence(
        self, definition_id: str | None = None,
    ) -> list[RuleEvidence]:
        sql = "SELECT * FROM rule_evidence WHERE active=1"
        params: list[Any] = []
        if definition_id:
            sql += " AND definition_id=?"
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
            confidence=float(
                row["confidence"] if row["confidence"] is not None else 1.0
            ),
            observed_at=row["observed_at"] or "",
            independence_key=row["independence_key"] or "",
            share_group_id=row["share_group_id"] or "",
            source_root_id=row["source_root_id"] or "",
            source_object_id=row["source_object_id"] or "",
            session_trusted=bool(int(row["session_trusted"] or 0)),
            feedback_id=row["feedback_id"] or "",
            feedback_authority=int(row["feedback_authority"] or 0),
        )

    def count_evidence(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_evidence WHERE active=1"
            ).fetchone()
        return int(row["c"])

    # ------------------------------------------------------------------
    # Negative evidence (P3-001 §5)
    # ------------------------------------------------------------------

    def upsert_negative_evidence(
        self, evidence: Any,
    ) -> Any:
        contribution = self._public_evidence_contribution(
            evidence, polarity="negative",
        )
        with self._write_conn() as conn:
            upsert_contribution(conn, contribution)
            rebuild_effective(conn, definition_id=contribution.definition_id)
            self._materialize_evidence_compat_conn(
                conn, [contribution.definition_id],
            )
        return evidence

    def list_negative_evidence(
        self, definition_id: str | None = None,
    ) -> list[Any]:
        from .rule_evidence import NegativeEvidence

        sql = "SELECT * FROM rule_negative_evidence WHERE active=1"
        params: list[Any] = []
        if definition_id:
            sql += " AND definition_id=?"
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
                confidence=float(
                    row["confidence"] if row["confidence"] is not None else 1.0
                ),
                observed_at=row["observed_at"] or "",
                independence_key=row["independence_key"] or "",
                share_group_id=row["share_group_id"] or "",
                session_id=row["session_id"] or "",
                receipt_id=row["receipt_id"] or "",
                feedback_id=row["feedback_id"] or "",
                feedback_authority=int(row["feedback_authority"] or 0),
                source_root_id=row["source_root_id"] or "",
                source_object_id=row["source_object_id"] or "",
                session_trusted=bool(int(row["session_trusted"] or 0)),
            )
            for row in rows
        ]

    def count_negative_evidence(self) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM rule_negative_evidence WHERE active=1"
            ).fetchone()
        return int(row["c"])

    def delete_evidence(self, evidence_id: str) -> None:
        if not evidence_id:
            return
        with self._write_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT definition_id FROM rule_evidence_contributions "
                "WHERE source_evidence_id=? AND polarity='positive'",
                (evidence_id,),
            ).fetchall()
            definitions = {str(row["definition_id"] or "") for row in rows}
            conn.execute(
                "UPDATE rule_evidence_contributions SET active=0, updated_at=? "
                "WHERE source_evidence_id=? AND polarity='positive'",
                (_now(), evidence_id),
            )
            conn.execute(
                "UPDATE rule_evidence SET active=0 WHERE evidence_id=?",
                (evidence_id,),
            )
            for definition_id in definitions:
                rebuild_effective(conn, definition_id=definition_id)
            self._materialize_evidence_compat_conn(conn, definitions)

    def delete_negative_evidence(self, evidence_id: str) -> None:
        if not evidence_id:
            return
        with self._write_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT definition_id FROM rule_evidence_contributions "
                "WHERE source_evidence_id=? AND polarity='negative'",
                (evidence_id,),
            ).fetchall()
            definitions = {str(row["definition_id"] or "") for row in rows}
            conn.execute(
                "UPDATE rule_evidence_contributions SET active=0, updated_at=? "
                "WHERE source_evidence_id=? AND polarity='negative'",
                (_now(), evidence_id),
            )
            conn.execute(
                "UPDATE rule_negative_evidence SET active=0 WHERE evidence_id=?",
                (evidence_id,),
            )
            for definition_id in definitions:
                rebuild_effective(conn, definition_id=definition_id)
            self._materialize_evidence_compat_conn(conn, definitions)

    def get_effective_feedback_projection(
        self, receipt_id: str,
    ) -> dict[str, Any] | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM rule_effective_feedback_projection "
                "WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def upsert_effective_feedback_projection(
        self,
        *,
        receipt_id: str,
        effective_feedback_id: str = "",
        definition_id: str = "",
        outcome: str = "",
        positive_evidence_id: str = "",
        negative_evidence_id: str = "",
        session_id: str = "",
        session_trusted: int = 0,
        session_source: str = "absent",
    ) -> None:
        trusted_flag = session_trusted is True or (
            isinstance(session_trusted, (int, float)) and session_trusted == 1
        )
        trusted = session_trust_is_valid(
            session_id, session_source, trusted_flag
        )
        with self._write_conn() as conn:
            conn.execute(
                """
                INSERT INTO rule_effective_feedback_projection (
                    receipt_id, effective_feedback_id, definition_id, outcome,
                    positive_evidence_id, negative_evidence_id,
                    session_trusted, session_source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO UPDATE SET
                    effective_feedback_id=excluded.effective_feedback_id,
                    definition_id=excluded.definition_id,
                    outcome=excluded.outcome,
                    positive_evidence_id=excluded.positive_evidence_id,
                    negative_evidence_id=excluded.negative_evidence_id,
                    session_trusted=excluded.session_trusted,
                    session_source=excluded.session_source,
                    updated_at=excluded.updated_at
                """,
                (
                    receipt_id, effective_feedback_id or "", definition_id or "",
                    outcome or "", positive_evidence_id or "",
                    negative_evidence_id or "", int(trusted),
                    session_source or "absent", _now(),
                ),
            )

    def clear_evidence_projection(self, receipt_id: str) -> None:
        # Never delete receipt history. Deactivate this receipt's ledger rows,
        # rebuild effective winners, and leave compatibility rows as inactive
        # audit materialization.
        self.deactivate_evidence_contributions_for_receipt(receipt_id)

    def set_projection_state(
        self,
        scope_id: str,
        *,
        last_outbox_event_id: str = "",
        last_projected_event_id: str = "",
        projection_lag: int = 0,
        projection_error: str = "",
    ) -> None:
        with self._write_conn() as conn:
            conn.execute(
                """
                INSERT INTO rule_projection_state (
                    scope_id, last_outbox_event_id, last_projected_event_id,
                    projection_lag, projection_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    last_outbox_event_id=CASE
                        WHEN excluded.last_outbox_event_id = ''
                        THEN rule_projection_state.last_outbox_event_id
                        ELSE excluded.last_outbox_event_id
                    END,
                    last_projected_event_id=CASE
                        WHEN excluded.last_projected_event_id = ''
                        THEN rule_projection_state.last_projected_event_id
                        ELSE excluded.last_projected_event_id
                    END,
                    projection_lag=excluded.projection_lag,
                    projection_error=excluded.projection_error,
                    updated_at=excluded.updated_at
                """,
                (
                    scope_id, last_outbox_event_id or "",
                    last_projected_event_id or "", max(0, int(projection_lag)),
                    projection_error or "", _now(),
                ),
            )

    @staticmethod
    def _normalize_group_ids(
        group_ids: Iterable[str] | None,
    ) -> set[str] | None:
        if group_ids is None:
            return None
        return {
            str(group_id).strip()
            for group_id in group_ids
            if str(group_id).strip()
        }

    @staticmethod
    def _projection_scope_selected(
        scope_id: str,
        selected_groups: set[str] | None,
    ) -> bool:
        if selected_groups is None:
            return True
        # Legacy databases used one global scope before per-group checkpoints
        # existed.  Preserve its fail-closed meaning only when no concrete
        # group is attached to the current definitions.
        return scope_id in selected_groups or (
            not selected_groups and scope_id == "rule-intelligence"
        )

    def _groups_for_definitions_conn(
        self,
        conn: sqlite3.Connection,
        definition_ids: Iterable[str],
    ) -> set[str]:
        ids = sorted({str(definition_id) for definition_id in definition_ids if str(definition_id)})
        if not ids:
            return set()
        placeholders = ",".join("?" for _ in ids)
        groups: set[str] = set()
        queries = (
            (
                "SELECT share_group_id FROM rule_bindings "
                f"WHERE status='active' AND definition_id IN ({placeholders})",
                ids,
            ),
            (
                "SELECT share_group_id FROM rule_binding_contributions "
                f"WHERE active=1 AND definition_id IN ({placeholders})",
                ids,
            ),
            (
                "SELECT share_group_id FROM rule_source_links "
                f"WHERE status='active' AND (canonical_definition_id IN ({placeholders}) "
                f"OR original_definition_id IN ({placeholders}))",
                ids + ids,
            ),
            (
                "SELECT share_group_id FROM rule_evidence "
                f"WHERE active=1 AND definition_id IN ({placeholders})",
                ids,
            ),
            (
                "SELECT share_group_id FROM rule_negative_evidence "
                f"WHERE active=1 AND definition_id IN ({placeholders})",
                ids,
            ),
        )
        for sql, params in queries:
            groups.update(
                str(row["share_group_id"] or "")
                for row in conn.execute(sql, params).fetchall()
                if str(row["share_group_id"] or "")
            )
        return groups

    def groups_for_definitions(
        self, definition_ids: Iterable[str],
    ) -> set[str]:
        """Return groups materially connected to these definitions."""
        with self._db() as conn:
            return self._groups_for_definitions_conn(conn, definition_ids)

    def projection_status(
        self, group_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        selected_groups = self._normalize_group_ids(group_ids)
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_projection_state ORDER BY scope_id"
            ).fetchall()
        if selected_groups is not None:
            rows = [
                row for row in rows
                if self._projection_scope_selected(
                    str(row["scope_id"] or ""), selected_groups,
                )
            ]
        return {
            "scopes": [dict(row) for row in rows],
            "projection_lag": sum(int(row["projection_lag"] or 0) for row in rows),
            "projection_error": next(
                (str(row["projection_error"] or "") for row in rows
                 if row["projection_error"]),
                "",
            ),
        }

    def projection_ready(
        self, group_ids: Iterable[str] | None = None,
    ) -> bool:
        status = self.projection_status(group_ids=group_ids)
        return status["projection_lag"] == 0 and not status["projection_error"]

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
        with self._write_conn() as conn:
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
        with self._write_conn() as conn:
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
        with self._write_conn() as conn:
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, 1, ?, ?, ?, ?, '')
                    """,
                    (
                        payload["definition_id"], payload["canonical_text"],
                        payload["normalized_intent"], payload["rule_kind"],
                        payload["polarity"], payload["semantic_hash"],
                        payload["parameter_schema"], payload["confidence"],
                        payload["rule_strength"],
                        payload["maturity_state"], payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                before_audiences = self._binding_audience_multiset_conn(
                    conn, old_definition_id,
                )
                before_contributions = self._binding_contribution_multiset_conn(
                    conn, old_definition_id,
                )
                self._rehome_binding_contributions_conn(
                    conn, old_definition_id, new_definition.definition_id,
                )
                after_audiences = self._binding_audience_multiset_conn(
                    conn, new_definition.definition_id,
                )
                after_contributions = self._binding_contribution_multiset_conn(
                    conn, new_definition.definition_id,
                )
                if before_audiences != after_audiences:
                    raise RuntimeError("rule_evolution_scope_change")
                if before_contributions != after_contributions:
                    raise RuntimeError("rule_evolution_contribution_change")
                if self._binding_contribution_diff_conn(conn) != 0:
                    raise RuntimeError("rule_evolution_binding_contribution_drift")
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
            except Exception:
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

    def _binding_digest(
        self, definition_ids: Iterable[str],
        *, conn: sqlite3.Connection | None = None,
    ) -> str:
        ids = sorted({str(item) for item in definition_ids})
        owns = conn is None
        connection = conn or self._db()
        try:
            rows: list[dict[str, Any]] = []
            for definition_id in ids:
                for row in connection.execute(
                    "SELECT * FROM rule_bindings "
                    "WHERE definition_id=? AND status='active' "
                    "ORDER BY binding_id",
                    (definition_id,),
                ).fetchall():
                    rows.append(self._row_to_binding(row).to_dict())
            return stable_hash(
                "rule-proposal-bindings",
                json.dumps(rows, ensure_ascii=False, sort_keys=True),
            )
        finally:
            if owns:
                connection.close()

    def _projection_watermark(
        self,
        conn: sqlite3.Connection,
        group_ids: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Capture P3 state plus relevant legacy outbox high-water marks."""
        selected_groups = self._normalize_group_ids(group_ids)
        state = [
            {
                "scope_id": row["scope_id"],
                "last_outbox_event_id": row["last_outbox_event_id"] or "",
                "last_projected_event_id": row["last_projected_event_id"] or "",
            }
            for row in conn.execute(
                "SELECT * FROM rule_projection_state ORDER BY scope_id"
            ).fetchall()
            if (
                (
                    selected_groups is None
                    or self._projection_scope_selected(
                        str(row["scope_id"] or ""), selected_groups,
                    )
                )
                and (
                    str(row["last_outbox_event_id"] or "")
                    or str(row["last_projected_event_id"] or "")
                )
            )
        ]
        legacy_state: list[dict[str, Any]] = []
        for group_id, db_path in iter_legacy_groups(self.workspace):
            if selected_groups is not None and group_id not in selected_groups:
                continue
            legacy_conn = sqlite3.connect(str(db_path), timeout=2.0)
            try:
                row = legacy_conn.execute(
                    "SELECT COUNT(*) AS total, COALESCE(MAX(rowid), 0) AS max_rowid, "
                    "COALESCE(MAX(created_at), '') AS max_created_at "
                    "FROM rule_event_outbox"
                ).fetchone()
                legacy_state.append({
                    "group_id": group_id,
                    "total": int(row[0] or 0),
                    "max_rowid": int(row[1] or 0),
                    "max_created_at": str(row[2] or ""),
                })
            except sqlite3.Error:
                legacy_state.append({
                    "group_id": group_id,
                    "total": -1,
                    "max_rowid": -1,
                    "max_created_at": "error",
                })
            finally:
                legacy_conn.close()
        return [{"p3": state}, {"legacy": legacy_state}]

    def _runtime_digest(
        self, definition_ids: Iterable[str],
        *, conn: sqlite3.Connection | None = None,
    ) -> str:
        ids = sorted({str(item) for item in definition_ids})
        owns = conn is None
        connection = conn or self._db()
        try:
            group_ids = self._groups_for_definitions_conn(connection, ids)
            rows = []
            for definition_id in ids:
                rows.extend(
                    dict(row) for row in connection.execute(
                        "SELECT * FROM rule_runtime_feedback "
                        "WHERE definition_id=? ORDER BY feedback_id",
                        (definition_id,),
                    ).fetchall()
                )
            return stable_hash(
                "rule-proposal-runtime",
                json.dumps(
                    {
                        "runtime": rows,
                        "projection_watermark": self._projection_watermark(
                            connection, group_ids,
                        ),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
        finally:
            if owns:
                connection.close()

    def _evidence_digest_from_ids(
        self,
        evidence_ids: Iterable[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        """Digest evidence ids *and payload* for TOCTOU protection."""
        ids = sorted({str(item) for item in evidence_ids if str(item)})
        owns = conn is None
        connection = conn or self._db()
        try:
            rows: list[dict[str, Any]] = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = [
                    dict(row) for row in connection.execute(
                        f"SELECT * FROM rule_evidence "
                        f"WHERE evidence_id IN ({placeholders}) "
                        "ORDER BY evidence_id",
                        ids,
                    ).fetchall()
                ]
            return stable_hash(
                "rule-proposal-evidence",
                json.dumps(
                    {"requested_ids": ids, "rows": rows},
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
            )
        finally:
            if owns:
                connection.close()

    def _negative_digest(
        self,
        definition_ids: Iterable[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        ids = sorted({str(item) for item in definition_ids if str(item)})
        owns = conn is None
        connection = conn or self._db()
        try:
            rows: list[dict[str, Any]] = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = [
                    dict(row) for row in connection.execute(
                        f"SELECT * FROM rule_negative_evidence "
                        f"WHERE definition_id IN ({placeholders}) "
                        "ORDER BY evidence_id",
                        ids,
                    ).fetchall()
                ]
            return stable_hash(
                "rule-proposal-negative-evidence",
                json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str),
            )
        finally:
            if owns:
                connection.close()

    def create_proposal(
        self,
        definition_ids: list[str],
        similarity_score: float,
        *,
        evidence: list[RuleEvidence] | tuple[RuleEvidence, ...] | None = None,
        contradiction_score: float = 0.0,
        explanation: str = "",
        readiness_score: float = 0.0,
        readiness_components: dict[str, Any] | str | None = None,
        readiness_digest: str = "",
        readiness_snapshot: dict[str, Any] | None = None,
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
        if definition_a is not None and definition_b is not None:
            # The caller's score is only a hint.  Persist the score derived
            # from the semantic definitions that the transaction will later
            # re-read, so a hand-built proposal cannot smuggle in a score.
            similarity_score = compute_layers(
                definition_a, definition_b,
            ).duplicate_score
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
        evidence_digest = self._evidence_digest_from_ids(
            e.evidence_id for e in (evidence or [])
        )
        negative_digest = self._negative_digest(sorted_ids)
        binding_digest = self._binding_digest(sorted_ids)
        runtime_digest = self._runtime_digest(sorted_ids)
        snapshot = readiness_snapshot or {}
        if readiness_components is None and isinstance(snapshot, dict):
            readiness_components = snapshot.get("components")
        if not readiness_digest and isinstance(snapshot, dict):
            readiness_digest = str(snapshot.get("digest") or "")
        readiness_components_text = (
            readiness_components
            if isinstance(readiness_components, str)
            else json.dumps(
                dict(readiness_components or {}),
                ensure_ascii=False, sort_keys=True,
            )
        )
        revision_a = int(getattr(definition_a, "revision", 0) or 0) if definition_a else 0
        revision_b = int(getattr(definition_b, "revision", 0) or 0) if definition_b else 0
        now = _now()
        with self._write_conn() as conn:
            # Direct Store callers may not have gone through the Service's
            # governance projection.  Still persist the same canonical
            # readiness snapshot that execute_merge will recompute.
            if not readiness_digest and len(sorted_ids) == 2:
                current_rows = {
                    definition_id: conn.execute(
                        "SELECT * FROM rule_definitions WHERE definition_id=?",
                        (definition_id,),
                    ).fetchone()
                    for definition_id in sorted_ids
                }
                if all(current_rows.values()):
                    computed = self._conn_readiness_snapshot(conn, current_rows)
                    readiness_score = float(computed["score"])
                    readiness_components_text = json.dumps(
                        computed.get("components") or {},
                        ensure_ascii=False, sort_keys=True,
                    )
                    readiness_digest = str(computed.get("digest") or "")
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
                        readiness_components=?, readiness_digest=?,
                        governance_reasons=?, cooldown_until=?, negative_score=?,
                        conflict_type=?, judge_source=?, judge_model=?,
                        judge_score=?, judge_confidence=?, judge_recommendation=?,
                        judge_rationale=?, explanation=?, candidate_since=?,
                        last_evaluated_at=?, assessment_revision=?,
                        definition_revision_a=?, definition_revision_b=?,
                        evidence_digest=?, negative_digest=?, binding_digest=?, runtime_digest=?,
                        policy_version=?, weight_breakdown=?
                    WHERE proposal_id=?
                    """,
                    (
                        float(similarity_score), len(evidence_list),
                        len(agents), len(projects),
                        float(contradiction_score), float(readiness_score),
                        readiness_components_text, readiness_digest or "",
                        governance_reasons or "",
                        (
                            existing["cooldown_until"]
                            if existing["cooldown_until"] is not None
                            else cooldown_until or ""
                        ),
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
                        revision_a, revision_b, evidence_digest, negative_digest,
                        binding_digest, runtime_digest,
                        MERGE_POLICY_VERSION, weight_breakdown or "",
                        proposal_id,
                    ),
                )
                refreshed = conn.execute(
                    "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchone()
                return self._row_to_proposal(refreshed)
            conn.execute(
                """
                INSERT INTO rule_merge_proposals (
                    proposal_id, definition_ids, similarity_score,
                    evidence_count, agent_count, project_count,
                    contradiction_score, readiness_score,
                    readiness_components, readiness_digest, governance_reasons,
                    cooldown_until, first_merge_acknowledged, negative_score,
                    conflict_type, judge_source, judge_model, judge_score,
                    judge_confidence, judge_recommendation, judge_rationale,
                    status, explanation, created_at, candidate_since,
                    last_evaluated_at, assessment_revision, definition_revision_a,
                    definition_revision_b, evidence_digest, negative_digest,
                    binding_digest, runtime_digest, policy_version, weight_breakdown
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    proposal_id,
                    json.dumps(sorted_ids, ensure_ascii=False),
                    float(similarity_score), len(evidence_list),
                    len(agents), len(projects),
                    float(contradiction_score), float(readiness_score),
                    readiness_components_text, readiness_digest or "",
                    governance_reasons or "", cooldown_until or "",
                    0, float(negative_score), conflict_type or "",
                    self._judge_field(judge, "source"),
                    self._judge_field(judge, "model"),
                    self._judge_score(judge),
                    self._judge_field(judge, "confidence"),
                    self._judge_field(judge, "recommendation"),
                    self._judge_field(judge, "rationale"),
                    "candidate", explanation, now, now, now, 1,
                    revision_a, revision_b, evidence_digest, negative_digest,
                    binding_digest, runtime_digest, MERGE_POLICY_VERSION,
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
        with self._write_conn() as conn:
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
            if status == "approved" and current != "candidate":
                raise ValueError("rule_merge_proposal_not_approvable")
            if status == "approved":
                # Status mutation is not approval.  Only approve_proposal()
                # may create the capability-backed approval row and advance
                # the proposal to ``approved``.
                raise ValueError("rule_merge_approval_required")
            conn.execute(
                "UPDATE rule_merge_proposals SET status=? WHERE proposal_id=?",
                (status, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def approve_proposal(
        self,
        proposal_id: str,
        *,
        approved_by: str = "",
        capability_id: str = "",
        capability_token: str = "",
        expected_definition_revisions: dict[str, int] | None = None,
        approval_scope: str = "merge",
        expires_at: str = "",
        access_context: AccessContext | None = None,
    ) -> dict[str, Any]:
        """First-class approval: records who approved what, then approves.

        ``merge_proposal(actor='admin')`` is no longer an approval by itself —
        an ``rule_merge_approvals`` row must exist for the human path to run.
        The recorded expected definition revisions are re-verified inside the
        merge transaction so a definition edited after approval cannot merge.
        """
        token = capability_token or capability_id
        if capability_token and capability_id and capability_token != capability_id:
            raise ValueError("rule_merge_approval_capability_mismatch")
        if not token:
            raise ValueError("rule_merge_approval_capability_required")
        if access_context is None:
            raise ValueError("trusted AccessContext required")
        principal = self._trusted_principal(access_context)
        if approved_by and approved_by != principal:
            raise ValueError("rule_merge_approval_principal_mismatch")
        approved_by = approved_by or principal
        if approval_scope != "merge":
            raise ValueError("rule_merge_approval_scope_invalid")
        with self._write_conn() as conn:
            row = conn.execute(
                "SELECT * FROM rule_merge_proposals WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ValueError("rule_merge_proposal_not_found")
            if str(row["status"] or "") != "candidate":
                raise ValueError("rule_merge_proposal_not_approvable")
            definition_ids = json.loads(row["definition_ids"] or "[]")
            if len(definition_ids) != 2:
                raise ValueError("rule_merge_proposal_must_pair_two_definitions")
            definition_rows = {
                str(definition_id): conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (str(definition_id),),
                ).fetchone()
                for definition_id in definition_ids
            }
            if any(item is None for item in definition_rows.values()):
                raise ValueError("rule_merge_definition_not_found")
            definitions = [
                self._row_to_definition(definition_rows[str(definition_id)])
                for definition_id in definition_ids
            ]
            layers = compute_layers(definitions[0], definitions[1])
            if layers.duplicate_score < HUMAN_MERGE_MIN_SIMILARITY:
                raise ValueError("rule_merge_similarity_gate_failed")
            current_revisions = {
                str(definition_id): int(
                    definition_rows[str(definition_id)]["revision"] or 0
                )
                for definition_id in definition_ids
            }
            proposal_revisions = {
                str(definition_ids[0]): int(row["definition_revision_a"] or 0),
                str(definition_ids[1]): int(row["definition_revision_b"] or 0),
            }
            if not all(proposal_revisions.values()):
                proposal_revisions = current_revisions
                conn.execute(
                    "UPDATE rule_merge_proposals SET definition_revision_a=?, "
                    "definition_revision_b=?, assessment_revision=assessment_revision+1 "
                    "WHERE proposal_id=?",
                    (
                        proposal_revisions[str(definition_ids[0])],
                        proposal_revisions[str(definition_ids[1])], proposal_id,
                    ),
                )
            if proposal_revisions != current_revisions:
                raise RuntimeError("rule_merge_definition_revision_drift")
            if expected_definition_revisions is None:
                expected_definition_revisions = current_revisions
            expected_definition_revisions = {
                str(key): int(value)
                for key, value in expected_definition_revisions.items()
            }
            if expected_definition_revisions != current_revisions:
                raise RuntimeError("rule_merge_definition_revision_drift")
            record = consume_capability_record(
                conn, token, principal=principal, proposal_id=proposal_id,
            )
            approval_id = stable_hash(
                "rule-merge-approval", proposal_id, record.token_hash,
            )
            now = _now()
            expected = json.dumps(
                expected_definition_revisions,
                ensure_ascii=False, sort_keys=True,
            )
            conn.execute(
                """
                INSERT INTO rule_merge_approvals (
                    approval_id, proposal_id, approved_by, capability_id,
                    expected_definition_revisions, approval_scope,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id, proposal_id, approved_by, record.token_hash,
                    expected, "merge", now, self._capability_expiry_text(record),
                ),
            )
            updated = conn.execute(
                "UPDATE rule_merge_proposals SET status='approved', "
                "first_merge_acknowledged=1, cooldown_until='' "
                "WHERE proposal_id=? AND status='candidate'",
                (proposal_id,),
            )
            if updated.rowcount != 1:
                raise ValueError("rule_merge_proposal_not_approvable")
            return {
                "approval_id": approval_id, "proposal_id": proposal_id,
                "approved_by": approved_by, "capability_id": record.token_hash,
                "expected_definition_revisions": dict(expected_definition_revisions),
                "approval_scope": "merge", "created_at": now,
                "expires_at": self._capability_expiry_text(record),
            }

    def get_valid_approval(self, proposal_id: str) -> dict[str, Any] | None:
        """The latest non-expired approval for a proposal, if any."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM rule_merge_approvals WHERE proposal_id=? "
                "ORDER BY created_at DESC",
                (proposal_id,),
            ).fetchall()
            for row in rows:
                capability_id = str(row["capability_id"] or "")
                capability = conn.execute(
                    "SELECT * FROM governance_capabilities "
                    "WHERE token_hash=? AND proposal_id=? AND scope=? "
                    "AND consumed=1",
                    (capability_id, proposal_id, "rule_merge_approve"),
                ).fetchone()
                if capability is None or float(
                    capability["expires_at"] or 0
                ) <= time.time():
                    continue
                expires_at = str(row["expires_at"] or "")
                return {
                    "approval_id": row["approval_id"],
                    "proposal_id": row["proposal_id"],
                    "approved_by": row["approved_by"] or "",
                    "capability_id": capability_id,
                    "principal": capability["principal"] or "",
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
        readiness_components: dict[str, Any] | str | None = None,
        readiness_digest: str = "",
        governance_reasons: str = "",
        cooldown_until: str = "",
        negative_score: float = 0.0,
        conflict_type: str = "",
        judge: Any | None = None,
    ) -> dict[str, Any] | None:
        """Persist the governance snapshot of one merge proposal."""
        with self._write_conn() as conn:
            conn.execute(
                """
                UPDATE rule_merge_proposals SET
                    readiness_score=?, readiness_components=?, readiness_digest=?,
                    governance_reasons=?, cooldown_until=?,
                    negative_score=?, conflict_type=?, judge_source=?,
                    judge_model=?, judge_score=?, judge_confidence=?,
                    judge_recommendation=?, judge_rationale=?
                WHERE proposal_id=?
                """,
                (
                 float(readiness_score),
                 readiness_components if isinstance(readiness_components, str)
                 else json.dumps(dict(readiness_components or {}), ensure_ascii=False, sort_keys=True),
                 readiness_digest or "",
                 governance_reasons or "", cooldown_until or "",
                 float(negative_score), conflict_type or "",
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
        self,
        proposal_id: str,
        actor: str = "human",
        *,
        capability_token: str = "",
        capability_id: str = "",
        access_context: AccessContext | None = None,
    ) -> dict[str, Any] | None:
        """Record explicit human acknowledgment of the first-merge risk.

        The very first merge involving a pair of definitions is the highest-risk
        operation in the layer (no rollback history, no error pattern).  It must
        not happen on an Agent's say-so alone: ``merge_proposal(actor='auto')``
        refuses until this acknowledgment exists.
        """
        token = capability_token or capability_id
        if not token or access_context is None:
            raise ValueError("rule_merge_approval_capability_required")
        principal = self._trusted_principal(access_context)
        with self._write_conn() as conn:
            consume_capability_record(
                conn, token, principal=principal, proposal_id=proposal_id,
            )
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

    def clear_proposal_cooldown(
        self,
        proposal_id: str,
        *,
        capability_token: str = "",
        capability_id: str = "",
        access_context: AccessContext | None = None,
    ) -> dict[str, Any] | None:
        """Clear the 72h cooldown after human review of a merge proposal."""
        # Kept as a public compatibility spelling, but it is now a governed
        # mutation and cannot bypass capability consumption.
        token = capability_token or capability_id
        if not token or access_context is None:
            raise ValueError("rule_merge_approval_capability_required")
        principal = self._trusted_principal(access_context)
        with self._write_conn() as conn:
            consume_capability_record(
                conn, token, principal=principal, proposal_id=proposal_id,
            )
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
    def _recompute_runtime_stats_conn(
        conn: sqlite3.Connection, definition_id: str,
    ) -> dict[str, Any]:
        rows = RuleMergeStore._runtime_rows_for_definition(conn, definition_id)
        followed = sum(1 for row in rows if row["outcome"] == "followed")
        violated = sum(1 for row in rows if row["outcome"] == "violated")
        not_applicable = sum(
            1 for row in rows if row["outcome"] == "not_applicable"
        )
        exception_count = sum(1 for row in rows if row["outcome"] == "exception")
        sessions = {
            str(row["session_id"] or "")
            for row in rows
            if (row["session_id"] or "").strip()
            and (row["agent_instance_id"] or "").strip()
            and (row["project_ref"] or "").strip()
            and int(row["session_trusted"] or 0) == 1
        }
        projects = {
            str(row["project_ref"] or "")
            for row in rows
            if (row["project_ref"] or "").strip()
            and (row["agent_instance_id"] or "").strip()
            and (row["session_id"] or "").strip()
            and int(row["session_trusted"] or 0) == 1
        }
        last_observed = max(
            (str(row["created_at"] or "") for row in rows), default="",
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
            (
                definition_id, followed, violated, not_applicable,
                exception_count, len(sessions), len(projects), last_observed,
            ),
        )
        return stats

    def _conn_projection_ready(
        self,
        conn: sqlite3.Connection,
        group_ids: Iterable[str] | None = None,
    ) -> bool:
        selected_groups = self._normalize_group_ids(group_ids)
        rows = conn.execute("SELECT * FROM rule_projection_state").fetchall()
        if selected_groups is not None:
            rows = [
                row for row in rows
                if self._projection_scope_selected(
                    str(row["scope_id"] or ""), selected_groups,
                )
            ]
        if not (
            sum(int(row["projection_lag"] or 0) for row in rows) == 0
            and not any(str(row["projection_error"] or "") for row in rows)
        ):
            return False
        for group_id, db_path in iter_legacy_groups(self.workspace):
            if selected_groups is not None and group_id not in selected_groups:
                continue
            legacy_conn = sqlite3.connect(str(db_path), timeout=2.0)
            try:
                pending = legacy_conn.execute(
                    "SELECT COUNT(*) FROM rule_event_outbox WHERE consumed_at=''"
                ).fetchone()
                if int(pending[0] or 0) > 0:
                    return False
            except sqlite3.Error:
                return False
            finally:
                legacy_conn.close()
        return True

    def _auto_maturity_gate(
        self,
        definition_rows: dict[str, sqlite3.Row],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        ordered = list(definition_rows.values())
        if len(ordered) != 2:
            return False
        definitions = [self._row_to_definition(row) for row in ordered]
        layers = compute_layers(definitions[0], definitions[1])
        if (
            layers.hash_score >= 1.0
            or layers.intent_score >= INTENTION_MATCH_THRESHOLD
        ):
            required = {"validated", "trusted"}
        else:
            required = {"trusted"}
        owns = conn is None
        connection = conn or self._db()
        try:
            for definition in definitions:
                runtime_rows = self._runtime_rows_for_definition(
                    connection, definition.definition_id,
                )
                snapshot = build_maturity_snapshot(
                    runtime={"events": [dict(row) for row in runtime_rows]},
                )
                if snapshot["state"] not in required:
                    return False
        finally:
            if owns:
                connection.close()
        return True

    def _conn_readiness_snapshot(
        self,
        conn: sqlite3.Connection,
        definition_rows: dict[str, sqlite3.Row],
        *,
        duplicate_score: float | None = None,
    ) -> dict[str, Any]:
        """Build the canonical readiness snapshot from current DB rows."""
        definitions = [
            self._row_to_definition(definition_rows[key])
            for key in sorted(definition_rows)
        ]
        evidence_rows = [
            row
            for definition in definitions
            for row in conn.execute(
                "SELECT * FROM rule_evidence WHERE definition_id=? AND active=1",
                (definition.definition_id,),
            ).fetchall()
            if self._evidence_row_is_eligible(row)
        ]
        evidence_rows = self._dedupe_conn_rows(evidence_rows)
        evidence_input = [
            self._row_to_evidence(row).to_dict() for row in evidence_rows
        ]
        runtime_rows = [
            dict(row)
            for definition in definitions
            for row in self._runtime_rows_for_definition(
                conn, definition.definition_id,
            )
        ]
        runtime = {
            "events": runtime_rows,
            "trusted_followed": sum(
                1 for row in runtime_rows
                if row.get("outcome") == "followed"
                and int(row.get("session_trusted") or 0) == 1
                and str(row.get("session_id") or "").strip()
                and str(row.get("agent_instance_id") or "").strip()
                and str(row.get("project_ref") or "").strip()
            ),
        }
        maturity = build_maturity_snapshot(runtime=runtime)
        runtime["trusted_total"] = maturity["trusted_total"]
        runtime["trusted_sessions"] = maturity["trusted_sessions"]
        runtime["trusted_agents"] = maturity["trusted_agents"]
        runtime["trusted_projects"] = maturity["trusted_projects"]
        reputation = [{
            "agent_id": row["agent_id"],
            "success_rate": float(row["success_rate"] or 0.0),
            "rule_accuracy": float(row["rule_accuracy"] or 0.0),
            "violation_rate": float(row["violation_rate"] or 0.0),
            "sample_count": int(row["sample_count"] or 0),
            "feedback_quality": float(row["feedback_quality"] or 0.0),
        } for row in conn.execute(
            "SELECT * FROM agent_reputation ORDER BY agent_id"
        ).fetchall()]
        project = [{
            "project_ref": row["project_ref"],
            "production_level": float(row["production_level"] or 0.0),
            "criticality": float(row["criticality"] or 0.0),
            "owner_verified": bool(row["owner_verified"]),
        } for row in conn.execute(
            "SELECT * FROM project_profile ORDER BY project_ref"
        ).fetchall()]
        layers = compute_layers(definitions[0], definitions[1])
        # Shared helper owns score/digest arithmetic.  Normalize the age input
        # at day precision so repeated proposal/execute snapshots do not drift
        # only because wall-clock advanced by a few microseconds.
        definition_input = []
        for item in definitions:
            payload = item.to_dict()
            created_at = str(payload.get("created_at") or "")
            if len(created_at) > 10:
                try:
                    parsed_created_at = datetime.fromisoformat(
                        created_at.replace("Z", "+00:00"),
                    )
                    if parsed_created_at.tzinfo is None:
                        parsed_created_at = parsed_created_at.replace(
                            tzinfo=timezone.utc,
                        )
                    payload["created_at"] = parsed_created_at.replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    ).isoformat()
                except ValueError:
                    pass
            definition_input.append(payload)
        snapshot = build_readiness_snapshot(
            definition={"definitions": definition_input},
            evidence=evidence_input,
            runtime=runtime,
            reputation={"items": reputation},
            project={"items": project},
            similarity={
                "duplicate_score": layers.duplicate_score
                if duplicate_score is None else duplicate_score,
            },
        )
        # ``build_readiness_snapshot`` remains the only source of readiness
        # arithmetic.  Only serialize its wall-clock stability witness at a
        # bounded precision so proposal/execute TOCTOU checks are repeatable.
        components = dict(snapshot.get("components") or {})
        components["stability"] = round(
            float(components.get("stability") or 0.0), 4,
        )
        snapshot["components"] = components
        snapshot["score"] = round(float(snapshot.get("score") or 0.0), 4)
        digest_payload = {
            key: value for key, value in snapshot.items() if key != "digest"
        }
        snapshot["digest"] = stable_hash(
            "rule-readiness-snapshot-v1",
            json.dumps(
                digest_payload, ensure_ascii=False, sort_keys=True, default=str,
            ),
        )
        return snapshot

    def build_readiness_snapshot(
        self, definition_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Build the canonical readiness snapshot for a persisted pair."""
        ids = sorted({str(item) for item in definition_ids})
        if len(ids) != 2:
            raise ValueError("rule_merge_pair_required")
        with self._db() as conn:
            rows = {
                definition_id: conn.execute(
                    "SELECT * FROM rule_definitions WHERE definition_id=?",
                    (definition_id,),
                ).fetchone()
                for definition_id in ids
            }
            if any(row is None for row in rows.values()):
                raise ValueError("rule_merge_definition_not_found")
            return self._conn_readiness_snapshot(conn, rows)

    @staticmethod
    def _evidence_row_is_eligible(row: sqlite3.Row) -> bool:
        """Treat explicit untrusted sessions as non-independent evidence.

        Legacy rows without a session remain compatible; once a producer
        supplies a session, that session must carry explicit trust from the
        immutable receipt/outbox snapshot before it can satisfy governance.
        """
        session_id = str(row["session_id"] or "") if "session_id" in row.keys() else ""
        if (
            str(row["source_root_id"] or "")
            == "ambiguous_migration_evidence"
        ):
            return False
        return not session_id.strip() or int(row["session_trusted"] or 0) == 1

    @staticmethod
    def _dedupe_conn_rows(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        best: dict[str, sqlite3.Row] = {}
        for row in rows:
            key = str(row["independence_key"] or row["evidence_id"] or "")
            current = best.get(key)
            if current is None or (
                int(row["feedback_authority"] or 0),
                float(row["confidence"] if row["confidence"] is not None else 1.0),
                str(row["observed_at"] or ""),
                str(row["evidence_id"] or ""),
            ) > (
                int(current["feedback_authority"] or 0),
                float(
                    current["confidence"]
                    if current["confidence"] is not None else 1.0
                ),
                str(current["observed_at"] or ""),
                str(current["evidence_id"] or ""),
            ):
                best[key] = row
        return list(best.values())

    def _state_snapshot_conn(
        self, conn: sqlite3.Connection, definition_ids: Iterable[str],
    ) -> dict[str, str]:
        ids = sorted({str(item) for item in definition_ids})
        placeholders = ",".join("?" for _ in ids)
        definitions = [
            dict(row) for row in conn.execute(
                f"SELECT definition_id,status,revision,superseded_by "
                f"FROM rule_definitions WHERE definition_id IN ({placeholders}) "
                f"ORDER BY definition_id",
                ids,
            ).fetchall()
        ]
        bindings = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_bindings WHERE status='active' "
                f"AND definition_id IN ({placeholders}) ORDER BY binding_id",
                ids,
            ).fetchall()
        ]
        binding_contributions = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_binding_contributions "
                f"WHERE definition_id IN ({placeholders}) "
                f"ORDER BY contribution_id",
                ids,
            ).fetchall()
        ]
        evidence = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_evidence WHERE definition_id IN ({placeholders}) "
                f"ORDER BY evidence_id",
                ids,
            ).fetchall()
        ]
        negative = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_negative_evidence "
                f"WHERE definition_id IN ({placeholders}) ORDER BY evidence_id",
                ids,
            ).fetchall()
        ]
        runtime = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_runtime_feedback "
                f"WHERE definition_id IN ({placeholders}) ORDER BY feedback_id",
                ids,
            ).fetchall()
        ]
        effective_projection = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_effective_feedback_projection "
                f"WHERE definition_id IN ({placeholders}) ORDER BY receipt_id",
                ids,
            ).fetchall()
        ]
        evidence_contributions = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_evidence_contributions "
                f"WHERE definition_id IN ({placeholders}) ORDER BY contribution_id",
                ids,
            ).fetchall()
        ]
        evidence_effective = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_evidence_effective "
                f"WHERE definition_id IN ({placeholders}) ORDER BY definition_id, independence_key, kind",
                ids,
            ).fetchall()
        ]
        links = [
            dict(row) for row in conn.execute(
                f"SELECT * FROM rule_source_links WHERE "
                f"canonical_definition_id IN ({placeholders}) "
                f"OR original_definition_id IN ({placeholders}) "
                f"ORDER BY share_group_id,memory_id",
                ids + ids,
            ).fetchall()
        ]
        return {
            "definitions": stable_hash("merge-state-definitions", json.dumps(definitions, sort_keys=True, default=str)),
            "bindings": stable_hash("merge-state-bindings", json.dumps(bindings, sort_keys=True, default=str)),
            "binding_contributions": stable_hash(
                "merge-state-binding-contributions",
                json.dumps(binding_contributions, sort_keys=True, default=str),
            ),
            "evidence": stable_hash("merge-state-evidence", json.dumps(evidence, sort_keys=True, default=str)),
            "negative": stable_hash("merge-state-negative", json.dumps(negative, sort_keys=True, default=str)),
            "runtime": stable_hash("merge-state-runtime", json.dumps(runtime, sort_keys=True, default=str)),
            "effective_projection": stable_hash(
                "merge-state-effective-projection",
                json.dumps(effective_projection, sort_keys=True, default=str),
            ),
            "evidence_contributions": stable_hash(
                "merge-state-evidence-contributions",
                json.dumps(evidence_contributions, sort_keys=True, default=str),
            ),
            "evidence_effective": stable_hash(
                "merge-state-evidence-effective",
                json.dumps(evidence_effective, sort_keys=True, default=str),
            ),
            "source_links": stable_hash("merge-state-links", json.dumps(links, sort_keys=True, default=str)),
        }

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
            "readiness_components": json.loads(
                row["readiness_components"] or "{}"
            ),
            "readiness_digest": row["readiness_digest"] or "",
            "governance_reasons": row["governance_reasons"] or "",
            "cooldown_until": row["cooldown_until"] or "",
            "first_merge_acknowledged": bool(row["first_merge_acknowledged"]),
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
            "negative_digest": row["negative_digest"] or "",
            "binding_digest": row["binding_digest"] or "",
            "runtime_digest": row["runtime_digest"] or "",
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
            with self._write_conn() as connection:
                connection.execute(sql, values)
        return {
            "decision_id": decision_id,
            "proposal_id": proposal_id,
            "canonical_definition_id": canonical_definition_id,
            "merged_definition_ids": sorted(merged_definition_ids),
            "before_bindings": before_bindings,
            "after_bindings": after_bindings,
            "migration": migration,
            "execution_mode": migration.get("execution_mode", "auto"),
            "auto_merge": bool(migration.get("auto_merge", actor == "auto")),
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
            "execution_mode": json.loads(row["migration"] or "{}").get(
                "execution_mode", "auto"
            ),
            "auto_merge": bool(
                json.loads(row["migration"] or "{}").get(
                    "auto_merge", (row["actor"] or "auto") == "auto"
                )
            ),
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
            "execution_mode": json.loads(r["migration"] or "{}").get(
                "execution_mode", "auto"
            ),
            "auto_merge": bool(
                json.loads(r["migration"] or "{}").get(
                    "auto_merge", (r["actor"] or "auto") == "auto"
                )
            ),
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
        execution_mode: str = "auto",
        expected_definition_revisions: dict[str, int] | None = None,
        expected_evidence_digest: str = "",
        expected_negative_digest: str = "",
        expected_binding_digest: str = "",
        expected_assessment_revision: int | None = None,
        expected_policy_version: str = "",
        expected_runtime_digest: str = "",
        expected_readiness_digest: str = "",
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
        execution_mode = str(execution_mode or "auto").strip().casefold()
        if execution_mode not in {"auto", "approved", "human-approved"}:
            raise ValueError("rule_merge_execution_mode_invalid")
        if execution_mode in {"approved", "human-approved"} and not approval_id:
            raise RuntimeError("rule_merge_approval_required")
        merged = sorted({str(x) for x in merged_definition_ids} - {canonical_definition_id})
        with self._write_conn() as conn:
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
                approval_expected_revisions: dict[str, int] = {}
                if approval_id:
                    approval_row = conn.execute(
                        "SELECT * FROM rule_merge_approvals WHERE approval_id=? "
                        "AND proposal_id=?",
                        (approval_id, proposal_id),
                    ).fetchone()
                    if approval_row is None:
                        raise RuntimeError("rule_merge_approval_invalid")
                    capability_row = conn.execute(
                        "SELECT * FROM governance_capabilities "
                        "WHERE token_hash=? AND proposal_id=? "
                        "AND scope=? AND consumed=1",
                        (
                            str(approval_row["capability_id"] or ""),
                            proposal_id, "rule_merge_approve",
                        ),
                    ).fetchone()
                    if capability_row is None:
                        raise RuntimeError("rule_merge_approval_capability_invalid")
                    if float(capability_row["expires_at"] or 0) <= time.time():
                        raise RuntimeError("rule_merge_approval_expired")
                    if str(approval_row["approval_scope"] or "merge") != "merge":
                        raise RuntimeError("rule_merge_approval_scope_invalid")
                    approval_expected_revisions = {
                        str(key): int(value)
                        for key, value in json.loads(
                            approval_row["expected_definition_revisions"] or "{}"
                        ).items()
                    }
                elif execution_mode in {"approved", "human-approved"}:
                    raise RuntimeError("rule_merge_approval_required")

                # Lock the proposal so a concurrent merge cannot double-run.
                conn.execute(
                    "UPDATE rule_merge_proposals SET status='merging' WHERE proposal_id=?",
                    (proposal_id,),
                )

                # Snapshot before-state: bindings and evidence per definition.
                before_bindings: list[dict[str, Any]] = []
                original_bindings: dict[str, list[str]] = {}
                original_evidence: dict[str, list[str]] = {}
                original_negative_evidence: dict[str, list[str]] = {}
                original_runtime_feedback: dict[str, list[str]] = {}
                original_contributions: dict[str, list[str]] = {}
                original_evidence_contributions: dict[str, list[str]] = {}
                original_effective_projection: dict[str, dict[str, Any]] = {}
                original_revisions: dict[str, int] = {}
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
                    if row["status"] != "active":
                        raise ValueError("rule_definition_already_merged")
                    definition_rows[definition_id] = row
                    original_revisions[definition_id] = int(row["revision"] or 0)
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
                    negative_rows = conn.execute(
                        "SELECT evidence_id FROM rule_negative_evidence "
                        "WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    original_negative_evidence[definition_id] = [
                        r["evidence_id"] for r in negative_rows
                    ]
                    runtime_rows = conn.execute(
                        "SELECT feedback_id FROM rule_runtime_feedback "
                        "WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    original_runtime_feedback[definition_id] = [
                        r["feedback_id"] for r in runtime_rows
                    ]
                    contribution_rows = conn.execute(
                        "SELECT contribution_id FROM rule_binding_contributions "
                        "WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    original_contributions[definition_id] = [
                        r["contribution_id"] for r in contribution_rows
                    ]
                    evidence_contribution_rows = conn.execute(
                        "SELECT contribution_id FROM rule_evidence_contributions "
                        "WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    original_evidence_contributions[definition_id] = [
                        r["contribution_id"] for r in evidence_contribution_rows
                    ]
                    projection_rows = conn.execute(
                        "SELECT * FROM rule_effective_feedback_projection "
                        "WHERE definition_id=?",
                        (definition_id,),
                    ).fetchall()
                    for projection_row in projection_rows:
                        original_effective_projection[projection_row["receipt_id"]] = dict(
                            projection_row
                        )

                if set((expected_definition_revisions or {})) != set(
                    all_definition_ids
                ):
                    raise RuntimeError("rule_merge_definition_snapshot_required")
                if not expected_evidence_digest:
                    raise RuntimeError("rule_merge_evidence_snapshot_required")
                if not expected_negative_digest:
                    raise RuntimeError("rule_merge_negative_snapshot_required")
                if not expected_binding_digest:
                    raise RuntimeError("rule_merge_binding_snapshot_required")
                if expected_assessment_revision is None:
                    raise RuntimeError("rule_merge_assessment_snapshot_required")
                if not expected_policy_version:
                    raise RuntimeError("rule_merge_policy_snapshot_required")

                proposal_definition_ids = json.loads(
                    proposal["definition_ids"] or "[]"
                )
                proposal_revisions = {
                    str(proposal_definition_ids[0]): int(
                        proposal["definition_revision_a"] or 0
                    ),
                    str(proposal_definition_ids[1]): int(
                        proposal["definition_revision_b"] or 0
                    ),
                }
                supplied_revisions = {
                    str(key): int(value)
                    for key, value in (expected_definition_revisions or {}).items()
                }
                if supplied_revisions != proposal_revisions:
                    raise RuntimeError("rule_merge_definition_snapshot_mismatch")
                if approval_expected_revisions and (
                    approval_expected_revisions != supplied_revisions
                ):
                    raise RuntimeError("rule_merge_approval_snapshot_mismatch")
                current_revisions = {
                    str(definition_id): int(
                        definition_rows[definition_id]["revision"] or 0
                    )
                    for definition_id in all_definition_ids
                }
                if supplied_revisions != current_revisions:
                    raise RuntimeError("rule_merge_definition_revision_drift")
                if str(expected_evidence_digest) != str(
                    proposal["evidence_digest"] or ""
                ):
                    raise RuntimeError("rule_merge_evidence_snapshot_mismatch")
                if str(expected_negative_digest) != str(
                    proposal["negative_digest"] or ""
                ):
                    raise RuntimeError("rule_merge_negative_snapshot_mismatch")
                if str(expected_binding_digest) != str(
                    proposal["binding_digest"] or ""
                ):
                    raise RuntimeError("rule_merge_binding_snapshot_mismatch")
                if str(expected_runtime_digest) != str(
                    proposal["runtime_digest"] or ""
                ):
                    raise RuntimeError("rule_merge_runtime_snapshot_mismatch")
                if int(expected_assessment_revision) != int(
                    proposal["assessment_revision"] or 0
                ):
                    raise RuntimeError("rule_merge_assessment_snapshot_mismatch")
                if str(expected_policy_version) != str(
                    proposal["policy_version"] or ""
                ):
                    raise RuntimeError("rule_merge_policy_snapshot_mismatch")

                # Hard gates re-computed against the *current* rows inside the
                # transaction: strength/polarity/parameter/negative evidence.
                # A definition edited after the scan (or a force-approved
                # conflict) can never merge — the human path cannot bypass this.
                gates = self._recompute_hard_gates(
                    conn,
                    definition_rows,
                    minimum_similarity=(
                        AUTO_MERGE_SCORE
                        if execution_mode == "auto"
                        else HUMAN_MERGE_MIN_SIMILARITY
                    ),
                )
                strength_ok = bool(gates["strength_ok"])
                negative_ok = bool(gates["negative_ok"])
                for gate, ok in gates.items():
                    if gate in {"duplicate_score", "match_kind"}:
                        continue
                    if not ok:
                        raise RuntimeError(f"rule_merge_hard_gate_regression: {gate}")
                if abs(
                    float(gates["duplicate_score"])
                    - float(proposal["similarity_score"] or 0.0)
                ) > 1e-6:
                    raise RuntimeError("rule_merge_similarity_drift")

                readiness_snapshot = self._conn_readiness_snapshot(
                    conn, definition_rows,
                )
                relevant_group_ids = self._groups_for_definitions_conn(
                    conn, all_definition_ids,
                )

                if execution_mode == "auto":
                    if not self._conn_projection_ready(
                        conn, group_ids=relevant_group_ids,
                    ):
                        raise RuntimeError("rule_merge_projection_incomplete")
                    if not self._auto_maturity_gate(definition_rows, conn=conn):
                        raise RuntimeError("rule_merge_maturity_gate_failed")
                    if float(readiness_snapshot["score"]) < AUTO_READINESS_SCORE:
                        raise RuntimeError("rule_merge_readiness_gate_failed")
                    if abs(
                        float(readiness_snapshot["score"])
                        - float(proposal["readiness_score"] or 0.0)
                    ) > 1e-6:
                        raise RuntimeError("rule_merge_readiness_drift")

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
                if expected_assessment_revision is not None and int(
                    proposal["assessment_revision"] or 0
                ) != int(expected_assessment_revision):
                    raise RuntimeError("rule_merge_assessment_revision_drift")
                if expected_policy_version and str(
                    proposal["policy_version"] or ""
                ) != str(expected_policy_version):
                    raise RuntimeError("rule_merge_policy_version_drift")

                # Expected evidence digest: reject a silently-changed evidence set.
                if expected_evidence_digest:
                    evidence_ids = sorted(
                        eid
                        for definition_id in all_definition_ids
                        for eid in original_evidence[definition_id]
                    )
                    digest = self._evidence_digest_from_ids(
                        evidence_ids, conn=conn,
                    )
                    if digest != expected_evidence_digest:
                        raise RuntimeError("rule_merge_evidence_digest_drift")
                if expected_negative_digest:
                    digest = self._negative_digest(
                        all_definition_ids, conn=conn,
                    )
                    if digest != expected_negative_digest:
                        raise RuntimeError("rule_merge_negative_digest_drift")
                if expected_binding_digest:
                    digest = self._binding_digest(all_definition_ids, conn=conn)
                    if digest != expected_binding_digest:
                        raise RuntimeError("rule_merge_binding_digest_drift")
                if expected_runtime_digest:
                    digest = self._runtime_digest(all_definition_ids, conn=conn)
                    if digest != expected_runtime_digest:
                        raise RuntimeError("rule_merge_runtime_digest_drift")

                # One shared readiness snapshot covers both auto gating and
                # proposal/TOCTOU verification.
                stored_readiness_digest = str(
                    proposal["readiness_digest"] or ""
                )
                if not expected_readiness_digest or not stored_readiness_digest:
                    raise RuntimeError("rule_merge_readiness_snapshot_required")
                if expected_readiness_digest != stored_readiness_digest:
                    raise RuntimeError("rule_merge_readiness_snapshot_mismatch")
                if readiness_snapshot["digest"] != expected_readiness_digest:
                    raise RuntimeError("rule_merge_readiness_digest_drift")
                if abs(
                    float(readiness_snapshot["score"])
                    - float(proposal["readiness_score"] or 0.0)
                ) > 1e-6:
                    raise RuntimeError("rule_merge_readiness_drift")

                # Update every merged definition's Bindings to the canonical id.
                for definition_id in merged:
                    conn.execute(
                        "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                        "updated_at=? WHERE definition_id=?",
                        (canonical_definition_id, now, definition_id),
                    )
                    # The binding rows above are the same rows referenced by
                    # their source contributions.  Repoint the contribution
                    # Definition in this transaction as well; a separate
                    # rehome call would create a visible cross-transaction
                    # window and could lose a source row on failure.
                    conn.execute(
                        "UPDATE rule_binding_contributions SET definition_id=?, "
                        "revision=revision+1, updated_at=? WHERE definition_id=?",
                        (canonical_definition_id, now, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_evidence SET definition_id=? WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_negative_evidence SET definition_id=? "
                        "WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_runtime_feedback SET definition_id=? "
                        "WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_effective_feedback_projection "
                        "SET definition_id=? WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "UPDATE rule_evidence_contributions SET definition_id=? "
                        "WHERE definition_id=?",
                        (canonical_definition_id, definition_id),
                    )
                    conn.execute(
                        "DELETE FROM rule_evidence_effective WHERE definition_id=?",
                        (definition_id,),
                    )
                    conn.execute(
                        "UPDATE rule_definitions SET status='merged', superseded_by=?, "
                        "updated_at=? WHERE definition_id=?",
                        (canonical_definition_id, now, definition_id),
                    )
                conn.execute(
                    "UPDATE rule_definitions SET revision=revision+1, updated_at=? "
                    "WHERE definition_id=?",
                    (now, canonical_definition_id),
                )
                for definition_id in all_definition_ids:
                    self._recompute_runtime_stats_conn(conn, definition_id)
                rebuild_effective(conn, definition_id=canonical_definition_id)
                self._materialize_evidence_compat_conn(
                    conn, all_definition_ids,
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
                    "original_negative_evidence": original_negative_evidence,
                    "original_runtime_feedback": original_runtime_feedback,
                    "original_contributions": original_contributions,
                    "original_evidence_contributions": original_evidence_contributions,
                    "original_effective_projection": original_effective_projection,
                    "original_revisions": original_revisions,
                }
                migration["execution_mode"] = execution_mode
                migration["auto_merge"] = execution_mode == "auto"
                migration["post_state"] = self._state_snapshot_conn(
                    conn, all_definition_ids,
                )
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
            except Exception:
                raise
        return decision

    def undo_merge(self, decision_id: str) -> dict[str, Any]:
        """Precisely undo a merge: restore bindings/evidence/definitions."""
        now = _now()
        with self._write_conn() as conn:
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
                original_negative_evidence = migration.get(
                    "original_negative_evidence", {}
                )
                original_runtime_feedback = migration.get(
                    "original_runtime_feedback", {}
                )
                original_contributions = migration.get(
                    "original_contributions", {}
                )
                original_evidence_contributions = migration.get(
                    "original_evidence_contributions", {}
                )
                original_effective_projection = migration.get(
                    "original_effective_projection", {}
                )
                original_revisions = migration.get("original_revisions", {})
                all_definition_ids = [canonical, *merged]
                expected_post_state = migration.get("post_state")
                if expected_post_state:
                    actual_post_state = self._state_snapshot_conn(
                        conn, all_definition_ids,
                    )
                    # Older decisions captured global projection watermarks,
                    # and pre-fix decisions did not capture binding
                    # contributions.  Ignore only those legacy fields while
                    # comparing the merge-owned domain; never compare global
                    # projection state during an unrelated undo.
                    expected_post_state = {
                        key: value for key, value in expected_post_state.items()
                        if key != "projection_state"
                    }
                    actual_for_compare = {
                        key: value for key, value in actual_post_state.items()
                        if key in expected_post_state
                    }
                    if actual_for_compare != expected_post_state:
                        conn.execute(
                            "UPDATE rule_merge_decisions SET status='conflict' "
                            "WHERE decision_id=?",
                            (decision_id,),
                        )
                        conn.commit()
                        return {
                            "decision_id": decision_id,
                            "status": "conflict",
                            "reason": "post_merge_state_drift",
                        }
                # Restore binding ownership for every merged definition.
                for definition_id, binding_ids in original_bindings.items():
                    for binding_id in binding_ids:
                        conn.execute(
                            "UPDATE rule_bindings SET definition_id=?, revision=revision+1, "
                            "updated_at=? WHERE binding_id=?",
                            (definition_id, now, binding_id),
                        )
                for definition_id, contribution_ids in original_contributions.items():
                    for contribution_id in contribution_ids:
                        conn.execute(
                            "UPDATE rule_binding_contributions SET definition_id=?, "
                            "revision=revision+1, updated_at=? "
                            "WHERE contribution_id=?",
                            (definition_id, now, contribution_id),
                        )
                for definition_id, contribution_ids in original_evidence_contributions.items():
                    for contribution_id in contribution_ids:
                        conn.execute(
                            "UPDATE rule_evidence_contributions SET definition_id=?, "
                            "updated_at=? WHERE contribution_id=?",
                            (definition_id, now, contribution_id),
                        )
                conn.execute(
                    "DELETE FROM rule_evidence_effective WHERE definition_id IN (" +
                    ",".join("?" for _ in all_definition_ids) + ")",
                    all_definition_ids,
                )
                for definition_id, evidence_ids in original_evidence.items():
                    for evidence_id in evidence_ids:
                        conn.execute(
                            "UPDATE rule_evidence SET definition_id=? WHERE evidence_id=?",
                            (definition_id, evidence_id),
                        )
                for definition_id, evidence_ids in original_negative_evidence.items():
                    for evidence_id in evidence_ids:
                        conn.execute(
                            "UPDATE rule_negative_evidence SET definition_id=? "
                            "WHERE evidence_id=?",
                            (definition_id, evidence_id),
                        )
                for definition_id, feedback_ids in original_runtime_feedback.items():
                    for feedback_id in feedback_ids:
                        conn.execute(
                            "UPDATE rule_runtime_feedback SET definition_id=? "
                            "WHERE feedback_id=?",
                            (definition_id, feedback_id),
                        )
                for projection in original_effective_projection.values():
                    conn.execute(
                        """
                        UPDATE rule_effective_feedback_projection SET
                            effective_feedback_id=?, definition_id=?, outcome=?,
                            positive_evidence_id=?, negative_evidence_id=?,
                            session_trusted=?, session_source=?, updated_at=?
                        WHERE receipt_id=?
                        """,
                        (
                            projection.get("effective_feedback_id", ""),
                            projection.get("definition_id", ""),
                            projection.get("outcome", ""),
                            projection.get("positive_evidence_id", ""),
                            projection.get("negative_evidence_id", ""),
                            int(projection.get("session_trusted", 0) or 0),
                            projection.get("session_source", "absent"),
                            projection.get("updated_at", ""),
                            projection.get("receipt_id", ""),
                        ),
                    )
                for definition_id in merged:
                    conn.execute(
                        "UPDATE rule_definitions SET status='active', superseded_by='', "
                        "updated_at=? WHERE definition_id=?",
                        (now, definition_id),
                    )
                for definition_id, revision in original_revisions.items():
                    conn.execute(
                        "UPDATE rule_definitions SET revision=?, updated_at=? "
                        "WHERE definition_id=?",
                        (int(revision), now, definition_id),
                    )
                for definition_id in all_definition_ids:
                    self._recompute_runtime_stats_conn(conn, definition_id)
                    rebuild_effective(conn, definition_id=definition_id)
                self._materialize_evidence_compat_conn(
                    conn, all_definition_ids,
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
            except Exception:
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

    @staticmethod
    def _shadow_value(value: Any, key: str, default: Any = "") -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _shadow_audience_key(
        cls,
        value: Any,
        share_group_id: str,
        *,
        priority: int | None = None,
    ) -> tuple[str, str, str, str, str, str, int, str]:
        target_type = str(
            cls._shadow_value(value, "target_type", "") or ""
        )
        target_id = str(cls._shadow_value(value, "target_id", "") or "")
        project_ref = canonical_project_ref(
            str(cls._shadow_value(value, "project_ref", "") or "")
        )
        provider = str(cls._shadow_value(value, "provider", "") or "")
        runtime_role = str(
            cls._shadow_value(value, "runtime_role", "") or ""
        )
        effect = str(cls._shadow_value(value, "effect", "include") or "include")
        if priority is None:
            raw_priority = cls._shadow_value(
                value,
                "priority" if isinstance(value, RuleBinding) else "priority_override",
                0,
            )
            priority = int(raw_priority or 0)
        if target_type == "project" and not project_ref:
            project_ref = canonical_project_ref(target_id)
        if target_type == "project":
            target_id = ""
        # Legacy assignments encode provider/runtime role in target_id;
        # materialized bindings may also carry the dedicated field.  Normalize
        # both representations to one audience identity.
        if target_type == "provider" and not provider:
            provider = target_id
        if target_type == "runtime_role" and not runtime_role:
            runtime_role = target_id
        return (
            target_type,
            target_id,
            project_ref,
            provider.casefold(),
            runtime_role.casefold(),
            effect,
            priority,
            str(share_group_id or ""),
        )

    @staticmethod
    def _shadow_audience_dict(
        key: tuple[str, str, str, str, str, str, int, str],
    ) -> dict[str, Any]:
        return {
            "target_type": key[0],
            "target_id": key[1],
            "project_ref": key[2],
            "provider": key[3],
            "runtime_role": key[4],
            "effect": key[5],
            "priority": key[6],
            "share_group_id": key[7],
        }

    def shadow_verify(
        self,
        context: EffectiveAgentContext,
        legacy_records: list[tuple[str, list[Any]]],
        *,
        legacy_priorities: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Compare the legacy matcher with the Definition/Binding matcher.

        ``legacy_records`` is a list of ``(memory_id, assignments)`` pairs
        taken from the legacy store.  The new matcher resolves the same
        context through Definitions → Bindings.  ``missing`` = legacy matched,
        new did not; ``extra`` = new matched, legacy did not; ``permission_diff``
        = a new binding is broader than any legacy assignment for this context.
        """
        legacy_matched: set[str] = set()
        legacy_audiences: Counter[tuple[Any, ...]] = Counter()
        priorities = legacy_priorities or {}
        for memory_id, assignments in legacy_records:
            for assignment in assignments:
                try:
                    normalized = normalize_assignment(assignment)
                except ValueError:
                    continue
                if not assignment_matches(normalized, context):
                    continue
                legacy_matched.add(memory_id)
                legacy_audiences[
                    self._shadow_audience_key(
                        normalized,
                        context.share_group_id,
                        priority=priorities.get(memory_id),
                    )
                ] += 1

        new_matched: set[str] = set()
        new_audiences: Counter[tuple[Any, ...]] = Counter()
        for binding in self.list_bindings(
            share_group_id=context.share_group_id,
        ):
            if not self._binding_matches(binding, context):
                continue
            new_audiences[
                self._shadow_audience_key(
                    binding, context.share_group_id,
                )
            ] += 1
            definition = self.get_definition(binding.definition_id)
            if definition is None or definition.status not in {"active", "alias"}:
                continue
            # Map definition back to the source rules (evidence origins).
            for evidence in self.list_evidence(definition_id=definition.definition_id):
                if evidence.source_rule_id:
                    new_matched.add(evidence.source_rule_id)

        missing = sorted(legacy_matched - new_matched)
        extra = sorted(new_matched - legacy_matched)
        # P3-03: permission boundaries compare the *unique* audience set, not
        # the number of contributing sources.  Two legacy assignments that both
        # materialize into one shared P3 binding are not a permission change;
        # source integrity is verified separately by ``missing``/``extra``.
        missing_audiences = set(legacy_audiences) - set(new_audiences)
        extra_audiences = set(new_audiences) - set(legacy_audiences)
        permission_missing = [
            self._shadow_audience_dict(key)
            for key in sorted(missing_audiences)
        ]
        permission_extra = [
            self._shadow_audience_dict(key)
            for key in sorted(extra_audiences)
        ]
        permission_diff = len(permission_missing) + len(permission_extra)
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
        with self._db() as conn:
            materialized_binding_map = {
                (str(row["binding_id"]), str(row["definition_id"]))
                for row in conn.execute(
                    "SELECT binding_id, definition_id FROM rule_bindings "
                    "WHERE status='active'"
                ).fetchall()
            }
            contribution_binding_map = {
                (str(row["binding_id"]), str(row["definition_id"]))
                for row in conn.execute(
                    "SELECT binding_id, definition_id "
                    "FROM rule_binding_contributions "
                    "WHERE active=1 AND status='active'"
                ).fetchall()
            }
        binding_contribution_diff = len(
            materialized_binding_map.symmetric_difference(
                contribution_binding_map,
            )
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
            # No executed decision is evidence of no violations.  Keep this
            # metric fail-closed so an empty database cannot report green.
            auto_merge_precision = 0.0
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
            "binding_contribution_diff": binding_contribution_diff,
            "evidence_count": self.count_evidence(),
            "proposal_count": len(self.list_proposals()),
            "merged_proposal_count": len(self.list_proposals(status="merged")),
            "system_auto_binding": len(system_auto),
            "auto_broad_binding": len(auto_broad),
            "merge_undo_success": 1 if undo_state_digest_diff == 0 else 0,
            "migration_loss": self._migration_loss(),
            "auto_merge_precision": round(auto_merge_precision, 4),
            "auto_merge_precision_status": (
                "observed" if mergeable_decision_count else "unobserved"
            ),
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

        auto_merge_precision = 0.0
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
            and bool(decisions)
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
            "auto_merge_precision_status": (
                "observed" if decisions else "unobserved"
            ),
            "merge_decision_count": len(decisions),
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
        *,
        minimum_similarity: float = HUMAN_MERGE_MIN_SIMILARITY,
    ) -> dict[str, Any]:
        """Recompute the merge hard gates from the current DB rows.

        Called inside ``execute_merge``'s transaction so a definition edited
        between scan and merge cannot silently turn a safe pair into a
        governance conflict (or back).
        """
        ordered = list(definition_rows.values())
        if len(ordered) != 2:
            raise ValueError("rule_merge_pair_required")
        a, b = (self._row_to_definition(row) for row in ordered)
        layers = compute_layers(a, b)
        similarity_ok = layers.duplicate_score >= float(minimum_similarity)
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
            if self._evidence_row_is_eligible(row)
        ]
        positive_rows = [
            row
            for definition_row in ordered
            for row in conn.execute(
                "SELECT * FROM rule_evidence WHERE definition_id=?",
                (definition_row["definition_id"],),
            ).fetchall()
            if self._evidence_row_is_eligible(row)
        ]
        negative_rows = self._dedupe_conn_rows(negative_rows)
        positive_rows = self._dedupe_conn_rows(positive_rows)
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
            "similarity_ok": similarity_ok,
            "duplicate_score": layers.duplicate_score,
            "match_kind": merge_match_kind(layers),
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
                evidence_confidence=float(
                    ev["confidence"] if ev["confidence"] is not None else 0.0
                ),
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
