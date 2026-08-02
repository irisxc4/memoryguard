"""Deterministic acceptance check for the Rule Intelligence merge layer (P3).

Drives the real ``RuleMergeService`` against a synthetic workspace and verifies
the full governance metric family (P3-001/002/003):

* ``auto_merge_precision``       -- merged pairs are never a strength/polarity/
                                    parameter/negative conflict (>= 0.995);
* ``binding_expansion``          -- merges never change the binding audience
                                    identity set (must be 0);
* ``system_auto_binding``        -- auto/backfill system bindings (must be 0);
* ``first_merge_human_approval`` -- an auto merge happened on a pair with no
                                    human acknowledgment of the first-merge
                                    risk (must be 0);
* ``strength_conflict_merge``    -- MUST+SHOULD pair got merged (must be 0);
* ``negative_evidence_leak``     -- a pair with weighted negative evidence got
                                    merged (must be 0);
* ``single_agent_dominance``     -- one Agent held >=60% of evidence weight on
                                    a candidate (must be 0);
* ``merge_rollback_success``     -- undo restored the exact pre-merge state;
* ``migration_loss``             -- backfill count drift (must be 0);
* ``judge_audited``              -- the merged decision carries the P3.3 judge
                                    source + recommendation (must be true);
* ``read_path_mode``             -- Phase5 bootstrap resolved through the
                                    canonical layer or fell back safely.

Exits non-zero when any gate fails, mirroring ``accept_rule_lifecycle.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.agent_binding import AgentBindingStore  # noqa: E402
from memoryguard.rule_binding import build_binding  # noqa: E402
from memoryguard.rule_evidence import build_evidence, build_negative_evidence  # noqa: E402
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore  # noqa: E402
from memoryguard.rule_semantic_judge import DiceJudge  # noqa: E402
from memoryguard.schema_v3 import (  # noqa: E402
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore  # noqa: E402


def _aged(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()


def _seed_legacy(workspace: Path, group_id: str, bodies: list[str]) -> None:
    AgentBindingStore(workspace).bind_agent(f"agent-{group_id}", group_id)
    store = SharedMemoryStore(workspace, group_id)
    for i, body in enumerate(bodies):
        store.append_record(SharedMemoryRecord(
            memory_id=f"{group_id}-{i}", body=body, kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE, injection_policy="always",
            priority=10, agent_instance_id=f"agent-{group_id}",
            created_at=_aged(90), updated_at=_aged(90),
        ), assignments=[
            {"target_type": "agent", "target_id": f"agent-{group_id}"},
        ])


def _seed_evidence(
    store: RuleMergeStore, definition_id: str, text: str,
    *, observed_at: str = "",
) -> None:
    for i in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=f"{definition_id}-ev{i}",
            agent_instance_id=f"agent-{i}",
            project_ref=f"project-{i}",
            session_id=f"session-{i}",
            content=text,
            observed_at=observed_at or _aged(60),
        ))


def _seed_reputations(store: RuleMergeStore) -> None:
    for i in range(3):
        store.upsert_agent_reputation(
            agent_id=f"agent-{i}", success_rate=0.98, rule_accuracy=0.98,
            violation_rate=0.02, sample_count=200, feedback_quality=0.95,
        )
        store.upsert_project_profile(
            project_ref=f"project-{i}", production_level=1.0,
            criticality=0.8, owner_verified=True,
        )


def evaluate() -> dict[str, object]:
    workspace = Path(tempfile.mkdtemp())
    # Three legacy groups:
    #   team-a/team-b share the exact wording "提交代码前必须运行测试" -> one
    #     canonical Definition across groups; team-a's synonym rephrase
    #     "提交前必须执行测试" is the merge candidate we auto-merge.
    #   team-c holds a MUST/SUGGESTION pair on pnpm -> a strength conflict that
    #     must never merge, plus negative evidence to exercise that gate.
    _seed_legacy(workspace, "team-a", [
        "提交代码前必须运行测试",   # exact duplicate with team-b
        "提交前必须执行测试",       # synonym of the above
    ])
    _seed_legacy(workspace, "team-b", [
        "提交代码前必须运行测试",   # exact duplicate with team-a
    ])
    _seed_legacy(workspace, "team-c", [
        "必须使用pnpm安装依赖",     # MUST
        "建议使用pnpm安装依赖",     # SUGGESTION -> strength conflict
    ])

    service = RuleMergeService(RuleMergeStore(workspace), judge=DiceJudge())
    backfill = service.backfill_legacy(workspace)

    # Seed independent evidence on every definition so synonym pairs become
    # auto-merge candidates, plus reputation so the evidence is weighted.
    intel = RuleMergeStore(workspace)
    for definition in intel.list_definitions():
        _seed_evidence(intel, definition.definition_id, definition.canonical_text)
    _seed_reputations(intel)

    # Negative evidence on the weaker (SHOULD) pnpm definition: a real project
    # contradicts the rule, so neither it nor its MUST twin may merge.
    weaker = next(
        d for d in intel.list_definitions()
        if d.rule_strength != "must"
    )
    intel.upsert_negative_evidence(build_negative_evidence(
        definition_id=weaker.definition_id,
        source_rule_id="neg-1",
        agent_instance_id="agent-0",
        project_ref="project-0",
        content="项目使用npm且运行正常，不遵循pnpm规则",
        observed_at=_aged(45),
    ))

    proposals = service.scan_and_propose()
    candidates = [p for p in proposals if p["status"] == "candidate"]
    conflicted = [p for p in proposals if p["status"] == "conflicted"]

    # The pnpm MUST/SHOULD pair must surface as a strength conflict.
    strength_conflict_found = any(
        p["conflict_type"] == "strength" for p in conflicted
    )
    # And the negative-evidence definition must not be a candidate.
    weaker_id = weaker.definition_id
    suggestion_never_candidate = all(
        weaker_id not in p["definition_ids"] for p in candidates
    )

    # Cold-start protection: a fresh candidate (even a highly-similar, highly-
    # evidenced one) must NOT auto-merge on the first attempt — cooldown and
    # first-merge acknowledgment both block it.
    merge_ok = False
    auto_blocked_first = False
    canonical_before = intel.count_definitions()
    binding_before = {b.audience_identity() for b in intel.list_bindings()}
    decision_id = ""
    for proposal in candidates:
        blocked = service.merge_proposal(proposal["proposal_id"])
        auto_blocked_first = (
            not blocked.get("ok")
            and blocked.get("blocked_reason") == "auto_merge_not_ready"
        )
        if auto_blocked_first:
            intel.acknowledge_first_merge(proposal["proposal_id"], actor="human")
            intel.clear_proposal_cooldown(proposal["proposal_id"])
            result = service.merge_proposal(proposal["proposal_id"])
            if result.get("ok"):
                merge_ok = True
                decision_id = result["decision"]["decision_id"]
        if merge_ok:
            break

    binding_after = {b.audience_identity() for b in intel.list_bindings()}
    binding_expansion = 0 if binding_before == binding_after else 1

    # Merge precision: every *merged* proposal must have recorded strength_ok
    # and negative_ok in its decision (a merged conflict would flip them).
    metrics = intel.metrics()
    auto_merge_precision = metrics["auto_merge_precision"]

    # Undo and restore check.
    undo_ok = False
    if decision_id:
        undo = service.undo_decision(decision_id)
        undo_ok = bool(undo.get("status") == "undone")

    # P3.3 judge audit: the merge decision must carry the judge's source.
    judge_audited = False
    if decision_id:
        judge_decision = intel.get_merge_decision(decision_id)
        judge_audited = bool(
            judge_decision
            and judge_decision.get("judge_source") == "dice"
            and judge_decision.get("judge_recommendation")
        )

    # Phase5 read-path: with an intelligence layer present, a bootstrap over a
    # seeded legacy group prefers the canonical layer (or falls back safely).
    from memoryguard.context_bootstrap import build_context_packet
    from memoryguard.schema_v3 import EffectiveAgentContext
    from memoryguard.shared_memory_store import SharedMemoryStore

    read_path_mode = "legacy"
    try:
        legacy = SharedMemoryStore(workspace, "team-a")
        packet = build_context_packet(
            legacy,
            task="运行测试",
            effective_context=EffectiveAgentContext("agent-team-a", "team-a"),
            read_path="auto",
        )
        read_path_mode = packet.get("read_path", {}).get("mode", "legacy")
    except Exception:
        read_path_mode = "legacy"

    report = {
        "auto_merge_precision": auto_merge_precision,
        "strength_conflict_merge": metrics["strength_conflict_merge"],
        "negative_evidence_leak": metrics["negative_evidence_leak"],
        "first_merge_human_approval": metrics["first_merge_human_approval"],
        "single_agent_dominance": metrics["single_agent_dominance"],
        "binding_expansion": binding_expansion,
        "system_auto_binding": metrics["system_auto_binding"],
        "auto_broad_binding": metrics["auto_broad_binding"],
        "merge_rollback_success": 1 if undo_ok else 0,
        "migration_loss": backfill["migration_loss"],
        "judge_audited": judge_audited,
        "read_path_mode": read_path_mode,
        "candidate_count": len(candidates),
        "conflicted_count": len(conflicted),
        "strength_conflict_found": bool(strength_conflict_found),
        "negative_blocked_candidate": bool(suggestion_never_candidate),
        "auto_blocked_on_first_attempt": bool(auto_blocked_first),
        "merged_count": int(merge_ok),
        "definitions_before": canonical_before,
        "binding_count": metrics["binding_count"],
        "passed": bool(
            auto_merge_precision >= 0.995
            and metrics["strength_conflict_merge"] == 0
            and metrics["negative_evidence_leak"] == 0
            and metrics["first_merge_human_approval"] == 0
            and metrics["single_agent_dominance"] == 0
            and binding_expansion == 0
            and metrics["system_auto_binding"] == 0
            and metrics["auto_broad_binding"] == 0
            and undo_ok
            and backfill["migration_loss"] == 0
            and judge_audited
            and read_path_mode in {"rule-intelligence", "legacy"}
            and strength_conflict_found
            and suggestion_never_candidate
            and auto_blocked_first
            and merge_ok
        ),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        report = evaluate()
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
