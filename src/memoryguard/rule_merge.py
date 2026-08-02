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

from .rule_binding import RuleBinding, build_binding
from .rule_definition import (
    POLARITY_POSITIVE,
    RuleDefinition,
    RuleStrength,
    build_definition,
)
from .rule_evidence import (
    NegativeEvidence,
    RuleEvidence,
    build_evidence,
    build_negative_evidence,
)
from .rule_merge_policy import (
    AUTO_MERGE_MIN_AGENTS,
    AUTO_MERGE_MIN_EVIDENCE,
    AUTO_MERGE_MIN_PROJECTS,
    AUTO_MERGE_SCORE,
    AUTO_READINESS_SCORE,
    COOLDOWN_HOURS,
    MAX_SINGLE_SOURCE_RATIO,
    NEGATIVE_EVIDENCE_THRESHOLD,
    OBSERVING_DAYS,
    TRUSTED_DAYS,
    TRUSTED_MIN_SUCCESS_SAMPLES,
    VALIDATED_SUCCESS_RATE,
    WEIGHTED_EVIDENCE_MIN,
    MergeAssessment,
    compute_layers,
    contradiction_score,
    days_between,
    evaluate_candidate,
    evidence_weight,
    largest_source_ratio,
    maturity_score,
    merge_readiness_score,
    negative_evidence_score,
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
        """Migrate one group's records/assignments/receipts into the P3 layer."""
        ledger = {
            "records": 0, "definitions": 0, "assignments": 0,
            "bindings": 0, "receipts": 0, "evidence": 0,
        }
        records = store.list_records()
        for record in records:
            if record.status == SharedMemoryStatus.DELETED:
                continue
            ledger["records"] += 1
            definition = self._definition_from_record(record)
            self.store.upsert_definition(definition)
            ledger["definitions"] += 1
            try:
                assignments = store.list_rule_assignments(record.memory_id)
            except Exception:
                assignments = []
            for assignment in assignments:
                ledger["assignments"] += 1
                binding = self._binding_from_assignment(
                    definition, assignment, share_group_id=group_id,
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
                    definition_id=definition.definition_id,
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
        """Dual-write: mirror one newly-created rule into the P3 layer.

        Best-effort by design: a P3-layer failure must never block rule
        creation in the legacy store, so callers wrap this in try/except.
        """
        definition = self._definition_from_record(record)
        self.store.upsert_definition(definition)
        bindings = 0
        for assignment in assignments or []:
            binding = self._binding_from_assignment(
                definition, assignment, share_group_id=group_id,
                owner_agent_id=record.agent_instance_id,
                created_by=created_by,
                authorization="dual-write",
            )
            self.store.upsert_binding(binding)
            bindings += 1
        evidence = 0
        for receipt in receipts or []:
            item = build_evidence(
                definition_id=definition.definition_id,
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
        return {
            "definition_id": definition.definition_id,
            "bindings": bindings,
            "evidence": evidence,
        }

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
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]
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
                else:
                    status = "rejected"
                    conflict_type = assessment.conflict_type
                proposal = self.store.create_proposal(
                    [a.definition_id, b.definition_id],
                    assessment.duplicate_score,
                    evidence=self._combined_evidence(a, b),
                    contradiction_score=contradiction_score(a, b),
                    explanation="; ".join(assessment.reasons),
                    readiness_score=governance["readiness_score"],
                    governance_reasons="; ".join(governance["governance_reasons"]),
                    cooldown_until=governance["cooldown_until"],
                    negative_score=governance["negative_score"],
                    conflict_type=conflict_type,
                    judge=assessment.judge,
                )
                self.store.set_proposal_status(proposal["proposal_id"], status)
                proposals.append(self.store.get_proposal(proposal["proposal_id"]))
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
        human_path = (
            status == "approved"
            or str(actor or "").casefold() in HUMAN_ACTORS
        )
        if status not in {"candidate", "approved"}:
            self.store.set_proposal_status(proposal_id, "rejected")
            return {
                "ok": False,
                "blocked_reason": "rule_merge_proposal_not_approved",
                "conflict_type": proposal.get("conflict_type") or "",
            }

        negative_score = self._negative_score(a, b)
        judge_verdict = getattr(
            compute_layers(a, b, judge=judge), "judge", None,
        )

        if human_path:
            # Human approval relaxes the *evidence and similarity* thresholds,
            # but the two governance conflicts the layer treats as never-
            # mergeable — strength mismatch (P3-002) and negative evidence
            # (P3-001 §5) — still block even a human-approved merge.
            strength_ok = str(a.rule_strength or "") == str(b.rule_strength or "")
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

    def _binding_from_assignment(
        self,
        definition: RuleDefinition,
        assignment: Any,
        *,
        share_group_id: str,
        owner_agent_id: str,
        created_by: str,
        authorization: str,
    ) -> RuleBinding:
        return build_binding(
            definition.definition_id,
            share_group_id=share_group_id,
            target_type=getattr(assignment, "target_type", "agent"),
            target_id=getattr(assignment, "target_id", ""),
            project_ref=getattr(assignment, "project_ref", ""),
            provider=getattr(assignment, "provider", ""),
            runtime_role=getattr(assignment, "runtime_role", ""),
            effect=getattr(assignment, "effect", "include"),
            priority=getattr(assignment, "priority_override", 0) or 0,
            owner_agent_id=owner_agent_id,
            created_by=created_by,
            authorization=authorization,
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
        """Recompute the lifecycle stage of one definition (P3-001 §1).

        observing <7d → candidate (evidence thresholds met) → validated (no
        conflict, success >=95% when known) → trusted (>=30d, >=20 success
        samples, no conflict).  Execution data is optional: when unknown it is
        treated as neutral (neither qualifies nor blocks), so the layer never
        fabricates a success rate.
        """
        age = days_between(definition.created_at)
        evidence = self.store.list_evidence(definition.definition_id)
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
        state = "candidate"
        success = self._execution_success(definition)
        if not negative and (success is None or success >= VALIDATED_SUCCESS_RATE):
            state = "validated"
            if (
                age >= TRUSTED_DAYS
                and self._success_sample_count(evidence) >= TRUSTED_MIN_SUCCESS_SAMPLES
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

    def _success_sample_count(self, evidence: list[Any]) -> int:
        reps = {r["agent_id"]: r for r in self.store.list_agent_reputations()}
        total = 0
        seen: set[str] = set()
        for e in evidence:
            agent = e.agent_instance_id or ""
            if agent and agent in reps and agent not in seen:
                seen.add(agent)
                total += int(reps[agent]["sample_count"] or 0)
        return total

    # ------------------------------------------------------------------
    # P3-003 evidence weighting (reputation + project profile + recency)
    # ------------------------------------------------------------------

    def _evidence_weights(self, evidence_list: list[Any]) -> list[float]:
        """Weight each observation (P3-003 §2); cold-start neutral is 1.0."""
        reps = {r["agent_id"]: r for r in self.store.list_agent_reputations()}
        profiles = {p["project_ref"]: p for p in self.store.list_project_profiles()}
        weights: list[float] = []
        for ev in evidence_list:
            rep = reps.get(ev.agent_instance_id or "")
            profile = profiles.get(ev.project_ref or "")
            weights.append(evidence_weight(
                agent_reputation=(
                    (rep["success_rate"] + rep["rule_accuracy"]) / 2.0
                    if rep else 1.0
                ),
                project_importance=(
                    profile["production_level"] if profile else 1.0
                ),
                historical_success=(
                    rep["success_rate"] if rep and rep["sample_count"] > 0 else 1.0
                ),
                feedback_quality=rep["feedback_quality"] if rep else 1.0,
                recency=recency_factor(days_between(ev.observed_at)),
            ))
        return weights

    def _negative_score(self, a: RuleDefinition, b: RuleDefinition) -> float:
        """Weighted contradiction fraction of both definitions (P3-001 §5)."""
        positive = self._combined_evidence(a, b)
        positive_weight = weighted_evidence_score(self._evidence_weights(positive))
        negative = [
            e
            for definition in (a, b)
            for e in self.store.list_negative_evidence(definition.definition_id)
        ]
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
        self.store.upsert_definition(new_definition)
        version = self.store.record_definition_version(
            definition_id=definition_id,
            superseded_by=new_definition_id,
            old_strength=old_strength,
            new_strength=new_value,
            change_reason=reason,
            actor=actor,
            evidence=evidence,
        )
        self.store.set_definition_status(
            definition_id, "superseded", superseded_by=new_definition_id,
        )
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
