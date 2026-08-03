"""Rule Merge orchestration (P3).

Sits between the shared-memory layer (SharedMemoryStore) and the Rule
Intelligence store (RuleMergeStore).  It wires up:

  * backfill       — migrate legacy records+assignments into Definitions/Bindings
  * dual-write     — keep the new layer in sync when a rule is created
  * duplicate scan — three-layer detection → merge proposals
  * safe merge     — evaluate the five conditions, execute atomically, record
                     an undoable decision

Security is explicit here, not incidental: a merge only ever repoints Bindings
from one definition_id to another, and the store's transaction refuses any
transaction where the audience identity set changes.
"""
from __future__ import annotations

import json
import inspect
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .rule_binding import (
    AUTO_ALLOWED_TARGET_TYPES,
    RuleBinding,
    binding_identity_key,
    build_binding,
)
from .rule_definition import (
    STRENGTH_UNKNOWN,
    RuleDefinition,
    RuleStrength,
    build_definition,
    normalize_rule_text,
    semantic_surface,
)
from .rule_evidence import (
    NegativeEvidence,
    RuleEvidence,
    build_evidence,
    build_negative_evidence,
    dedupe_evidence,
)
from .rule_evidence_ledger import build_contribution
from .merge_governance_coordinator import MergeGovernanceCoordinator
from .access_context import session_trust_is_valid
from .rule_merge_policy import (
    AUTO_MERGE_MIN_AGENTS,
    AUTO_MERGE_MIN_EVIDENCE,
    AUTO_MERGE_MIN_PROJECTS,
    AUTO_MERGE_SCORE,
    AUTO_READINESS_SCORE,
    COOLDOWN_HOURS,
    char_bigram_set,
    MAX_SINGLE_SOURCE_RATIO,
    MIN_REPUTATION_SAMPLES,
    NEGATIVE_EVIDENCE_THRESHOLD,
    OBSERVING_DAYS,
    REJECTED_PERSIST_FLOOR,
    TRUSTED_DAYS,
    TRUSTED_MIN_SUCCESS_SAMPLES,
    VALIDATED_SUCCESS_RATE,
    WEIGHTED_EVIDENCE_MIN,
    MergeAssessment,
    bayesian_accuracy,
    build_maturity_snapshot,
    compute_layers,
    contradiction_score,
    days_between,
    evaluate_candidate,
    evidence_weight,
    feedback_authority_score,
    largest_source_ratio,
    maturity_score,
    merge_match_kind,
    maturity_gate,
    negative_evidence_score,
    parameters_of,
    parameter_conflict,
    project_importance_score,
    recency_factor,
    weighted_evidence_score,
)
from .rule_merge_store import (
    HUMAN_MERGE_MIN_SIMILARITY,
    RuleMergeStore,
    iter_legacy_groups,
)
from .schema_v3 import SharedMemoryRecord, SharedMemoryStatus, _now_iso, stable_hash

# Actors that count as explicit human governance (bypass readiness/cooldown/
# first-merge gates — the human already reviewed the risk).  Hard conflicts
# (strength/polarity/parameter/negative evidence) are never bypassed.
HUMAN_ACTORS = {"human", "user", "admin", "manual"}

# Candidate recall is bounded per compatible definition group.  Small groups
# are exhaustively paired so near-synonyms cannot be lost to a hash bucket;
# large groups use deterministic character-block top-k recall.
SCAN_TOP_K = 20
SCAN_EXHAUSTIVE_GROUP_LIMIT = 64
SCAN_POSTING_LIMIT = 64
READINESS_DRIFT_TOLERANCE = 1e-4

_ACTIVE_SOURCE_STATUS = SharedMemoryStatus.ACTIVE.value


