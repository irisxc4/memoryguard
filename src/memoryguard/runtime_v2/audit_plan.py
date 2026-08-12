"""Deterministic V2 audit plan/apply receipts for the GUI.

Only repairs with an existing idempotent V2 projector are automatable. Schema,
reference, integrity and unknown-data findings remain explicitly non-fixable;
this service never guesses repairs or deletes authoritative rows.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from ..evidence.store import EvidenceStore
from ..maintenance_v2.reference_audit import Blocker, ReferenceAudit, Result
from ..memory.store import MemoryAtomStore
from ..rules.v2_store import EvidenceProjector, RuleV2Store
from ..storage.layout import WorkspaceV2Layout
from .group_native import GroupControlError, SystemControlStore


class AuditPlanError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "audit_plan_failed")
        super().__init__(self.code)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _finding_id(blocker: Blocker) -> str:
    return "v2-" + hashlib.sha256(
        f"{blocker.code}\0{blocker.domain}\0{blocker.table}".encode("utf-8")
    ).hexdigest()[:16]


class AuditPlanService:
    """ReferenceAudit plan generation plus narrowly supported repair actions."""

    _SUPPORTED = {
        ("unconsumed_outbox", "memory", "domain_outbox"): "project_memory_outbox",
        ("unconsumed_outbox", "rules", "rule_evidence_outbox"): "project_rule_evidence_outbox",
    }

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.layout = WorkspaceV2Layout(self.workspace)
        self.system = SystemControlStore(self.workspace, write=False)

    def _audit(self) -> Result:
        try:
            return ReferenceAudit(self.workspace, mode="ro").audit()
        except Exception as exc:
            raise AuditPlanError("audit_unavailable") from exc

    @staticmethod
    def _precondition(blocker: Blocker, result: Result) -> str:
        return _digest({
            "finding": {
                "code": blocker.code,
                "domain": blocker.domain,
                "table": blocker.table,
                "detail": dict(blocker.detail),
            },
            "registry_digest": result.registry_digest,
            "schema_fingerprints": dict(result.schema_fingerprints),
            "manifest_generation": result.manifest_generation,
        })

    def _plan(self, blocker: Blocker, result: Result) -> dict[str, Any]:
        action = self._SUPPORTED.get((blocker.code, blocker.domain, blocker.table), "manual_repair_required")
        fixable = action != "manual_repair_required"
        precondition = self._precondition(blocker, result)
        finding = _finding_id(blocker)
        plan_id = "audit-plan-" + _digest({
            "finding_id": finding,
            "action": action,
            "precondition_digest": precondition,
        })
        return {
            "plan_id": plan_id,
            "finding_id": finding,
            "code": blocker.code,
            "domain": blocker.domain,
            "table": blocker.table,
            "action": action,
            "fixable": fixable,
            "undoable": False,
            "precondition_digest": precondition,
            "requires_confirmation": fixable,
            "reason": (
                "V2 idempotent outbox projector is available"
                if fixable else
                "automatic repair is unsafe for this authoritative finding"
            ),
        }

    def generate(self, finding_id: str) -> dict[str, Any]:
        target = str(finding_id or "").strip()
        if not target:
            raise AuditPlanError("finding_id_required")
        result = self._audit()
        blocker = next((item for item in result.blockers if _finding_id(item) == target or item.code == target), None)
        if blocker is None:
            raise AuditPlanError("finding_not_found")
        return {
            "ok": True,
            "status": "succeeded",
            "plan": self._plan(blocker, result),
            "audit_status": result.status,
        }

    def _find_plan(self, plan_id: str, result: Result) -> tuple[Blocker, dict[str, Any]] | None:
        for blocker in result.blockers:
            plan = self._plan(blocker, result)
            if plan["plan_id"] == plan_id:
                return blocker, plan
        return None

    def _claim(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        request = {
            "plan_id": str(plan["plan_id"]),
            "finding_id": str(plan["finding_id"]),
            "action": str(plan["action"]),
            "precondition_digest": str(plan["precondition_digest"]),
        }
        store = SystemControlStore(self.workspace, write=True)

        def apply(_conn: Any):
            return ({
                "ok": True,
                "status": "claimed",
                "plan_id": str(plan["plan_id"]),
                "finding_id": str(plan["finding_id"]),
                "action": str(plan["action"]),
                "precondition_digest": str(plan["precondition_digest"]),
                "fixable": bool(plan["fixable"]),
                "undoable": False,
            }, str(plan["finding_id"]))

        try:
            return store.mutate("audit_plan_claim", str(plan["plan_id"]), request, apply)
        except GroupControlError as exc:
            raise AuditPlanError(exc.code) from exc

    def _finalize(self, plan: Mapping[str, Any], repair: Mapping[str, Any], *, recovered: bool) -> dict[str, Any]:
        request = {
            "plan_id": str(plan["plan_id"]),
            "finding_id": str(plan["finding_id"]),
            "precondition_digest": str(plan["precondition_digest"]),
        }
        store = SystemControlStore(self.workspace, write=True)

        def apply(_conn: Any):
            return ({
                "ok": True,
                "status": "succeeded",
                "plan_id": str(plan["plan_id"]),
                "change_id": str(plan["plan_id"]),
                "finding_id": str(plan["finding_id"]),
                "action": str(plan["action"]),
                "undoable": False,
                "recovered": bool(recovered),
                "processed_count": int(repair.get("processed_count") or 0),
                "remaining_count": int(repair.get("remaining_count") or 0),
            }, str(plan["finding_id"]))

        try:
            return store.mutate("audit_plan_apply", str(plan["plan_id"]), request, apply)
        except GroupControlError as exc:
            raise AuditPlanError(exc.code) from exc

    def _repair(self, action: str) -> dict[str, Any]:
        if not self.layout.evidence_db.is_file():
            raise AuditPlanError("audit_plan_dependency_missing")
        if action == "project_memory_outbox":
            if not self.layout.memory_db.is_file():
                raise AuditPlanError("audit_plan_dependency_missing")
            try:
                memory = MemoryAtomStore(self.workspace, readonly=False)
                evidence = EvidenceStore(self.workspace, readonly=False)
                result = memory.project_evidence(evidence)
            except Exception as exc:
                raise AuditPlanError("memory_outbox_projection_failed") from exc
            return {
                "processed_count": int(result.get("projected") or 0),
                "remaining_count": int(result.get("pending") or 0),
            }
        if action == "project_rule_evidence_outbox":
            if not self.layout.rules_db.is_file():
                raise AuditPlanError("audit_plan_dependency_missing")
            try:
                rules = RuleV2Store(self.workspace)
                evidence = EvidenceStore(self.workspace, readonly=False)
                result = EvidenceProjector(rules, evidence).project()
            except Exception as exc:
                raise AuditPlanError("rule_outbox_projection_failed") from exc
            return {
                "processed_count": int(result.get("consumed") or 0),
                "remaining_count": int(result.get("pending") or 0),
            }
        raise AuditPlanError("audit_plan_not_fixable")

    def apply(self, plan_id: str) -> dict[str, Any]:
        target = str(plan_id or "").strip()
        if not target:
            raise AuditPlanError("plan_id_required")
        # A final receipt is the authoritative replay result.
        try:
            receipt = self.system.read_receipt("audit_plan_apply", target)
        except GroupControlError as exc:
            raise AuditPlanError(exc.code) from exc
        if receipt is not None:
            result = dict(receipt["result"])
            result["replayed"] = True
            return result

        result = self._audit()
        located = self._find_plan(target, result)
        claim_receipt = self.system.read_receipt("audit_plan_claim", target)
        if located is None:
            if claim_receipt is None:
                raise AuditPlanError("audit_plan_not_found_or_stale")
            plan = dict(claim_receipt["result"])
            if not bool(plan.get("fixable")):
                raise AuditPlanError("audit_plan_not_fixable")
            # A crash may have happened after the projector completed but before
            # final receipt commit. If the finding is already gone, finalize it.
            finding_id = str(plan.get("finding_id") or "")
            if any(_finding_id(item) == finding_id for item in result.blockers):
                raise AuditPlanError("audit_plan_stale")
            return self._finalize(plan, {"processed_count": 0, "remaining_count": 0}, recovered=True)

        blocker, plan = located
        if not bool(plan["fixable"]):
            raise AuditPlanError("audit_plan_not_fixable")
        if claim_receipt is None:
            self._claim(plan)
        elif str(claim_receipt["result"].get("precondition_digest") or "") != str(plan["precondition_digest"]):
            raise AuditPlanError("audit_plan_stale")

        repair = self._repair(str(plan["action"]))
        after = self._audit()
        if any(_finding_id(item) == str(plan["finding_id"]) for item in after.blockers):
            raise AuditPlanError("audit_plan_not_resolved")
        return self._finalize(plan, repair, recovered=False)

    def undo(self, change_id: str) -> dict[str, Any]:
        target = str(change_id or "").strip()
        if not target:
            raise AuditPlanError("change_id_required")
        try:
            receipt = self.system.read_receipt("audit_plan_apply", target)
        except GroupControlError as exc:
            raise AuditPlanError(exc.code) from exc
        if receipt is None:
            raise AuditPlanError("change_not_found")
        if not bool(receipt["result"].get("undoable")):
            raise AuditPlanError("change_not_undoable")
        raise AuditPlanError("audit_undo_unavailable")


__all__ = ["AuditPlanError", "AuditPlanService"]
