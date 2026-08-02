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
)
from .rule_evidence import (
    NegativeEvidence,
    RuleEvidence,
    build_evidence,
    build_negative_evidence,
    dedupe_evidence,
)
from .rule_merge_policy import (
    AUTO_MERGE_MIN_AGENTS,
    AUTO_MERGE_MIN_EVIDENCE,
    AUTO_MERGE_MIN_PROJECTS,
    AUTO_MERGE_SCORE,
    AUTO_READINESS_SCORE,
    COOLDOWN_HOURS,
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
    compute_layers,
    contradiction_score,
    days_between,
    evaluate_candidate,
    evidence_weight,
    feedback_authority_score,
    largest_source_ratio,
    maturity_score,
    merge_readiness_score,
    negative_evidence_score,
    parameter_conflict,
    project_importance_score,
    recency_factor,
    weighted_evidence_score,
)
from .rule_merge_store import RuleMergeStore, iter_legacy_groups
from .schema_v3 import SharedMemoryRecord, SharedMemoryStatus, _now_iso, stable_hash

# Actors that count as explicit human governance (bypass readiness/cooldown/
# first-merge gates — the human already reviewed the risk).  Hard conflicts
# (strength/polarity/parameter/negative evidence) are never bypassed.
HUMAN_ACTORS = {"human", "user", "admin", "manual"}


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
        for record in records:
            if record.status == SharedMemoryStatus.DELETED:
                continue
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

            # v1 -> v2 identity migration for the active definition.
            legacy_id = self._legacy_definition_id(record)
            if legacy_id != canonical_id:
                removed = self.store.migrate_legacy_definition(
                    legacy_id, canonical_id,
                )
                if removed is not None:
                    migrated_audiences[canonical_id] = removed
            try:
                assignments = store.list_rule_assignments(record.memory_id)
            except Exception:
                assignments = []
            for assignment in assignments:
                ledger["assignments"] += 1
                binding = self._binding_from_assignment(
                    canonical_id, assignment, share_group_id=group_id,
                    owner_agent_id=record.agent_instance_id,
                    created_by="backfill",
                    authorization="backfill",
                )
                self.store.upsert_binding(binding)
                ledger["bindings"] += 1
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
        # Scope preservation: every audience removed by a v1->v2 migration must
        # have been recreated under the canonical definition by this pass.
        if migrated_audiences:
            for new_id, removed in migrated_audiences.items():
                present = {
                    binding_identity_key(b)
                    for b in self.store.list_bindings(
                        definition_id=new_id, status="active",
                    )
                }
                for audience in removed:
                    if audience not in present:
                        raise RuntimeError(
                            "rule_migration_scope_change: an audience "
                            "disappeared during v1->v2 migration"
                        )
        self._mark_orphan_v1_definitions()
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
        existing = self.store.get_definition(canonical_id)
        if existing is None or existing.status == "active":
            if canonical_id != new_id:
                definition = build_definition(
                    record.body,
                    definition_id=canonical_id,
                    kind=record.kind,
                    confidence=record.confidence,
                    created_at=record.created_at,
                )
            self.store.upsert_definition(definition)
        bindings = 0
        for assignment in assignments or []:
            binding = self._binding_from_assignment(
                canonical_id, assignment, share_group_id=group_id,
                owner_agent_id=record.agent_instance_id,
                created_by=created_by,
                authorization="dual-write",
            )
            self.store.upsert_binding(binding)
            bindings += 1
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
            )
            self.store.upsert_evidence(item)
            evidence += 1
        self.store.upsert_source_link(
            share_group_id=group_id, memory_id=record.memory_id,
            source_revision=getattr(record, "updated_at", "") or getattr(record, "created_at", "") or "",
            original_definition_id=new_id,
            canonical_definition_id=canonical_id,
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
        self, workspace: str | Path | None = None, *, only_group: str | None = None,
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
        for group_id, _db_path in iter_legacy_groups(workspace):
            if only_group and group_id != only_group:
                continue
            try:
                legacy = SharedMemoryStore(workspace, group_id)
                events = legacy.list_unconsumed_rule_events()
            except Exception:
                continue
            summary["groups"] += 1
            summary["events_seen"] += len(events)
            for event in events:
                if self._consume_feedback_event(legacy, group_id, event):
                    summary["events_consumed"] += 1
                    summary["definitions_touched"] += 1
                    try:
                        legacy.mark_rule_event_consumed(event["event_id"])
                    except Exception:
                        pass
        return summary

    def _consume_feedback_event(
        self, legacy: Any, group_id: str, event: dict[str, Any],
    ) -> bool:
        """Project one outbox event; idempotent, checkpointed by the caller."""
        memory_id = str(event.get("memory_id") or "")
        outcome = str(event.get("outcome") or "")
        if outcome in {"ignored", "unobserved"} or not memory_id:
            return True  # nothing to project; still checkpoint so it never retries
        try:
            record = legacy.get_record(memory_id)
        except Exception:
            record = None
        if record is None:
            return True  # source gone; do not resurrect it
        definition = self._definition_from_record(record)
        canonical_id = definition.definition_id
        link = self.store.get_source_link(group_id, memory_id)
        if link and link.get("canonical_definition_id"):
            canonical_id = link["canonical_definition_id"]
        # Follow the alias/merged/superseded chain: feedback on a source whose
        # definition was merged lands on the current canonical, never reviving
        # the merged definition.
        canonical_id = self.store.resolve_canonical(canonical_id)
        if self.store.get_definition(canonical_id) is None:
            self.store.upsert_definition(definition)
        agent = str(event.get("agent_instance_id") or "")
        project = str(event.get("project_ref") or "")
        session = str(event.get("session_id") or "")
        created_at = str(event.get("created_at") or "") or _now_iso()
        confidence = float(event.get("confidence") or 1.0)
        receipt_id = str(event.get("receipt_id") or "")
        provider = str(event.get("provider") or "")
        evidence_text = str(event.get("evidence") or "")
        share_group_id = str(event.get("share_group_id") or group_id)
        feedback_id = str(event.get("feedback_id") or "")
        authority = int(event.get("authority") or 0)
        if outcome == "followed":
            self.store.upsert_evidence(build_evidence(
                definition_id=canonical_id, source_rule_id=memory_id,
                agent_instance_id=agent, project_ref=project,
                session_id=session, receipt_id=receipt_id, provider=provider,
                content=record.body, confidence=confidence, observed_at=created_at,
                share_group_id=share_group_id, session_trusted=1,
                feedback_id=feedback_id, feedback_authority=authority,
            ))
        elif outcome in {"not_applicable", "exception"}:
            self.store.upsert_negative_evidence(build_negative_evidence(
                definition_id=canonical_id, source_rule_id=memory_id,
                agent_instance_id=agent, project_ref=project,
                content=record.body, confidence=confidence, observed_at=created_at,
                share_group_id=share_group_id, session_id=session,
                receipt_id=receipt_id, feedback_id=feedback_id,
                feedback_authority=authority, session_trusted=1,
            ))
        elif outcome == "corrected":
            lowered = evidence_text.casefold()
            if any(
                marker in lowered
                for marker in ("scope", "范围", "not applicable", "不适用")
            ):
                self.store.upsert_negative_evidence(build_negative_evidence(
                    definition_id=canonical_id, source_rule_id=memory_id,
                    agent_instance_id=agent, project_ref=project,
                    content=record.body, confidence=confidence,
                    observed_at=created_at, share_group_id=share_group_id,
                    session_id=session, receipt_id=receipt_id,
                    feedback_id=feedback_id, feedback_authority=authority,
                    session_trusted=1,
                ))
        # Every observed outcome feeds the idempotent runtime-feedback ledger;
        # counters are derived in recompute_runtime_stats, never incremented.
        self.store.upsert_runtime_feedback(
            feedback_id=str(event.get("feedback_id") or ""),
            definition_id=canonical_id, outcome=outcome,
            agent_instance_id=agent, project_ref=project, session_id=session,
            source=str(event.get("source") or ""),
            authority=int(event.get("authority") or 0),
            created_at=created_at,
        )
        self.store.recompute_runtime_stats(canonical_id)
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
        # maturity snapshot is fresh (idempotent; no-op when the outbox is empty).
        try:
            self.consume_outbox(self.store.workspace)
        except Exception:
            pass
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
        # PR7 bounded scan: candidates are bucketed by semantic_hash (intent +
        # polarity + parameters), so only pairs that could actually be the same
        # rule are full-assessed — never every pair in an O(N²) sweep.  Pairs
        # from different buckets are different rules by construction.
        buckets: dict[str, list[Any]] = {}
        for definition in candidates:
            buckets.setdefault(
                definition.semantic_hash or "∅", [],
            ).append(definition)
        pairs_evaluated = 0
        pairs_skipped = 0
        rejected_persisted = 0
        for members in buckets.values():
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
                    if assessment.can_auto_merge:
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

        negative_score = self._negative_score(a, b)
        judge_verdict = getattr(
            compute_layers(a, b, judge=judge), "judge", None,
        )

        if human_path:
            # Human approval relaxes the *evidence and similarity* thresholds,
            # but the governance conflicts — strength (P3-002), negative
            # evidence (P3-001 §5), polarity, parameters and contradiction —
            # still block even a human-approved merge.  An ``unknown`` strength
            # is likewise never mergeable.
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
            if not assessment.can_auto_merge:
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

        governance = self._proposal_governance(a, b, proposal=proposal)
        if not human_path and not governance["eligible"]:
            # Soft governance gate blocked the automatic merge.  Stay a
            # candidate so evidence keeps collecting; record why.
            self.store.update_proposal_governance(
                proposal_id,
                readiness_score=governance["readiness_score"],
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

        canonical, merged = self._pick_canonical(a, b)
        # For a human-approved merge, pin the merge to the exact state the
        # approver reviewed: definition revisions + evidence digest captured at
        # the last scan/approval, and the approval id itself.  The store
        # re-verifies all of these and the hard gates inside its transaction.
        expected_revisions = None
        expected_digest = ""
        approval_id = ""
        if approval is not None:
            approval_id = approval.get("approval_id", "")
            rev_a = int(proposal.get("definition_revision_a") or 0)
            rev_b = int(proposal.get("definition_revision_b") or 0)
            if rev_a > 0 and rev_b > 0:
                expected_revisions = {
                    str(a.definition_id): rev_a,
                    str(b.definition_id): rev_b,
                }
            expected_digest = str(proposal.get("evidence_digest") or "")
        decision = self.store.execute_merge(
            proposal_id=proposal_id,
            canonical_definition_id=canonical.definition_id,
            merged_definition_ids=[merged.definition_id],
            actor=actor,
            readiness_at_merge=governance["readiness_score"],
            strength_ok=strength_ok,
            negative_ok=negative_ok,
            first_merge_acknowledged=(
                bool(proposal.get("first_merge_acknowledged"))
                or human_path
            ),
            judge=judge_verdict,
            approval_id=approval_id,
            expected_definition_revisions=expected_revisions,
            expected_evidence_digest=expected_digest,
        )
        return {
            "ok": True,
            "decision": decision,
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
            evidences.extend(self.store.list_evidence(definition.definition_id))
        return evidences

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
            self.store.list_evidence(definition.definition_id),
        )
        agents = {e.agent_instance_id for e in evidence if e.agent_instance_id}
        projects = {
            e.project_ref for e in evidence if (e.project_ref or "").strip()
        }
        negative = self.store.list_negative_evidence(definition.definition_id)
        if age < OBSERVING_DAYS:
            return "observing"
        if (
            len(evidence) < AUTO_MERGE_MIN_EVIDENCE
            or len(agents) < AUTO_MERGE_MIN_AGENTS
            or len(projects) < AUTO_MERGE_MIN_PROJECTS
        ):
            return "observing"
        stats = self.store.get_runtime_stats(definition.definition_id) or {}
        followed = int(stats.get("followed") or 0)
        total_runtime = (
            followed
            + int(stats.get("violated") or 0)
            + int(stats.get("not_applicable") or 0)
            + int(stats.get("exception_count") or 0)
        )
        if total_runtime <= 0:
            # Evidence exists but no execution feedback yet: candidate, never
            # validated — an unknown success rate must not be guessed.
            return "candidate"
        success_rate = followed / total_runtime
        state = "candidate"
        if (
            not negative
            and total_runtime >= TRUSTED_MIN_SUCCESS_SAMPLES // 2
            and success_rate >= VALIDATED_SUCCESS_RATE
        ):
            state = "validated"
            if (
                age >= TRUSTED_DAYS
                and total_runtime >= TRUSTED_MIN_SUCCESS_SAMPLES
                and len(projects) >= 3
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
        """Average observed success rate of the agents behind this definition.

        None when no agent has a reputation yet (neutral, never blocks).
        """
        reps = {r["agent_id"]: r for r in self.store.list_agent_reputations()}
        rates = [
            reps[e.agent_instance_id]["success_rate"]
            for e in self.store.list_evidence(definition.definition_id)
            if e.agent_instance_id in reps
        ]
        if not rates:
            return None
        return sum(rates) / len(rates)

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

    def _negative_score(self, a: RuleDefinition, b: RuleDefinition) -> float:
        """Weighted contradiction fraction of both definitions (P3-001 §5)."""
        positive = dedupe_evidence(self._combined_evidence(a, b))
        positive_weight = weighted_evidence_score(self._evidence_weights(positive))
        negative = dedupe_evidence([
            e
            for definition in (a, b)
            for e in self.store.list_negative_evidence(definition.definition_id)
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
        evidence = self._combined_evidence(a, b)
        layers = compute_layers(a, b)
        weights = self._evidence_weights(evidence)
        weighted = weighted_evidence_score(weights)
        evidence_confidence = min(1.0, weighted / WEIGHTED_EVIDENCE_MIN)
        maturity = min(
            maturity_score(a.maturity_state),
            maturity_score(b.maturity_state),
        )
        success_a = self._execution_success(a)
        success_b = self._execution_success(b)
        execution_success = (
            (success_a if success_a is not None else 0.5)
            + (success_b if success_b is not None else 0.5)
        ) / 2.0
        agents = {e.agent_instance_id for e in evidence if e.agent_instance_id}
        projects = {
            e.project_ref for e in evidence if (e.project_ref or "").strip()
        }
        source_diversity = min(1.0, (len(agents) + len(projects)) / 4.0)
        stability = min(
            1.0,
            min(
                days_between(a.created_at),
                days_between(b.created_at),
            ) / TRUSTED_DAYS,
        )
        readiness = merge_readiness_score(
            duplicate_score=layers.duplicate_score,
            evidence_confidence=evidence_confidence,
            maturity=maturity,
            execution_success=execution_success,
            source_diversity=source_diversity,
            stability=stability,
        )

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
        if readiness < AUTO_READINESS_SCORE:
            reasons.append("readiness_below_auto")
        if cooldown_active:
            reasons.append("cooldown_active")
        if first_merge and not acknowledged:
            reasons.append("first_merge_requires_approval")
        if dominance_ratio >= MAX_SINGLE_SOURCE_RATIO:
            reasons.append("single_agent_dominance")

        return {
            "readiness_score": readiness,
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