class RuleMergeService:
    """High-level merge pipeline over one Rule Intelligence store."""

    def __init__(self, store: RuleMergeStore, judge: Any | None = None):
        self.store = store
        # P3.3: optional semantic judge.  None keeps the deterministic Dice
        # semantic layer exactly as before; a judge adds an auditable verdict.
        self.judge = judge
        # PR7: the bounded scan reports how many pairs it evaluated, skipped and
        # persisted instead of silently dropping them.
        self.last_scan_summary: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Backfill / dual-write
    # ------------------------------------------------------------------

    def backfill_legacy(
        self,
        workspace: str | Path,
        *,
        only_group: str | None = None,
    ) -> dict[str, Any]:
        """Migrate every legacy shared-memory group into Definitions/Bindings.

        Returns a ``{missing:0, extra:0, permission_diff:0}``-style ledger plus
        per-group counts so the migration is machine-checkable.
        """
        from .shared_memory_store import SharedMemoryStore

        workspace = Path(workspace).resolve()
        groups: list[tuple[str, Path]] = []
        for group_id, db_path in iter_legacy_groups(workspace):
            if only_group and group_id != only_group:
                continue
            groups.append((group_id, db_path))

        totals = {
            "records": 0, "definitions": 0, "assignments": 0,
            "bindings": 0, "receipts": 0, "evidence": 0,
        }
        per_group: dict[str, dict[str, int]] = {}
        for group_id, _db_path in groups:
            store = SharedMemoryStore(workspace, group_id)
            ledger = self.backfill_group(store, group_id)
            per_group[group_id] = ledger
            for key in totals:
                totals[key] += ledger.get(key, 0)
        return {
            "groups": len(groups),
            "totals": totals,
            "per_group": per_group,
            "migration_loss": 0,
        }

    def backfill_group(
        self, store: Any, group_id: str,
    ) -> dict[str, int]:
        """Backfill one group as one RuleMergeStore transaction."""
        with self.store._write_conn():
            ledger = self._backfill_group_atomic(store, group_id)
        # This pass reads the committed post-migration state and is itself
        # fail-closed for unrecoverable legacy rows.
        self._mark_orphan_v1_definitions()
        return ledger

    def _backfill_group_atomic(
        self, store: Any, group_id: str,
    ) -> dict[str, int]:
        """Migrate one group's governed rules into the P3 layer (PR6).

        Only ``always``-policy rules are migrated (``relevant`` rules are recall,
        not governance, and must not enter the merge canonical set).  Backfill is
        idempotent and *never resurrects* a merged/superseded/alias lifecycle:

          * the source link resolves each record to its current canonical
            Definition (following the alias/merged chain), and a record whose
            rule was already merged routes its new bindings/evidence to the
            canonical instead of reviving the merged definition;
          * a pre-v2 Definition (whose id only covered canonical wording) is
            migrated onto its v2 id atomically, with the removed audiences
            re-verified after the pass;
          * wide legacy assignments (group/project/provider/runtime_role/system)
            are copied losslessly as ``migration``-sourced bindings carrying the
            legacy assignment hash + migration run id.
        """
        ledger = {
            "records": 0, "definitions": 0, "assignments": 0,
            "bindings": 0, "receipts": 0, "evidence": 0,
        }
        migrated_audiences: dict[str, list[Any]] = {}
        records = store.list_records()
        governed_records = [
            record for record in records
            if str(record.injection_policy or "") == "always"
        ]
        # Preflight v1 identity collisions before the per-source pass.  A
        # surface-only v1 id can represent multiple v2 strengths; moving all
        # old evidence to whichever source happens to run first would silently
        # inflate the wrong definition.  Keep the old row/bindings intact until
        # the source bindings have been rebuilt below.
        collision_targets: dict[str, dict[str, str]] = {}
        collision_definitions: dict[str, RuleDefinition] = {}
        for record in governed_records:
            legacy_id = self._legacy_definition_id(record)
            definition = self._definition_from_record(record)
            collision_definitions.setdefault(definition.definition_id, definition)
            bucket = collision_targets.setdefault(legacy_id, {})
            bucket[record.memory_id] = definition.definition_id
        collision_targets = {
            legacy_id: mapping
            for legacy_id, mapping in collision_targets.items()
            if len(set(mapping.values())) > 1
            and self.store.get_definition(legacy_id) is not None
            and self.store.get_definition(legacy_id).status == "active"
        }
        for mapping in collision_targets.values():
            for definition_id in set(mapping.values()):
                if self.store.get_definition(definition_id) is None:
                    self.store.upsert_definition(collision_definitions[definition_id])
        for legacy_id, mapping in collision_targets.items():
            removed = self.store.split_legacy_evidence(legacy_id, mapping)
            if removed:
                primary_id = sorted(set(mapping.values()))[0]
                migrated_audiences[primary_id] = removed

        for record in records:
            if str(record.injection_policy or "") != "always":
                continue  # governed rules only
            ledger["records"] += 1
            definition = self._definition_from_record(record)
            new_id = definition.definition_id
            # Resolve the current canonical for this source (source link then
            # alias/merged chain) BEFORE touching any row, so a merged rule is
            # never resurrected by a re-run.
            link = self.store.get_source_link(group_id, record.memory_id)
            canonical_id = new_id
            if link and link.get("canonical_definition_id"):
                canonical_id = self.store.resolve_canonical(
                    link["canonical_definition_id"],
                )
            existing = self.store.get_definition(canonical_id)
            if existing is not None and existing.status != "active":
                canonical_id = self.store.resolve_canonical(canonical_id)
                existing = self.store.get_definition(canonical_id)
            if existing is None:
                self.store.upsert_definition(definition)
                ledger["definitions"] += 1
            elif canonical_id == new_id:
                # Active rule refresh (idempotent re-run).
                self.store.upsert_definition(definition)
                ledger["definitions"] += 1
            # else: the source routes to a different canonical; leave the
            # merged/superseded lifecycle untouched.

            if getattr(record.status, "value", record.status) != _ACTIVE_SOURCE_STATUS:
                self.sync_rule(
                    store, group_id, record,
                    assignments=[], receipts=[], created_by="backfill",
                )
                continue

            legacy_id = self._legacy_definition_id(record)
            try:
                assignments = store.list_rule_assignments(record.memory_id)
            except Exception:
                assignments = []
            ledger["assignments"] += len(assignments)
            self.store.replace_source_contributions(
                group_id,
                record.memory_id,
                [
                    self._binding_from_assignment(
                        canonical_id, assignment, share_group_id=group_id,
                        owner_agent_id=record.agent_instance_id,
                        created_by="backfill",
                        authorization="backfill",
                    )
                    for assignment in assignments
                ],
                source_revision=(
                    record.updated_at or record.created_at or ""
                ),
                owner_agent_id=record.agent_instance_id,
            )
            ledger["bindings"] += len(assignments)
            try:
                receipts = store.list_rule_match_receipts(memory_id=record.memory_id)
            except Exception:
                receipts = []
            for receipt in receipts:
                ledger["receipts"] += 1
                evidence = build_evidence(
                    definition_id=canonical_id,
                    source_rule_id=record.memory_id,
                    agent_instance_id=receipt.agent_instance_id,
                    project_ref=receipt.project_ref,
                    provider=receipt.provider,
                    session_id=receipt.session_id,
                    receipt_id=receipt.receipt_id,
                    content=record.body,
                    confidence=receipt.confidence,
                    session_trusted=int(
                        session_trust_is_valid(
                            getattr(receipt, "session_id", ""),
                            getattr(receipt, "session_source", ""),
                            getattr(receipt, "session_trusted", False),
                        )
                    ),
                )
                self.store.upsert_evidence(evidence)
                ledger["evidence"] += 1
            self.store.upsert_source_link(
                share_group_id=group_id, memory_id=record.memory_id,
                source_revision=record.updated_at or record.created_at,
                original_definition_id=(
                    legacy_id if legacy_id != canonical_id else new_id
                ),
                canonical_definition_id=canonical_id,
            )
            # Alias only after this source's bindings, evidence and source
            # link are rebuilt.  If a later write fails, the v1 row and its
            # live audience remain recoverable for a retry.
            if legacy_id != canonical_id and legacy_id not in collision_targets:
                removed = self.store.migrate_legacy_definition(
                    legacy_id, canonical_id,
                )
                if removed is not None:
                    migrated_audiences[canonical_id] = removed
        # Only now alias the collided v1 row.  Any audience it carried is
        # already recreated from the legacy owner records, and any remaining
        # ambiguous evidence is handled conservatively by the normal
        # finalizer in one transaction.
        for legacy_id, mapping in collision_targets.items():
            primary_id = sorted(set(mapping.values()))[0]
            removed = self.store.migrate_legacy_definition(
                legacy_id, primary_id,
            )
            if removed and legacy_id not in collision_targets:
                migrated_audiences.setdefault(primary_id, []).extend(removed)
        # Scope preservation: every audience removed by a v1->v2 migration must
        # have been recreated under the canonical definition by this pass.
        if migrated_audiences:
            for new_id, removed in migrated_audiences.items():
                scope_definition_ids = {new_id}
                # Collision migration preserves audience globally across the
                # per-source target definitions; no target may inherit the
                # other source's audience by default.
                for mapping in collision_targets.values():
                    if new_id in set(mapping.values()):
                        scope_definition_ids.update(mapping.values())
                conn = self.store._active_write_conn()
                if conn is None:  # pragma: no cover - wrapper invariant
                    raise RuntimeError("v1_migration_transaction_missing")
                placeholders = ",".join("?" for _ in scope_definition_ids)
                present = {
                    binding_identity_key(self.store._row_to_binding(row))
                    for row in conn.execute(
                        "SELECT * FROM rule_bindings WHERE status='active' "
                        f"AND definition_id IN ({placeholders})",
                        sorted(scope_definition_ids),
                    ).fetchall()
                }
                for audience in removed:
                    if audience not in present:
                        raise RuntimeError(
                            "rule_migration_scope_change: an audience "
                            "disappeared during v1->v2 migration"
                        )
        return ledger

    def sync_rule(
        self,
        store: Any,
        group_id: str,
        record: SharedMemoryRecord,
        *,
        assignments: Iterable[Any] | None = None,
        receipts: Iterable[Any] | None = None,
        created_by: str = "auto",
        replace_assignments: bool = False,
    ) -> dict[str, Any]:
        """Dual-write: mirror one newly-created rule into the P3 layer (PR6).

        The source link is resolved first so a record whose rule was already
        merged/superseded writes its new bindings/evidence to the current
        canonical Definition instead of resurrecting the old lifecycle.  The
        P3-layer definition is only upserted when it is missing or active.
        Best-effort by design: a P3-layer failure must never block rule creation
        in the legacy store, so callers wrap this in try/except.
        """
        definition = self._definition_from_record(record)
        new_id = definition.definition_id
        link = self.store.get_source_link(group_id, record.memory_id)
        canonical_id = new_id
        if link and link.get("canonical_definition_id"):
            canonical_id = self.store.resolve_canonical(
                link["canonical_definition_id"],
            )
        record_status = getattr(record.status, "value", record.status)
        source_revision = (
            getattr(record, "updated_at", "")
            or getattr(record, "created_at", "")
            or (link or {}).get("source_revision", "")
            or ""
        )
        # A source contribution is owned by the source record, not by its
        # current owner.  Every inactive or non-governed source must therefore
        # retract only its own rows before the binding is materialized.
        if record_status != _ACTIVE_SOURCE_STATUS or (
            str(record.injection_policy or "") != "always"
        ):
            self.store.deactivate_source_contributions(
                group_id, record.memory_id,
                owner_agent_id=record.agent_instance_id,
            )
            self.store.deactivate_source_evidence(
                record.memory_id, record.agent_instance_id,
            )
            if record_status != _ACTIVE_SOURCE_STATUS or link:
                self.store.upsert_source_link(
                    share_group_id=group_id,
                    memory_id=record.memory_id,
                    source_revision=source_revision,
                    original_definition_id=(link or {}).get(
                        "original_definition_id", new_id,
                    ),
                    canonical_definition_id=canonical_id,
                    status=str(record_status or "deleted"),
                )
            return {
                "definition_id": canonical_id,
                "bindings": 0,
                "evidence": 0,
            }

        existing = self.store.get_definition(canonical_id)
        if (
            link
            and str(link.get("status") or "active") == _ACTIVE_SOURCE_STATUS
            and str(link.get("source_revision") or "")
            and str(link.get("source_revision") or "") != source_revision
        ):
            # Source updates retract the previous source-owned evidence and
            # runtime projection before the new revision is written.
            self.store.deactivate_source_evidence(
                record.memory_id, record.agent_instance_id,
            )
        # A source link may point at an already-merged canonical whose body
        # is authoritative.  Refresh only the source's own definition; never
        # project the source text over the canonical core during sync.
        if existing is None or (
            canonical_id == new_id and existing.status == "active"
        ):
            self.store.upsert_definition(definition)
        if assignments is None:
            try:
                assignments = store.list_rule_assignments(record.memory_id)
            except Exception:
                assignments = []
        assignment_items = list(assignments)
        bindings = len(assignment_items)
        writer = (
            self.store.replace_source_contributions
            if replace_assignments else self.store.upsert_source_contributions
        )
        writer(
            group_id,
            record.memory_id,
            [
                self._binding_from_assignment(
                    canonical_id, assignment, share_group_id=group_id,
                    owner_agent_id=record.agent_instance_id,
                    created_by=created_by,
                    authorization="dual-write",
                )
                for assignment in assignment_items
            ],
            source_revision=source_revision,
            owner_agent_id=record.agent_instance_id,
        )
        evidence = 0
        for receipt in receipts or []:
            item = build_evidence(
                definition_id=canonical_id,
                source_rule_id=record.memory_id,
                agent_instance_id=receipt.agent_instance_id,
                project_ref=receipt.project_ref,
                provider=receipt.provider,
                session_id=receipt.session_id,
                receipt_id=receipt.receipt_id,
                content=record.body,
                confidence=getattr(receipt, "confidence", 1.0),
                session_trusted=int(
                    session_trust_is_valid(
                        getattr(receipt, "session_id", ""),
                        getattr(receipt, "session_source", ""),
                        getattr(receipt, "session_trusted", False),
                    )
                ),
            )
            stored_item = self.store.upsert_evidence(item)
            evidence += 1
        self.store.upsert_source_link(
            share_group_id=group_id, memory_id=record.memory_id,
            source_revision=source_revision,
            original_definition_id=new_id,
            canonical_definition_id=canonical_id,
            status=_ACTIVE_SOURCE_STATUS,
        )
        return {
            "definition_id": canonical_id,
            "bindings": bindings,
            "evidence": evidence,
        }

    # ------------------------------------------------------------------
    # P2 -> P3 transactional outbox consumption (PR4)
    # ------------------------------------------------------------------

    def consume_outbox(
        self,
        workspace: str | Path | None = None,
        *,
        only_group: str | None = None,
        only_groups: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Drain the legacy outbox under the shared workspace governance lock."""
        with self.store.governance_lock():
            return self._consume_outbox_locked(
                workspace, only_group=only_group, only_groups=only_groups,
            )

    def _consume_outbox_locked(
        self,
        workspace: str | Path | None = None,
        *,
        only_group: str | None = None,
        only_groups: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Project P2 feedback events into the rule-intelligence layer.

        Feedback is written with its outbox row in one legacy-store transaction
        (``append_rule_match_feedback``), so no feedback can be missed.  Each
        event is resolved through the source link to the *current canonical*
        Definition (a merged rule's new evidence lands on the canonical, never
        resurrecting the merged definition), projected, and then checkpointed
        consumed.  Every projection step is idempotent, so re-delivery after a
        partial failure is safe.

        Outcome → projection:
          followed       -> positive runtime evidence
          violated       -> adherence signal (runtime stat only, never negative)
          not_applicable -> negative scope evidence
          exception      -> negative/exception evidence
          corrected      -> negative scope evidence when the evidence mentions scope
        """
        workspace = Path(workspace) if workspace is not None else self.store.workspace
        workspace = Path(workspace).resolve()
        from .shared_memory_store import SharedMemoryStore

        summary: dict[str, Any] = {
            "groups": 0, "events_seen": 0, "events_consumed": 0,
            "definitions_touched": 0,
        }
        selected_groups = (
            {str(group_id) for group_id in only_groups}
            if only_groups is not None else None
        )
        if only_group is not None:
            selected_groups = {str(only_group)}
        for group_id, _db_path in iter_legacy_groups(workspace):
            if selected_groups is not None and group_id not in selected_groups:
                continue
            legacy = SharedMemoryStore(workspace, group_id)
            events = legacy.list_unconsumed_rule_events()
            summary["groups"] += 1
            summary["events_seen"] += len(events)
            last_projected_event_id = ""
            try:
                for event in events:
                    if self._consume_feedback_event(legacy, group_id, event):
                        summary["events_consumed"] += 1
                        summary["definitions_touched"] += 1
                        # Checkpoint only after the P3 projection commits. Any
                        # projection/checkpoint error must escape so scan
                        # callers cannot continue on stale governance data.
                        legacy.mark_rule_event_consumed(event["event_id"])
                        last_projected_event_id = str(event["event_id"] or "")
            except Exception as exc:
                remaining = len(legacy.list_unconsumed_rule_events())
                self.store.set_projection_state(
                    group_id,
                    last_outbox_event_id=(
                        str(events[-1]["event_id"] or "") if events else ""
                    ),
                    last_projected_event_id=last_projected_event_id,
                    projection_lag=remaining,
                    projection_error=f"{type(exc).__name__}: {exc}",
                )
                raise
            remaining = len(legacy.list_unconsumed_rule_events())
            self.store.set_projection_state(
                group_id,
                last_outbox_event_id=(
                    str(events[-1]["event_id"] or "") if events else ""
                ),
                last_projected_event_id=last_projected_event_id,
                projection_lag=remaining,
                projection_error="",
            )
        return summary

    @staticmethod
    def _feedback_event_state(
        event: dict[str, Any], side: str,
    ) -> dict[str, Any]:
        """Normalize nested and flattened effective-feedback event shapes."""
        raw = event.get(side)
        state = dict(raw) if isinstance(raw, dict) else {}
        if isinstance(state.get("receipt"), dict):
            nested = dict(state["receipt"])
            nested.update(state)
            state = nested
        if side == "previous":
            aliases = {
                "feedback_id": "previous_effective_feedback_id",
                "outcome": "previous_outcome",
            }
        else:
            aliases = {
                "feedback_id": "new_effective_feedback_id",
                "outcome": "new_outcome",
            }
        for key, event_key in aliases.items():
            if key not in state and event_key in event:
                state[key] = event.get(event_key)
        if side == "new":
            # The legacy event fields are aliases for the new effective state.
            for key in (
                "feedback_id", "outcome", "source", "authority", "actor",
                "evidence", "confidence", "created_at",
            ):
                if key not in state and key in event:
                    state[key] = event.get(key)
        # Receipt/session identity is common to both sides in the outbox row,
        # but a newer producer may place it inside each state.
        for key in (
            "receipt_id", "memory_id", "share_group_id", "agent_instance_id",
            "project_ref", "session_id", "provider", "session_trusted",
            "session_source",
        ):
            if key not in state and key in event:
                state[key] = event.get(key)
        return state

    @staticmethod
    def _session_trusted_value(
        state: dict[str, Any], event: dict[str, Any],
    ) -> int:
        """Read trust provenance; absent/invalid values fail closed to 0."""
        raw = state.get("session_trusted")
        if raw is None:
            raw = event.get("session_trusted")
        if isinstance(raw, bool):
            trusted = raw
        elif isinstance(raw, (int, float)):
            trusted = raw == 1
        elif isinstance(raw, str):
            trusted = raw.strip().casefold() in {"1", "true", "yes"}
        else:
            trusted = False
        session_id = str(
            state.get("session_id") or event.get("session_id") or ""
        ).strip()
        source = str(
            state.get("session_source") or event.get("session_source") or ""
        ).strip().casefold()
        return int(session_trust_is_valid(session_id, source, trusted))

    def _clear_feedback_projection(
        self, receipt_id: str, previous_feedback_id: str = "",
    ) -> set[str]:
        """Retract all P3 artifacts currently attributable to one receipt."""
        if not receipt_id:
            return set()
        projection = self.store.get_effective_feedback_projection(receipt_id)
        touched: set[str] = set()
        if projection:
            definition_id = str(projection.get("definition_id") or "")
            if definition_id:
                touched.add(definition_id)
            self.store.clear_evidence_projection(receipt_id)

        # Also clean rows written before the projection ledger existed.  This
        # keeps a first effective-state event from leaving stale evidence.
        for definition in self.store.list_definitions():
            definition_id = str(definition.definition_id)
            for item in self.store.list_evidence(definition_id):
                if str(getattr(item, "receipt_id", "") or "") == receipt_id:
                    self.store.delete_evidence(item.evidence_id)
                    touched.add(definition_id)
            for item in self.store.list_negative_evidence(definition_id):
                if str(getattr(item, "receipt_id", "") or "") == receipt_id:
                    self.store.delete_negative_evidence(item.evidence_id)
                    touched.add(definition_id)

        # The flattened outbox carries the previous feedback id even when a
        # projection row was lost.  It is the only safe runtime row to retract.
        if previous_feedback_id:
            self.store.delete_runtime_feedback(previous_feedback_id)
        for definition_id in touched:
            self.store.recompute_runtime_stats(definition_id)
        return touched

    @staticmethod
    def _feedback_independence_key(
        *,
        share_group_id: str,
        memory_id: str,
        receipt_id: str,
        agent_instance_id: str,
        project_ref: str,
        session_id: str,
    ) -> str:
        """Use one polarity-neutral group for a receipt's evidence history."""
        return stable_hash(
            "rule-feedback-independence", share_group_id, memory_id,
            receipt_id, agent_instance_id, project_ref, session_id,
        )

    def _feedback_contribution(
        self,
        *,
        definition_id: str,
        memory_id: str,
        record: SharedMemoryRecord,
        state: dict[str, Any],
        event: dict[str, Any],
        receipt_id: str,
        feedback_id: str,
        share_group_id: str,
        agent: str,
        project: str,
        session: str,
        created_at: str,
        authority: int,
        confidence: float,
        session_trusted: int,
        polarity: str,
        source_evidence_id: str,
    ) -> Any:
        independence_key = self._feedback_independence_key(
            share_group_id=share_group_id, memory_id=memory_id,
            receipt_id=receipt_id, agent_instance_id=agent,
            project_ref=project, session_id=session,
        )
        return build_contribution(
            contribution_id=stable_hash(
                "rule-feedback-contribution", definition_id,
                independence_key, feedback_id, polarity,
            ),
            definition_id=definition_id,
            independence_key=independence_key,
            kind="feedback",
            polarity=polarity,
            authority=authority,
            confidence=confidence,
            observed_at=created_at,
            receipt_id=receipt_id,
            feedback_id=feedback_id,
            source_rule_id=memory_id,
            source_evidence_id=source_evidence_id,
            source_memory_id=memory_id,
            source_ids={
                "receipt_id": receipt_id,
                "feedback_id": feedback_id,
                "source_rule_id": memory_id,
            },
            agent_instance_id=agent,
            project_ref=project,
            share_group_id=share_group_id,
            session_id=session,
            session_trusted=bool(session_trusted),
        )

    def _consume_lifecycle_event(
        self, legacy: Any, group_id: str, event: dict[str, Any],
    ) -> bool:
        """Project a legacy rule mutation before checkpointing its outbox row."""
        event_type = str(event.get("event_type") or "")
        payload = event.get("evidence")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except (TypeError, ValueError):
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        target_ids = [str(event.get("memory_id") or "")]
        for key in (
            "target_ids", "parent_rule_id", "child_rule_id",
            "descendants_shadowed",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                target_ids.extend(str(item) for item in value if str(item))
            elif value:
                target_ids.append(str(value))
        target_ids = list(dict.fromkeys(item for item in target_ids if item))
        for memory_id in target_ids:
            record = legacy.get_record(memory_id)
            link = self.store.get_source_link(group_id, memory_id)
            if record is None or record.status == SharedMemoryStatus.DELETED:
                self.store.deactivate_source_contributions(group_id, memory_id)
                self.store.deactivate_source_evidence(memory_id)
                canonical_id = self.store.resolve_canonical(
                    str(link.get("canonical_definition_id") or "")
                ) if link else ""
                if canonical_id or link:
                    self.store.upsert_source_link(
                        share_group_id=group_id,
                        memory_id=memory_id,
                        source_revision=(
                            (
                                record.updated_at or record.created_at
                                if record is not None else ""
                            )
                            or (link or {}).get("source_revision", "")
                        ),
                        original_definition_id=(link or {}).get(
                            "original_definition_id", ""
                        ),
                        canonical_definition_id=canonical_id,
                        status="deleted",
                    )
                continue

            record_status = getattr(record.status, "value", record.status)
            if record_status != _ACTIVE_SOURCE_STATUS:
                self.store.deactivate_source_contributions(
                    group_id, memory_id,
                    owner_agent_id=record.agent_instance_id,
                )
                self.store.deactivate_source_evidence(
                    memory_id, record.agent_instance_id,
                )
                definition = self._definition_from_record(record)
                canonical_id = (
                    self.store.resolve_canonical(
                        str(link.get("canonical_definition_id") or "")
                    )
                    if link and link.get("canonical_definition_id")
                    else definition.definition_id
                )
                if self.store.get_definition(canonical_id) is None:
                    self.store.upsert_definition(definition)
                self.store.upsert_source_link(
                    share_group_id=group_id,
                    memory_id=memory_id,
                    source_revision=(
                        record.updated_at or record.created_at
                        or (link or {}).get("source_revision", "")
                    ),
                    original_definition_id=(link or {}).get(
                        "original_definition_id", definition.definition_id,
                    ),
                    canonical_definition_id=canonical_id,
                    status=str(record_status),
                )
                continue
            assignments = legacy.list_rule_assignments(memory_id)
            try:
                receipts = legacy.list_rule_match_receipts(memory_id=memory_id)
            except Exception:
                receipts = []
            self.sync_rule(
                legacy,
                group_id,
                record,
                assignments=assignments,
                receipts=receipts,
                created_by="outbox",
                replace_assignments=event_type in {
                    "assignments_replaced", "assignment_changed",
                },
            )
        return True

    def _consume_feedback_event(
        self, legacy: Any, group_id: str, event: dict[str, Any],
    ) -> bool:
        """Reconcile one effective receipt event; checkpoint happens outside."""
        is_effective = str(event.get("event_type") or "") == (
            "effective_rule_feedback_changed"
        )
        if not is_effective and str(event.get("event_type") or "") in {
            "rule_created", "rule_updated", "rule_deleted", "rule_restored",
            "rule_quarantined", "rule_conflicted", "rule_superseded",
            "rule_undo", "assignments_replaced", "assignment_changed",
            "rule_narrowed",
            "exception_split", "exception_revoked",
        }:
            return self._consume_lifecycle_event(legacy, group_id, event)
        previous = self._feedback_event_state(event, "previous")
        current = self._feedback_event_state(event, "new")
        receipt_id = str(
            current.get("receipt_id") or previous.get("receipt_id")
            or event.get("receipt_id") or ""
        )
        previous_feedback_id = str(previous.get("feedback_id") or "")
        if is_effective and not receipt_id:
            raise ValueError("effective_rule_feedback_receipt_id_required")

        touched = self._clear_feedback_projection(
            receipt_id, previous_feedback_id,
        ) if receipt_id else set()

        state = current if is_effective else event
        memory_id = str(state.get("memory_id") or event.get("memory_id") or "")
        outcome = str(state.get("outcome") or event.get("outcome") or "")
        feedback_id = str(
            state.get("feedback_id") or event.get("feedback_id") or ""
        )
        inactive_outcomes = {
            "", "ignored", "unobserved", "cleared", "deleted", "tombstone",
        }
        active = bool(
            memory_id and feedback_id and outcome not in inactive_outcomes
        )
        if is_effective and outcome in inactive_outcomes:
            active = False

        record = legacy.get_record(memory_id) if active else None
        if active and record is None:
            # Source deletion is a revocation from P3's point of view.  Keep a
            # durable tombstone instead of resurrecting a Definition.
            active = False

        canonical_id = ""
        if active and record is not None:
            definition = self._definition_from_record(record)
            canonical_id = definition.definition_id
            link = self.store.get_source_link(group_id, memory_id)
            if link and link.get("canonical_definition_id"):
                canonical_id = link["canonical_definition_id"]
            # Follow the alias/merged/superseded chain: feedback on a source
            # whose definition was merged lands on its current canonical.
            canonical_id = self.store.resolve_canonical(canonical_id)
            if self.store.get_definition(canonical_id) is None:
                self.store.upsert_definition(definition)
        elif touched:
            canonical_id = sorted(touched)[0]

        if not active:
            if receipt_id:
                self.store.upsert_effective_feedback_projection(
                    receipt_id=receipt_id,
                    effective_feedback_id="",
                    definition_id=canonical_id,
                    outcome="tombstone",
                    session_id=str(
                        current.get("session_id")
                        or event.get("session_id") or ""
                    ),
                    session_trusted=self._session_trusted_value(
                        current, event,
                    ),
                    session_source=str(
                        current.get("session_source")
                        or event.get("session_source") or "absent"
                    ),
                )
            for definition_id in touched:
                if definition_id != canonical_id:
                    self.store.recompute_runtime_stats(definition_id)
            return True

        agent = str(state.get("agent_instance_id") or event.get(
            "agent_instance_id", ""
        ))
        project = str(state.get("project_ref") or event.get("project_ref", ""))
        session = str(state.get("session_id") or event.get("session_id", ""))
        created_at = str(state.get("created_at") or event.get(
            "created_at", ""
        )) or _now_iso()
        raw_confidence = state.get("confidence")
        if raw_confidence is None:
            raw_confidence = event.get("confidence")
        try:
            confidence = float(
                1.0 if raw_confidence is None else raw_confidence,
            )
        except (TypeError, ValueError):
            confidence = 1.0
        provider = str(state.get("provider") or event.get("provider", ""))
        evidence_text = str(state.get("evidence") or event.get("evidence", ""))
        share_group_id = str(
            state.get("share_group_id") or event.get("share_group_id") or group_id
        )
        authority = int(state.get("authority") or event.get("authority") or 0)
        session_trusted = self._session_trusted_value(state, event)
        session_source = str(
            state.get("session_source") or event.get("session_source")
            or "absent"
        )
        positive_evidence_id = ""
        negative_evidence_id = ""
        if outcome == "followed":
            item = build_evidence(
                definition_id=canonical_id, source_rule_id=memory_id,
                agent_instance_id=agent, project_ref=project,
                session_id=session, receipt_id=receipt_id, provider=provider,
                content=record.body, confidence=confidence, observed_at=created_at,
                share_group_id=share_group_id, session_trusted=session_trusted,
                feedback_id=feedback_id, feedback_authority=authority,
            )
            contribution = self._feedback_contribution(
                definition_id=canonical_id, memory_id=memory_id,
                record=record, state=state, event=event,
                receipt_id=receipt_id, feedback_id=feedback_id,
                share_group_id=share_group_id, agent=agent, project=project,
                session=session, created_at=created_at, authority=authority,
                confidence=confidence, session_trusted=session_trusted,
                polarity="positive", source_evidence_id=item.evidence_id,
            )
            stored_item = self.store.upsert_evidence_contribution(contribution)
            if (
                str(getattr(stored_item, "receipt_id", "") or "") == receipt_id
                and str(getattr(stored_item, "feedback_id", "") or "") == feedback_id
            ):
                positive_evidence_id = stored_item.source_evidence_id
        elif outcome in {"not_applicable", "exception"}:
            item = build_negative_evidence(
                definition_id=canonical_id, source_rule_id=memory_id,
                agent_instance_id=agent, project_ref=project,
                content=record.body, confidence=confidence, observed_at=created_at,
                share_group_id=share_group_id, session_id=session,
                receipt_id=receipt_id, feedback_id=feedback_id,
                feedback_authority=authority, session_trusted=session_trusted,
            )
            contribution = self._feedback_contribution(
                definition_id=canonical_id, memory_id=memory_id,
                record=record, state=state, event=event,
                receipt_id=receipt_id, feedback_id=feedback_id,
                share_group_id=share_group_id, agent=agent, project=project,
                session=session, created_at=created_at, authority=authority,
                confidence=confidence, session_trusted=session_trusted,
                polarity="negative", source_evidence_id=item.evidence_id,
            )
            stored_item = self.store.upsert_evidence_contribution(contribution)
            if (
                str(getattr(stored_item, "receipt_id", "") or "") == receipt_id
                and str(getattr(stored_item, "feedback_id", "") or "") == feedback_id
            ):
                negative_evidence_id = stored_item.source_evidence_id
        elif outcome == "corrected":
            lowered = evidence_text.casefold()
            if any(
                marker in lowered
                for marker in ("scope", "范围", "not applicable", "不适用")
            ):
                item = build_negative_evidence(
                    definition_id=canonical_id, source_rule_id=memory_id,
                    agent_instance_id=agent, project_ref=project,
                    content=record.body, confidence=confidence,
                    observed_at=created_at, share_group_id=share_group_id,
                    session_id=session, receipt_id=receipt_id,
                    feedback_id=feedback_id, feedback_authority=authority,
                    session_trusted=session_trusted,
                )
                contribution = self._feedback_contribution(
                    definition_id=canonical_id, memory_id=memory_id,
                    record=record, state=state, event=event,
                    receipt_id=receipt_id, feedback_id=feedback_id,
                    share_group_id=share_group_id, agent=agent, project=project,
                    session=session, created_at=created_at, authority=authority,
                    confidence=confidence, session_trusted=session_trusted,
                    polarity="negative", source_evidence_id=item.evidence_id,
                )
                stored_item = self.store.upsert_evidence_contribution(contribution)
                if (
                    str(getattr(stored_item, "receipt_id", "") or "") == receipt_id
                    and str(getattr(stored_item, "feedback_id", "") or "") == feedback_id
                ):
                    negative_evidence_id = stored_item.source_evidence_id
        # Every observed outcome feeds the idempotent runtime-feedback ledger;
        # counters are derived in recompute_runtime_stats, never incremented.
        self.store.upsert_runtime_feedback(
            feedback_id=feedback_id, definition_id=canonical_id,
            receipt_id=receipt_id, outcome=outcome,
            agent_instance_id=agent, project_ref=project, session_id=session,
            source=str(state.get("source") or event.get("source") or ""),
            authority=authority, session_trusted=session_trusted,
            created_at=created_at,
        )
        self.store.recompute_runtime_stats(canonical_id)
        if receipt_id:
            self.store.upsert_effective_feedback_projection(
                receipt_id=receipt_id,
                effective_feedback_id=feedback_id,
                definition_id=canonical_id,
                outcome=outcome,
                positive_evidence_id=positive_evidence_id,
                negative_evidence_id=negative_evidence_id,
                session_id=session,
                session_trusted=session_trusted,
                session_source=session_source,
            )
        return True

    # ------------------------------------------------------------------
    # Duplicate detection → proposals
    # ------------------------------------------------------------------

    def scan_and_propose(
        self,
        *,
        min_score: float = AUTO_MERGE_SCORE,
        min_evidence: int = AUTO_MERGE_MIN_EVIDENCE,
        min_agents: int = AUTO_MERGE_MIN_AGENTS,
        min_projects: int = AUTO_MERGE_MIN_PROJECTS,
        definition_ids: list[str] | None = None,
        judge: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Find duplicate-active Definition pairs and persist merge proposals.

        Every pair whose candidate fails is still recorded as a proposal with
        its reasons, so the governance UI can show why a merge was refused:

          * ``candidate``  — all hard safety conditions hold (P3 §5 + strength +
            negative evidence); automatic merge still needs the soft governance
            gates (readiness / first-merge acknowledgment / cooldown).
          * ``conflicted`` — a strength conflict: same intent, different rule
            strength.  Never mergeable, needs human resolution.
          * ``rejected``  — any other hard failure (similarity/polarity/params/
            evidence/contradiction).

        Maturity is recomputed before scanning so the readiness snapshot is
        fresh.
        """
        judge = judge if judge is not None else self.judge
        # Project any pending P2 feedback before scanning so the evidence and
        # maturity snapshot is fresh.  Projection failures are deliberate
        # fail-closed errors: proposing against stale feedback is unsafe.
        relevant_groups = (
            self.store.groups_for_definitions(definition_ids)
            if definition_ids is not None else None
        )
        self.consume_outbox(
            self.store.workspace,
            only_groups=relevant_groups,
        )
        candidates = self.store.list_definitions(status="active")
        if definition_ids:
            wanted = set(definition_ids)
            candidates = [d for d in candidates if d.definition_id in wanted]
        for definition in candidates:
            self._refresh_maturity(definition)
        candidates = self.store.list_definitions(status="active")
        if definition_ids:
            wanted = set(definition_ids)
            candidates = [d for d in candidates if d.definition_id in wanted]

        proposals: list[dict[str, Any]] = []
        # PR7 bounded scan: recall by kind/strength/polarity/parameters and
        # bounded character blocking, not semantic_hash alone.  Near-meaningful
        # pairs with different deterministic hashes must remain discoverable.
        candidate_pairs = self._candidate_pairs(candidates)
        pairs_evaluated = 0
        pairs_skipped = 0
        rejected_persisted = 0
        for members in candidate_pairs:
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    # Deterministic pair order so the proposal's stored
                    # definition_ids and its captured revisions stay aligned.
                    if a.definition_id > b.definition_id:
                        a, b = b, a
                    pairs_evaluated += 1
                    negative_score = self._negative_score(a, b)
                    assessment = self._assess_pair(
                        a, b, min_score=min_score, min_evidence=min_evidence,
                        min_agents=min_agents, min_projects=min_projects,
                        negative_score=negative_score, judge=judge,
                    )
                    governance = self._proposal_governance(
                        a, b, negative_score=negative_score,
                    )
                    if assessment.hard_gates_ok:
                        status = "candidate"
                        conflict_type = ""
                        # P3-001 §4: a freshly-eligible candidate enters a 72h
                        # cooldown during which evidence can only accumulate.
                        governance["cooldown_until"] = (
                            datetime.fromisoformat(_now_iso())
                            + timedelta(hours=COOLDOWN_HOURS)
                        ).isoformat()
                    elif assessment.conflict_type == "strength":
                        status = "conflicted"
                        conflict_type = "strength"
                    elif assessment.duplicate_score < REJECTED_PERSIST_FLOOR:
                        # Ordinary rejection: count it, don't persist another
                        # near-identical row the governance UI will never use.
                        pairs_skipped += 1
                        continue
                    else:
                        status = "rejected"
                        conflict_type = assessment.conflict_type
                        rejected_persisted += 1
                    proposal = self.store.create_proposal(
                        [a.definition_id, b.definition_id],
                        assessment.duplicate_score,
                        evidence=self._combined_evidence(a, b),
                        contradiction_score=contradiction_score(a, b),
                        explanation="; ".join(assessment.reasons),
                        readiness_score=governance["readiness_score"],
                        readiness_components=governance.get(
                            "readiness_components", {}
                        ),
                        readiness_digest=governance.get(
                            "readiness_digest", ""
                        ),
                        governance_reasons="; ".join(
                            governance["governance_reasons"],
                        ),
                        cooldown_until=governance["cooldown_until"],
                        negative_score=governance["negative_score"],
                        conflict_type=conflict_type,
                        judge=assessment.judge,
                        definition_a=a,
                        definition_b=b,
                        weight_breakdown=governance.get(
                            "weight_breakdown", "",
                        ),
                    )
                    self.store.set_proposal_status(
                        proposal["proposal_id"], status,
                    )
                    proposals.append(
                        self.store.get_proposal(proposal["proposal_id"]),
                    )
        self.last_scan_summary = {
            "pairs_evaluated": pairs_evaluated,
            "pairs_skipped": pairs_skipped,
            "rejected_persisted": rejected_persisted,
            "proposals_persisted": len(proposals),
        }
        return proposals

    def _candidate_pairs(
        self, definitions: list[RuleDefinition],
    ) -> list[tuple[RuleDefinition, RuleDefinition]]:
        """Recall compatible pairs with bounded deterministic top-k blocking.

        The primary key keeps kind/strength/polarity/parameters aligned.  A
        secondary key intentionally crosses strength only so strength conflicts
        remain auditable.  This avoids the old semantic-hash-only blind spot
        while keeping large compatible populations bounded.
        """
        primary: dict[tuple[Any, ...], list[RuleDefinition]] = {}
        strength_conflicts: dict[tuple[Any, ...], list[RuleDefinition]] = {}
        for definition in definitions:
            params = tuple(sorted(parameters_of(definition)))
            kind = str(definition.rule_kind or "")
            polarity = str(definition.polarity or "")
            strength = str(definition.rule_strength or "")
            primary.setdefault(
                (kind, strength, polarity, params), [],
            ).append(definition)
            strength_conflicts.setdefault(
                (kind, polarity, params), [],
            ).append(definition)

        pair_ids: set[tuple[str, str]] = set()
        for members in primary.values():
            pair_ids.update(self._bounded_pair_ids(members))
        for members in strength_conflicts.values():
            pair_ids.update(
                self._bounded_pair_ids(members, cross_strength=True),
            )
        by_id = {d.definition_id: d for d in definitions}
        return [
            (by_id[left], by_id[right])
            for left, right in sorted(pair_ids)
            if left in by_id and right in by_id
        ]

    @staticmethod
    def _bounded_pair_ids(
        members: list[RuleDefinition], *, cross_strength: bool = False,
    ) -> set[tuple[str, str]]:
        """Return all small-group pairs or character-block top-k pairs."""
        ordered = sorted(members, key=lambda d: d.definition_id)
        pairs: set[tuple[str, str]] = set()

        def add(left: RuleDefinition, right: RuleDefinition) -> None:
            if cross_strength and str(left.rule_strength or "") == str(
                right.rule_strength or "",
            ):
                return
            pair = tuple(sorted((left.definition_id, right.definition_id)))
            if pair[0] != pair[1]:
                pairs.add(pair)

        if len(ordered) <= SCAN_EXHAUSTIVE_GROUP_LIMIT:
            for index, left in enumerate(ordered):
                for right in ordered[index + 1:]:
                    add(left, right)
            return pairs

        surfaces = {
            d.definition_id: semantic_surface(d.canonical_text)
            for d in ordered
        }
        grams = {
            d.definition_id: char_bigram_set(surfaces[d.definition_id])
            for d in ordered
        }
        inverted: dict[str, list[str]] = {}
        for definition in ordered:
            for gram in grams[definition.definition_id]:
                inverted.setdefault(gram, []).append(definition.definition_id)

        by_id = {d.definition_id: d for d in ordered}
        for left in ordered:
            left_id = left.definition_id
            overlap: dict[str, int] = {}
            for gram in grams[left_id]:
                for right_id in inverted.get(gram, [])[:SCAN_POSTING_LIMIT]:
                    if right_id != left_id:
                        overlap[right_id] = overlap.get(right_id, 0) + 1
            # A rare no-shared-gram case still gets a bounded deterministic
            # neighborhood; the full policy assessment will fail closed.
            if not overlap:
                ranked_ids: list[str] = []
                for right in ordered:
                    if right.definition_id == left_id:
                        continue
                    if cross_strength and str(right.rule_strength or "") == str(
                        left.rule_strength or "",
                    ):
                        continue
                    ranked_ids.append(right.definition_id)
                    if len(ranked_ids) >= SCAN_TOP_K:
                        break
            else:
                ranked_ids = [
                    right_id
                    for right_id, _score in sorted(
                        overlap.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:SCAN_TOP_K]
                ]
            for right_id in ranked_ids:
                add(left, by_id[right_id])
        return pairs

    def merge_proposal(
        self, proposal_id: str, *, actor: str = "auto", judge: Any | None = None,
    ) -> dict[str, Any]:
        """Evaluate and execute one merge proposal.

        Returns the merge decision on success, or a blocked result carrying the
        assessment/governance reasons.  Two paths exist:

          * **human** (actor in ``HUMAN_ACTORS`` or proposal already
            ``approved``): the human reviewed the risk, so the soft governance
            gates (readiness, cooldown, first-merge acknowledgment) are
            bypassed — but the hard safety gates (similarity, polarity,
            parameters, strength, negative evidence) are *never* bypassed, and
            the store's scope-invariance transaction still refuses any merge
            that would change a binding's audience.
          * **auto**: every hard gate AND the readiness score, cooldown and
            first-merge acknowledgment must hold.
        """
        judge = judge if judge is not None else self.judge
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError("rule_merge_proposal_not_found")
        definition_ids = proposal["definition_ids"]
        if len(definition_ids) != 2:
            raise ValueError("rule_merge_proposal_must_pair_two_definitions")
        a, b = (
            self.store.get_definition(definition_ids[0]),
            self.store.get_definition(definition_ids[1]),
        )
        if a is None or b is None:
            raise ValueError("rule_merge_definition_not_found")

        status = str(proposal.get("status", "") or "")
        # The human path is granted by a *first-class approval record*, never by
        # the actor string: merge_proposal(actor="admin") is no longer an
        # approval by itself.
        approval = None
        if status == "approved":
            approval = self.store.get_valid_approval(proposal_id)
        human_path = status == "approved" and approval is not None
        if status not in {"candidate", "approved"}:
            self.store.set_proposal_status(proposal_id, "rejected")
            return {
                "ok": False,
                "blocked_reason": "rule_merge_proposal_not_approved",
                "conflict_type": proposal.get("conflict_type") or "",
            }
        if status == "approved" and not human_path:
            return {
                "ok": False,
                "blocked_reason": "rule_merge_approval_required",
                "conflict_type": proposal.get("conflict_type") or "",
            }

        # Recompute lifecycle and every soft gate at execution time.  The
        # proposal snapshot is advisory; runtime feedback or maturity may have
        # changed since scan/approval.
        self._refresh_maturity(a)
        self._refresh_maturity(b)
        negative_score = self._negative_score(a, b)
        judge_verdict = getattr(
            compute_layers(a, b, judge=judge), "judge", None,
        )
        governance = self._proposal_governance(
            a, b, proposal=proposal, negative_score=negative_score,
        )

        if human_path:
            # Human approval relaxes the *evidence and similarity* thresholds,
            # but the governance conflicts — strength (P3-002), negative
            # evidence (P3-001 §5), polarity, parameters and contradiction —
            # still block even a human-approved merge.  An ``unknown`` strength
            # is likewise never mergeable.
            current_layers = compute_layers(a, b, judge=judge)
            if current_layers.duplicate_score < HUMAN_MERGE_MIN_SIMILARITY:
                self.store.set_proposal_status(proposal_id, "rejected")
                return {
                    "ok": False,
                    "blocked_reason": "merge_similarity_gate_failed",
                    "conflict_type": "similarity",
                    "assessment": {
                        "duplicate_score": current_layers.duplicate_score,
                        "reasons": ["similarity_below_human_threshold"],
                    },
                }
            strength_ok = (
                str(a.rule_strength or "") == str(b.rule_strength or "")
                and str(a.rule_strength or "") != STRENGTH_UNKNOWN
            )
            polarity_ok = a.polarity == b.polarity
            params_ok = not parameter_conflict(a, b)
            contradiction_ok = (
                contradiction_score(a, b) <= 0
            )
            negative_ok = negative_score < NEGATIVE_EVIDENCE_THRESHOLD
            if not strength_ok:
                self.store.set_proposal_status(proposal_id, "conflicted")
                return {
                    "ok": False,
                    "blocked_reason": "merge_safety_evaluation_failed",
                    "conflict_type": "strength",
                    "assessment": {"reasons": ["strength_conflict"]},
                }
            if not negative_ok:
                self.store.set_proposal_status(proposal_id, "rejected")
                return {
                    "ok": False,
                    "blocked_reason": "merge_safety_evaluation_failed",
                    "conflict_type": "negative_evidence",
                    "assessment": {"reasons": ["negative_evidence"]},
                }
            if not (polarity_ok and params_ok and contradiction_ok):
                reasons = []
                if not polarity_ok:
                    reasons.append("polarity_conflict")
                if not params_ok:
                    reasons.append("parameter_conflict")
                if not contradiction_ok:
                    reasons.append("contradiction")
                conflict_type = "polarity" if not polarity_ok else (
                    "parameter" if not params_ok else "contradiction"
                )
                self.store.set_proposal_status(proposal_id, "rejected")
                return {
                    "ok": False,
                    "blocked_reason": "merge_safety_evaluation_failed",
                    "conflict_type": conflict_type,
                    "assessment": {"reasons": reasons},
                }
        else:
            # Automatic path: every hard safety condition must hold (P3 §5 +
            # strength + negative evidence).
            assessment = evaluate_candidate(
                a, b,
                evidence=self._combined_evidence(a, b),
                min_score=AUTO_MERGE_SCORE,
                min_evidence=AUTO_MERGE_MIN_EVIDENCE,
                min_agents=AUTO_MERGE_MIN_AGENTS,
                min_projects=AUTO_MERGE_MIN_PROJECTS,
                negative_score=negative_score,
                judge=judge,
            )
            if not assessment.hard_gates_ok:
                new_status = (
                    "conflicted"
                    if assessment.conflict_type == "strength" else "rejected"
                )
                self.store.set_proposal_status(proposal_id, new_status)
                return {
                    "ok": False,
                    "blocked_reason": "merge_safety_evaluation_failed",
                    "conflict_type": assessment.conflict_type,
                    "assessment": {
                        "duplicate_score": assessment.duplicate_score,
                        "reasons": list(assessment.reasons),
                    },
                }
            strength_ok = assessment.strength_ok
            negative_ok = assessment.negative_ok

        if not human_path and not governance["eligible"]:
            # Soft governance gate blocked the automatic merge.  Stay a
            # candidate so evidence keeps collecting; record why.
            self.store.update_proposal_governance(
                proposal_id,
                readiness_score=governance["readiness_score"],
                readiness_components=governance.get("readiness_components", {}),
                readiness_digest=governance.get("readiness_digest", ""),
                governance_reasons="; ".join(governance["governance_reasons"]),
                cooldown_until=governance["cooldown_until"],
                negative_score=negative_score,
            )
            return {
                "ok": False,
                "blocked_reason": "auto_merge_not_ready",
                "readiness_score": governance["readiness_score"],
                "auto_ready_threshold": AUTO_READINESS_SCORE,
                "governance_reasons": governance["governance_reasons"],
            }

        # Persist the freshly recomputed soft-gate evidence on both paths.  A
        # human approval may override these automatic gates, but it must not
        # erase the reasons that were reviewed.
        self.store.update_proposal_governance(
            proposal_id,
            readiness_score=governance["readiness_score"],
            readiness_components=governance.get("readiness_components", {}),
            readiness_digest=governance.get("readiness_digest", ""),
            governance_reasons="; ".join(governance["governance_reasons"]),
            cooldown_until=governance["cooldown_until"],
            negative_score=negative_score,
        )
        proposal = self.store.get_proposal(proposal_id) or proposal
        canonical, merged = self._pick_canonical(a, b)
        # For a human-approved merge, pin the merge to the exact state the
        # approver reviewed: definition revisions + evidence digest captured at
        # the last scan/approval, and the approval id itself.  The store
        # re-verifies all of these and the hard gates inside its transaction.
        expected_revisions = None
        expected_digest = str(proposal.get("evidence_digest") or "")
        approval_id = ""
        if approval is not None:
            approval_id = approval.get("approval_id", "")
            approved_revisions = dict(
                approval.get("expected_definition_revisions") or {}
            )
            if approved_revisions:
                expected_revisions = {
                    str(key): int(value)
                    for key, value in approved_revisions.items()
                }
            else:
                rev_a = int(proposal.get("definition_revision_a") or 0)
                rev_b = int(proposal.get("definition_revision_b") or 0)
                expected_revisions = {
                    str(a.definition_id): rev_a or int(a.revision or 0),
                    str(b.definition_id): rev_b or int(b.revision or 0),
                }
        elif proposal.get("definition_revision_a") or proposal.get(
            "definition_revision_b"
        ):
            expected_revisions = {
                str(a.definition_id): int(
                    proposal.get("definition_revision_a") or a.revision or 0
                ),
                str(b.definition_id): int(
                    proposal.get("definition_revision_b") or b.revision or 0
                ),
            }
        if expected_revisions is None:
            expected_revisions = {
                str(a.definition_id): int(a.revision or 0),
                str(b.definition_id): int(b.revision or 0),
            }
        execution_mode = "human-approved" if human_path else "auto"
        execute_kwargs: dict[str, Any] = {
            "proposal_id": proposal_id,
            "canonical_definition_id": canonical.definition_id,
            "merged_definition_ids": [merged.definition_id],
            "actor": actor,
            "readiness_at_merge": governance["readiness_score"],
            "strength_ok": strength_ok,
            "negative_ok": negative_ok,
            "first_merge_acknowledged": (
                bool(proposal.get("first_merge_acknowledged"))
                or human_path
            ),
            "judge": judge_verdict,
            "approval_id": approval_id,
            "execution_mode": execution_mode,
            "expected_definition_revisions": expected_revisions,
            "expected_evidence_digest": expected_digest,
            "expected_negative_digest": proposal.get("negative_digest", ""),
            "expected_binding_digest": proposal.get("binding_digest", ""),
            "expected_runtime_digest": proposal.get("runtime_digest", ""),
            "expected_readiness_digest": proposal.get("readiness_digest", ""),
            "expected_assessment_revision": (
                int(proposal.get("assessment_revision") or 0) or None
            ),
            "expected_policy_version": proposal.get("policy_version", ""),
        }
        # Keep compatibility with older Store implementations while placing
        # the actual merge behind the workspace drain/high-water barrier.
        supported = inspect.signature(self.store.execute_merge).parameters
        merge_error: list[BaseException] = []

        def execute_current_merge(_snapshot: Any = None) -> Any:
            try:
                return self.store.execute_merge(
                    **{
                        key: value for key, value in execute_kwargs.items()
                        if key in supported
                    }
                )
            except BaseException as exc:
                merge_error.append(exc)
                raise

        def current_group_ids() -> set[str]:
            return self.store.groups_for_definitions(definition_ids)

        def current_legacy_stores() -> list[Any]:
            from .shared_memory_store import SharedMemoryStore

            legacy_paths = dict(iter_legacy_groups(self.store.workspace))
            return [
                SharedMemoryStore(
                    self.store.workspace, group_id, must_exist=True,
                )
                for group_id in sorted(current_group_ids())
                if group_id in legacy_paths
            ]

        coordinator = MergeGovernanceCoordinator(
            self.store.workspace,
            legacy_stores=current_legacy_stores,
            drain_callback=lambda: self.consume_outbox(
                self.store.workspace, only_groups=current_group_ids(),
            ),
            projection_status=lambda: self.store.projection_status(
                group_ids=current_group_ids(),
            ),
        )
        barrier = coordinator.run_merge(execute_current_merge)
        if merge_error:
            raise merge_error[0]
        if not barrier.ok:
            return {
                "ok": False,
                "blocked_reason": barrier.error or "merge_projection_barrier_failed",
                "barrier": barrier.to_dict(),
            }
        decision = barrier.merge_result
        decision = dict(decision)
        decision.update({
            "execution_mode": execution_mode,
            "auto_merge": not human_path,
            "match_kind": governance["match_kind"],
            "governance_reasons": governance["governance_reasons"],
        })
        return {
            "ok": True,
            "decision": decision,
            "execution_mode": execution_mode,
            "auto_merge": not human_path,
            "canonical_definition_id": canonical.definition_id,
            "merged_definition_ids": [merged.definition_id],
        }

    def undo_decision(self, decision_id: str) -> dict[str, Any]:
        """Undo a merge decision via the store's precise inverse."""
        return self.store.undo_merge(decision_id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _definition_from_record(self, record: SharedMemoryRecord) -> RuleDefinition:
        return build_definition(
            record.body,
            kind=record.kind,
            confidence=record.confidence,
            created_at=record.created_at,
        )

    @staticmethod
    def _legacy_definition_id(record: SharedMemoryRecord) -> str:
        """The pre-v2 definition id this record's body would have produced."""
        return stable_hash(
            "rule-definition", "canonical", normalize_rule_text(record.body),
        )

    def _mark_orphan_v1_definitions(self) -> None:
        """Mark pre-v2 definitions with no recoverable body as ``unknown``.

        A v1-format definition still active after backfill has no legacy source
        record to recover its strength from (its source was deleted, or it was
        never covered by a migration).  It can never assert whether a proposed
        merge is a strength conflict, so it is excluded from automatic merging.
        """
        for definition in self.store.list_definitions(status="active"):
            legacy_id = stable_hash(
                "rule-definition", "canonical", definition.canonical_text,
            )
            if definition.definition_id != legacy_id:
                continue  # already a v2 id
            if self.store.get_definition_alias(definition.definition_id):
                continue
            self.store.set_definition_strength_unknown(definition.definition_id)

    def _binding_from_assignment(
        self,
        definition_id: str,
        assignment: Any,
        *,
        share_group_id: str,
        owner_agent_id: str,
        created_by: str,
        authorization: str,
    ) -> RuleBinding:
        target_type = str(getattr(assignment, "target_type", "agent") or "agent")
        source = created_by
        auth = authorization
        if target_type not in AUTO_ALLOWED_TARGET_TYPES:
            # Lossless copy of a legacy wide assignment (group/project/provider/
            # runtime_role/system) that P0-P2 already permitted.  Not an
            # automatic broadening: the binding is built only FROM a legacy
            # assignment and is audited with the legacy hash + migration run id.
            source = "migration"
            auth = json.dumps({
                "legacy_assignment_hash": stable_hash(
                    "legacy-assignment", share_group_id,
                    str(getattr(assignment, "target_type", "") or ""),
                    str(getattr(assignment, "target_id", "") or ""),
                    str(getattr(assignment, "project_ref", "") or ""),
                ),
                "migration_run_id": stable_hash(
                    "rule-migration", share_group_id, "v1",
                ),
                "source_database": "shared-memory",
                "source_revision": "1",
                "created_by": created_by or "backfill",
            }, ensure_ascii=False, sort_keys=True)
        return build_binding(
            definition_id,
            share_group_id=share_group_id,
            target_type=target_type,
            target_id=getattr(assignment, "target_id", ""),
            project_ref=getattr(assignment, "project_ref", ""),
            provider=getattr(assignment, "provider", ""),
            runtime_role=getattr(assignment, "runtime_role", ""),
            effect=getattr(assignment, "effect", "include"),
            priority=getattr(assignment, "priority_override", 0) or 0,
            owner_agent_id=owner_agent_id,
            created_by=source,
            authorization=auth,
        )

    def _combined_evidence(
        self, *definitions: RuleDefinition,
    ) -> list[RuleEvidence]:
        evidences: list[RuleEvidence] = []
        for definition in definitions:
            evidences.extend(
                item for item in self.store.list_evidence(definition.definition_id)
                if self._evidence_is_eligible(item)
            )
        return dedupe_evidence(evidences)

    @staticmethod
    def _evidence_is_eligible(evidence: Any) -> bool:
        """Explicit untrusted sessions cannot satisfy merge governance."""
        if str(getattr(evidence, "source_root_id", "") or "") == (
            "ambiguous_migration_evidence"
        ):
            return False
        session_id = str(getattr(evidence, "session_id", "") or "").strip()
        if not session_id:
            return True
        raw_trusted = getattr(evidence, "session_trusted", 0)
        return bool(
            raw_trusted is True
            or (isinstance(raw_trusted, (int, float)) and raw_trusted == 1)
        )

    def _assess_pair(
        self,
        a: RuleDefinition,
        b: RuleDefinition,
        *,
        min_score: float,
        min_evidence: int,
        min_agents: int,
        min_projects: int,
        negative_score: float = 0.0,
        judge: Any | None = None,
    ) -> MergeAssessment:
        return evaluate_candidate(
            a, b,
            evidence=self._combined_evidence(a, b),
            min_score=min_score,
            min_evidence=min_evidence,
            min_agents=min_agents,
            min_projects=min_projects,
            negative_score=negative_score,
            judge=judge,
        )

    @staticmethod
    def _pick_canonical(
        a: RuleDefinition, b: RuleDefinition,
    ) -> tuple[RuleDefinition, RuleDefinition]:
        # The canonical is the definition whose semantic hash matches intent
        # most closely to the raw text; ties fall to the lexicographically
        # smaller id for determinism.
        if a.semantic_hash == b.semantic_hash:
            return (a, b) if a.definition_id < b.definition_id else (b, a)
        return (a, b)

    # ------------------------------------------------------------------
    # P3-001 maturity engine (pure, recomputed on scan)
    # ------------------------------------------------------------------

    def _maturity_of(self, definition: RuleDefinition) -> str:
        """Recompute the lifecycle stage of one definition (P3-001 §1, PR5).

        Maturity is driven by *this definition's own* runtime feedback — the
        agent reputation of its contributors is a weight input, never a
        substitute for the rule's execution history (a rule must not borrow a
        different rule's success record).  Independent evidence is counted
        after the PR5 independence dedup, so repeated receipts of the same fact
        cannot inflate the stage.

          observing   < 7 days, or independent evidence / agent / project
                      thresholds not yet met
          candidate   evidence thresholds met (no runtime feedback yet)
          validated   >= 7 days + >= 10 rule-specific feedback events with
                      followed/applicable >= 95% and no negative evidence
          trusted     >= 30 days + >= 20 rule-specific feedback events across
                      >= 3 independent projects with no negative evidence
        """
        age = days_between(definition.created_at)
        evidence = dedupe_evidence(
            [
                item
                for item in self.store.list_evidence(definition.definition_id)
                if self._evidence_is_eligible(item)
            ],
        )
        agents = {e.agent_instance_id for e in evidence if e.agent_instance_id}
        projects = {
            e.project_ref for e in evidence if (e.project_ref or "").strip()
        }
        negative = [
            item
            for item in self.store.list_negative_evidence(definition.definition_id)
            if self._evidence_is_eligible(item)
        ]
        if age < OBSERVING_DAYS:
            return "observing"
        if (
            len(evidence) < AUTO_MERGE_MIN_EVIDENCE
            or len(agents) < AUTO_MERGE_MIN_AGENTS
            or len(projects) < AUTO_MERGE_MIN_PROJECTS
        ):
            return "observing"
        runtime_rows = self.store.list_runtime_feedback(definition.definition_id)
        maturity_snapshot = build_maturity_snapshot(
            runtime={"events": runtime_rows},
        )
        trusted_rows = [
            row for row in runtime_rows
            if row.get("session_trusted")
            and str(row.get("session_id") or "").strip()
            and str(row.get("agent_instance_id") or "").strip()
            and str(row.get("project_ref") or "").strip()
        ]
        total_runtime = int(maturity_snapshot["trusted_total"] or 0)
        followed = sum(
            1 for row in trusted_rows if row.get("outcome") == "followed"
        )
        if total_runtime <= 0:
            # Evidence exists but no execution feedback yet: candidate, never
            # validated — an unknown success rate must not be guessed.
            return "candidate"
        success_rate = followed / total_runtime
        state = "candidate"
        if (
            not negative
            and maturity_snapshot["state"] in {"validated", "trusted"}
            and total_runtime >= TRUSTED_MIN_SUCCESS_SAMPLES // 2
            and success_rate >= VALIDATED_SUCCESS_RATE
        ):
            state = "validated"
            if (
                age >= TRUSTED_DAYS
                and maturity_snapshot["state"] == "trusted"
            ):
                state = "trusted"
        return state

    def _refresh_maturity(self, definition: RuleDefinition) -> str:
        state = self._maturity_of(definition)
        if state != definition.maturity_state:
            self.store.set_definition_maturity(definition.definition_id, state)
            definition.maturity_state = state
        return state

    def _execution_success(self, definition: RuleDefinition) -> float | None:
        """Return this Definition's observed runtime success rate only.

        Agent reputation is intentionally excluded: another rule's successful
        execution cannot serve as evidence that this Definition executes well.
        Missing/empty runtime stats remain unknown and are fail-closed by the
        readiness gate.
        """
        stats = self.store.get_runtime_stats(definition.definition_id)
        if not stats:
            return None
        followed = int(
            stats.get("trusted_followed", stats.get("followed", 0)) or 0
        )
        total = int(stats.get("trusted_total", 0) or 0)
        if "trusted_total" not in stats:
            total = sum(
                int(stats.get(name, 0) or 0)
                for name in (
                    "followed", "violated", "not_applicable", "exception_count",
                )
            )
        if total <= 0:
            return None
        return followed / total

    # ------------------------------------------------------------------
    # P3-003 evidence weighting (reputation + project profile + recency)
    # ------------------------------------------------------------------

    def _evidence_weights(self, evidence_list: list[Any]) -> list[float]:
        """Weight each observation (P3-003 §2, PR5 Bayesian).

        Unknown sources default to 0.5, never to full credit; an established
        verified production Agent ranks above a fresh unknown one.
        """
        reps = {r["agent_id"]: r for r in self.store.list_agent_reputations()}
        profiles = {p["project_ref"]: p for p in self.store.list_project_profiles()}
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
            stats = self.store.get_runtime_stats(ev.definition_id)
            total_runtime = int(
                (stats or {}).get(
                    "trusted_total",
                    int((stats or {}).get("followed") or 0)
                    + int((stats or {}).get("violated") or 0)
                    + int((stats or {}).get("not_applicable") or 0)
                    + int((stats or {}).get("exception_count") or 0),
                ) or 0
            )
            if stats and total_runtime > 0:
                rule_specific_success = bayesian_accuracy(
                    int(stats.get("trusted_followed", stats.get("followed", 0)) or 0),
                    total_runtime - int(
                        stats.get("trusted_followed", stats.get("followed", 0))
                        or 0
                    ),
                )
            else:
                rule_specific_success = 0.5
            raw_confidence = getattr(ev, "confidence", None)
            try:
                evidence_confidence = float(
                    1.0 if raw_confidence is None else raw_confidence,
                )
            except (TypeError, ValueError):
                evidence_confidence = 0.0
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
                evidence_confidence=evidence_confidence,
            ))
        return weights

    def _negative_score(self, a: RuleDefinition, b: RuleDefinition) -> float:
        """Weighted contradiction fraction of both definitions (P3-001 §5)."""
        positive = dedupe_evidence(self._combined_evidence(a, b))
        positive_weight = weighted_evidence_score(self._evidence_weights(positive))
        negative = dedupe_evidence([
            e
            for definition in (a, b)
            for e in self.store.list_negative_evidence(definition.definition_id)
            if self._evidence_is_eligible(e)
        ])
        negative_weight = weighted_evidence_score(self._evidence_weights(negative))
        return negative_evidence_score(negative_weight, positive_weight)

    # ------------------------------------------------------------------
    # Merge readiness / governance gate (P3-001 §2, §3, §4)
    # ------------------------------------------------------------------

    def _proposal_governance(
        self, a: RuleDefinition, b: RuleDefinition,
        *,
        proposal: dict[str, Any] | None = None,
        negative_score: float | None = None,
    ) -> dict[str, Any]:
        """Compute the soft governance snapshot of one candidate pair.

        The hard gates (similarity/polarity/params/strength/negative) live in
        ``evaluate_candidate``.  This is the *soft* gate that decides whether a
        candidate may be auto-merged right now: readiness score, cooldown, and
        first-merge acknowledgment.
        """
        self._refresh_maturity(a)
        self._refresh_maturity(b)
        evidence = self._combined_evidence(a, b)
        layers = compute_layers(a, b)
        match_kind = merge_match_kind(layers)
        maturity_ok, maturity_reasons = maturity_gate(a, b, match_kind)
        weights = self._evidence_weights(evidence)
        success_a = self._execution_success(a)
        success_b = self._execution_success(b)
        # Store builds this from the exact SQLite rows later locked by execute_merge.
        readiness_snapshot = self.store.build_readiness_snapshot(
            [a.definition_id, b.definition_id],
        )
        readiness = float(readiness_snapshot["score"])

        cooldown_until = str((proposal or {}).get("cooldown_until") or "")
        cooldown_active = False
        if cooldown_until:
            try:
                cooldown_active = _now_iso() < cooldown_until
            except (TypeError, ValueError):
                cooldown_active = False
        first_merge = (
            self.store.count_merge_decisions_for_definitions(
                [a.definition_id, b.definition_id]
            )
            == 0
        )
        acknowledged = bool((proposal or {}).get("first_merge_acknowledged"))

        # P3-003 §5: no single Agent may dominate a rule's evidence weight.
        per_agent: dict[str, float] = {}
        for ev, w in zip(evidence, weights):
            per_agent[ev.agent_instance_id or ""] = (
                per_agent.get(ev.agent_instance_id or "", 0.0) + w
            )
        dominance_ratio = largest_source_ratio(per_agent)

        reasons: list[str] = []
        reasons.extend(maturity_reasons)
        if success_a is None or success_b is None:
            reasons.append("runtime_feedback_missing")
        if readiness < AUTO_READINESS_SCORE:
            reasons.append("readiness_below_auto")
        stored_readiness = (proposal or {}).get("readiness_score")
        if stored_readiness is not None:
            try:
                if abs(float(stored_readiness) - readiness) > READINESS_DRIFT_TOLERANCE:
                    reasons.append("readiness_drift")
            except (TypeError, ValueError):
                reasons.append("readiness_drift")
        if cooldown_active:
            reasons.append("cooldown_active")
        if first_merge and not acknowledged:
            reasons.append("first_merge_requires_approval")
        if dominance_ratio >= MAX_SINGLE_SOURCE_RATIO:
            reasons.append("single_agent_dominance")

        return {
            "readiness_score": readiness,
            "readiness_components": readiness_snapshot["components"],
            "readiness_digest": readiness_snapshot["digest"],
            "readiness_snapshot": readiness_snapshot,
            "cooldown_until": cooldown_until,
            "cooldown_active": cooldown_active,
            "first_merge": first_merge,
            "negative_score": (
                self._negative_score(a, b) if negative_score is None
                else float(negative_score)
            ),
            "single_source_ratio": round(dominance_ratio, 4),
            "governance_reasons": reasons,
            "eligible": not reasons,
            "match_kind": match_kind,
            "maturity_ok": maturity_ok,
            "maturity_state_a": a.maturity_state,
            "maturity_state_b": b.maturity_state,
            "execution_success_a": success_a,
            "execution_success_b": success_b,
            "runtime_feedback_ok": success_a is not None and success_b is not None,
            # PR5: persist the full weight breakdown for every proposal so the
            # weight model is auditable, not a self-reported score.
            "weight_breakdown": json.dumps({
                "per_agent": {
                    k: round(v, 4)
                    for k, v in sorted(per_agent.items()) if k
                },
                "total_weight": round(sum(weights), 4),
                "evidence_count": len(evidence),
                "min_evidence": WEIGHTED_EVIDENCE_MIN,
            }, ensure_ascii=False, sort_keys=True),
        }

    # ------------------------------------------------------------------
    # P3-002 strength evolution (Rule Evolution Graph)
    # ------------------------------------------------------------------

    def evolve_strength(
        self,
        definition_id: str,
        new_strength: str,
        *,
        reason: str = "",
        actor: str = "auto",
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Promote/demote a rule's strength as a *new* Definition.

        P3-002 §5: "提交建议测试" -> "提交必须测试" is not an overwrite.  A new
        Definition is created with the new strength, a version row records the
        change (old/new/reason/actor), and the old Definition is marked
        ``superseded``.  Because the scanner only ever considers ``active``
        Definitions, a rule can never be proposed as a merge of its own history.
        """
        old = self.store.get_definition(definition_id)
        if old is None:
            raise ValueError("rule_definition_not_found")
        old_strength = old.rule_strength
        try:
            new_value = RuleStrength(new_strength).value
        except (TypeError, ValueError):
            raise ValueError("invalid rule_strength") from None
        if new_value == old_strength:
            raise ValueError("rule_strength_unchanged")

        now = _now_iso()
        new_definition_id = stable_hash(
            "rule-definition-version", definition_id, new_value, now,
        )
        new_definition = build_definition(
            old.canonical_text,
            definition_id=new_definition_id,
            kind=old.rule_kind,
            confidence=old.confidence,
            created_at=now,
            rule_strength=new_value,
        )
        # Atomic: create the new Definition, migrate every active Binding to it,
        # record the evolution link, mark the old Definition superseded — in one
        # transaction, so a half-evolved orphan can never exist.
        result = self.store.evolve_definition_atomic(
            old_definition_id=definition_id,
            new_definition=new_definition,
            old_strength=old_strength,
            new_strength=new_value,
            change_reason=reason,
            actor=actor,
            evidence=evidence,
        )
        version = {
            "version_id": result["version_id"],
            "definition_id": definition_id,
            "superseded_by": new_definition_id,
            "old_strength": old_strength,
            "new_strength": new_value,
            "change_reason": reason,
            "actor": actor,
            "evidence": evidence or {},
            "created_at": now,
        }
        return {
            "old_definition_id": definition_id,
            "new_definition_id": new_definition_id,
            "version": version,
            "new_strength": new_value,
        }


# ---------------------------------------------------------------------------
# Convenience entry points (used by the acceptance script)
# ---------------------------------------------------------------------------


def open_merge_store(workspace: str | Path) -> RuleMergeStore:
    return RuleMergeStore(workspace)


def open_merge_service(workspace: str | Path) -> RuleMergeService:
    return RuleMergeService(RuleMergeStore(workspace))
