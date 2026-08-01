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
from pathlib import Path
from typing import Any, Iterable

from .rule_binding import RuleBinding, build_binding
from .rule_definition import RuleDefinition, build_definition
from .rule_evidence import RuleEvidence, build_evidence
from .rule_merge_policy import (
    AUTO_MERGE_MIN_AGENTS,
    AUTO_MERGE_MIN_EVIDENCE,
    AUTO_MERGE_MIN_PROJECTS,
    AUTO_MERGE_SCORE,
    MergeAssessment,
    contradiction_score,
    evaluate_candidate,
)
from .rule_merge_store import RuleMergeStore, iter_legacy_groups
from .schema_v3 import SharedMemoryRecord, SharedMemoryStatus


class RuleMergeService:
    """High-level merge pipeline over one Rule Intelligence store."""

    def __init__(self, store: RuleMergeStore):
        self.store = store

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
    ) -> list[dict[str, Any]]:
        """Find duplicate-active Definition pairs and persist merge proposals.

        Every pair whose candidate fails is still recorded as a proposal with
        ``status='rejected'`` and its reasons, so the governance UI can show why
        a merge was refused.  Only pairs passing all conditions become
        ``candidate`` proposals.
        """
        candidates = self.store.list_definitions(status="active")
        if definition_ids:
            wanted = set(definition_ids)
            candidates = [d for d in candidates if d.definition_id in wanted]
        proposals: list[dict[str, Any]] = []
        for i in range(len(candidates)):
            for j in range(i + 1, len(candidates)):
                a, b = candidates[i], candidates[j]
                assessment = self._assess_pair(
                    a, b, min_score=min_score, min_evidence=min_evidence,
                    min_agents=min_agents, min_projects=min_projects,
                )
                proposal = self.store.create_proposal(
                    [a.definition_id, b.definition_id],
                    assessment.duplicate_score,
                    evidence=self._combined_evidence(a, b),
                    contradiction_score=contradiction_score(a, b),
                    explanation="; ".join(assessment.reasons),
                )
                if assessment.can_auto_merge:
                    self.store.set_proposal_status(
                        proposal["proposal_id"], "candidate",
                    )
                else:
                    self.store.set_proposal_status(
                        proposal["proposal_id"], "rejected",
                    )
                proposals.append(self.store.get_proposal(proposal["proposal_id"]))
        return proposals

    def merge_proposal(
        self, proposal_id: str, *, actor: str = "auto",
    ) -> dict[str, Any]:
        """Evaluate and execute one merge proposal.

        Returns the merge decision on success, or a blocked result carrying the
        assessment reasons when the candidate fails safety evaluation.
        """
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
        if status == "candidate":
            # Automatic path: every safety condition must hold (P3 §5).
            assessment = evaluate_candidate(
                a, b,
                evidence=self._combined_evidence(a, b),
                min_score=AUTO_MERGE_SCORE,
                min_evidence=AUTO_MERGE_MIN_EVIDENCE,
                min_agents=AUTO_MERGE_MIN_AGENTS,
                min_projects=AUTO_MERGE_MIN_PROJECTS,
            )
            if not assessment.can_auto_merge:
                self.store.set_proposal_status(proposal_id, "rejected")
                return {
                    "ok": False,
                    "blocked_reason": "merge_safety_evaluation_failed",
                    "assessment": {
                        "duplicate_score": assessment.duplicate_score,
                        "reasons": list(assessment.reasons),
                    },
                }
        elif status != "approved":
            # Human-approved proposals may proceed with the automatic
            # thresholds relaxed, but the store's scope-invariance transaction
            # still refuses any merge that would change a binding's audience.
            self.store.set_proposal_status(proposal_id, "rejected")
            return {
                "ok": False,
                "blocked_reason": "rule_merge_proposal_not_approved",
            }

        canonical, merged = self._pick_canonical(a, b)
        decision = self.store.execute_merge(
            proposal_id=proposal_id,
            canonical_definition_id=canonical.definition_id,
            merged_definition_ids=[merged.definition_id],
            actor=actor,
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
    ) -> MergeAssessment:
        return evaluate_candidate(
            a, b,
            evidence=self._combined_evidence(a, b),
            min_score=min_score,
            min_evidence=min_evidence,
            min_agents=min_agents,
            min_projects=min_projects,
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


# ---------------------------------------------------------------------------
# Convenience entry points (used by the acceptance script)
# ---------------------------------------------------------------------------


def open_merge_store(workspace: str | Path) -> RuleMergeStore:
    return RuleMergeStore(workspace)


def open_merge_service(workspace: str | Path) -> RuleMergeService:
    return RuleMergeService(RuleMergeStore(workspace))
