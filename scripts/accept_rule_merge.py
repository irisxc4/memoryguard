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
* ``trusted_session_receipt_missing`` -- trusted bootstrap receipts are
                                         durable and retain trusted provenance;
* ``binding_source_projection_diff``  -- materialized bindings equal active
                                         source contributions;
* ``evidence_fallback_loss``          -- canonical read never loses a source
                                         while falling back to legacy;
* ``readiness_snapshot_diff``         -- replaying public readiness inputs is
                                         deterministic;
* ``projection_watermark_regression`` -- outbox/projection watermarks never
                                         move backwards or remain lagged;
* ``unrelated_undo_conflict``         -- undo of one merge survives another
                                         independent merge;
* ``v1_collision_binding_leak``       -- V1 strength collision keeps source
                                         bindings on their own V2 definitions;
* ``v1_collision_runtime_leak``       -- V1 strength collision keeps runtime
                                         projections on their own V2 definitions;
* ``untrusted_merge_waiver``          -- human approval cannot waive
                                         untrusted-session evidence;
* ``shadow_permission_false_positive`` -- exact legacy audience has no false
                                         shadow permission diff.
* ``pending_unlinked_group``            -- an outbox event for a group that
                                         has not been linked is not consumed
                                         or dropped before backfill.
* ``unlinked_negative_feedback``        -- a negative feedback event survives
                                         an unlinked phase and reaches its
                                         canonical Definition after backfill.
* ``new_source_canonical_route``        -- a new source whose old Definition
                                         was merged routes to the active
                                         canonical Definition.
* ``inactive_binding_target``           -- retracting one source does not
                                         revoke another source's binding.
* ``strength_evolution_contribution_diff`` -- strength evolution preserves
                                         source-contribution ownership.
* ``strength_evolution_rollback``       -- a rejected evolution is an exact
                                         no-op over the public state.
* ``public_positive_runner_up`` / ``public_negative_runner_up`` -- removing
                                         a public feedback winner restores the
                                         retained runner-up of either polarity.
* ``all_evidence_writes_contributions`` -- direct positive and negative
                                         evidence writes are represented in
                                         the public contribution ledger.
* ``duplicate_receipt_independence`` / ``distinct_session_independence`` --
                                         duplicate receipts collapse while
                                         distinct trusted sessions remain
                                         independent.
* ``exact_wide_shadow_diff``            -- a lossless wide legacy audience is
                                         not called a permission expansion.
* ``true_permission_expansion``         -- a genuinely broader binding is
                                         detected by shadow verification.
* ``exact_system_migration_audience_diff`` -- legacy system audience
                                         multiset survives real backfill.
* ``true_system_expansion_missed``      -- system-only shadow expansion is
                                         detected; raw expansion is reported.
* ``backfill_real_migration_loss``      -- a new unbackfilled source is
                                         detected, then disappears after the
                                         real backfill API runs.
* ``unrelated_group_readiness``         -- one group's lag cannot block an
                                         unrelated healthy group.

Exits non-zero when any gate fails, mirroring ``accept_rule_lifecycle.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memoryguard.access_context import AccessContext  # noqa: E402
from memoryguard.agent_binding import AgentBindingStore  # noqa: E402
from memoryguard.rule_binding import build_binding  # noqa: E402
from memoryguard.rule_definition import (  # noqa: E402
    RuleDefinition,
    build_definition,
    normalize_rule_text,
)
from memoryguard.rule_evidence import build_evidence, build_negative_evidence  # noqa: E402
from memoryguard.rule_merge import RuleMergeService, RuleMergeStore  # noqa: E402
from memoryguard.rule_merge_policy import build_readiness_snapshot  # noqa: E402
from memoryguard.rule_read_path import RuleReadPath  # noqa: E402
from memoryguard.rule_semantic_judge import DiceJudge  # noqa: E402
from memoryguard.rule_scope import canonical_project_ref, normalize_assignment  # noqa: E402
from memoryguard.schema_v3 import (  # noqa: E402
    EffectiveAgentContext,
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
    stable_hash,
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
    *, source_rule_id: str = "", observed_at: str = "",
) -> None:
    for i in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=definition_id,
            source_rule_id=source_rule_id or f"{definition_id}-ev{i}",
            agent_instance_id=f"agent-{i}",
            project_ref=f"project-{i}",
            session_id=f"session-{i}",
            session_trusted=True,
            content=text,
            observed_at=observed_at or _aged(60),
        ))


# team-a's record bodies in memory_id order: the canonical read-path check
# resolves evidence source ids back to these legacy memory ids, so evidence on
# team-a's definitions must carry the real memory ids or the canonical read
# would (correctly) refuse to resolve them.
_TEAM_A_BODIES = ("提交代码前必须运行测试", "提交前必须执行测试")


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


def _seed_rule(
    workspace: Path,
    group_id: str,
    memory_id: str,
    body: str,
    *,
    agent_id: str = "agent-a",
    with_receipt: bool = False,
    trusted_session: bool = True,
    assignment: dict[str, str] | None = None,
) -> SharedMemoryStore:
    """Seed one real legacy record, audience, and optional match receipt."""
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    store = SharedMemoryStore(workspace, group_id)
    store.append_record(
        SharedMemoryRecord(
            memory_id=memory_id,
            body=body,
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            agent_instance_id=agent_id,
            created_at=_aged(90),
            updated_at=_aged(90),
        ),
        assignments=[assignment or {
            "target_type": "agent",
            "target_id": agent_id,
        }],
    )
    if with_receipt:
        receipt = RuleMatchReceipt(
            receipt_id=f"receipt-{memory_id}",
            memory_id=memory_id,
            share_group_id=group_id,
            agent_instance_id=agent_id,
            task_hash=f"task-{memory_id}",
            task="acceptance task",
            session_id=f"session-{memory_id}",
            session_trusted=trusted_session,
            session_source="host" if trusted_session else "absent",
            project_ref="project-a",
            provider="codex",
            runtime_role="worker",
            context_hash=f"context-{memory_id}",
        )
        store.append_rule_match_receipt(receipt)
    return store


def _reconcile_group(workspace: Path, group_id: str) -> dict[str, Any]:
    """Drive a group through the real reconciliation saga.

    Req8 gates the canonical read on group-level canonical activation AND
    ``canonical_reconciliation_status(...).canonical_ready``.  A backfill-only
    group therefore falls back to legacy by design; the read-path checks must
    first establish the full canonical state so the explicitly requested
    canonical read genuinely engages (PR7).
    """
    from memoryguard.rule_reconciliation import (  # noqa: PLC0415
        RuleReconciliationService,
        _active_mandatory,
        build_bundles,
    )
    store = RuleMergeStore(workspace)
    legacy = SharedMemoryStore(workspace, group_id)
    active = _active_mandatory(legacy)
    if not active:
        raise RuntimeError(
            f"no active mandatory records to reconcile for {group_id}"
        )
    plan = build_bundles(store, legacy, group_id, active)
    service = RuleReconciliationService(store, workspace=workspace)
    return service.run(group_id, bundle_plan=plan, model_mode="scripted")


def _binding_source_projection_diff(store: RuleMergeStore) -> int:
    bindings = {
        (item.binding_id, item.definition_id)
        for item in store.list_bindings(status="active")
    }
    contributions = {
        (str(item.get("binding_id", "")), str(item.get("definition_id", "")))
        for item in store.list_binding_contributions(active=True)
    }
    return len(bindings.symmetric_difference(contributions))


def _audience_key(value: object, share_group_id: str) -> tuple[object, ...]:
    """Normalize legacy assignments and materialized bindings to one audience key."""
    if hasattr(value, "priority_override"):
        value = normalize_assignment(value)  # type: ignore[arg-type]
    target_type = str(getattr(value, "target_type", "") or "")
    target_id = str(getattr(value, "target_id", "") or "")
    project_ref = canonical_project_ref(
        str(getattr(value, "project_ref", "") or "")
    )
    provider = str(getattr(value, "provider", "") or "")
    runtime_role = str(getattr(value, "runtime_role", "") or "")
    effect = str(getattr(value, "effect", "include") or "include")
    priority = getattr(value, "priority", None)
    if priority is None:
        priority = getattr(value, "priority_override", 0)
    priority = int(priority or 0)
    if target_type == "project":
        project_ref = project_ref or canonical_project_ref(target_id)
        target_id = ""
    if target_type == "provider" and not provider:
        provider = target_id
    if target_type == "runtime_role" and not runtime_role:
        runtime_role = target_id
    return (
        str(share_group_id or ""), target_type, target_id, project_ref,
        provider.casefold(), runtime_role.casefold(), effect, priority,
    )


def _audience_multiset(
    values: list[object], share_group_id: str,
) -> Counter[tuple[object, ...]]:
    return Counter(_audience_key(value, share_group_id) for value in values)


def _acceptance_context() -> AccessContext:
    """Return the trusted principal used by this synthetic acceptance run."""
    return AccessContext(
        trusted_agent_id="acceptance-admin",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
    )


def _trusted_session_receipt_missing() -> int:
    """Exercise MCP bootstrap's trusted receipt writer end to end."""
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id = "trusted-group", "trusted-agent"
    task = "运行测试"
    legacy = _seed_rule(
        workspace, group_id, "mandatory", "提交前必须运行测试",
        agent_id=agent_id,
    )
    # The MCP access resolver takes identity from this binding and the trusted
    # host launch environment.  Restore process state after the probe.
    env = {
        "MEMORYGUARD_WORKSPACE": str(workspace),
        "MEMORYGUARD_AGENT_ID": agent_id,
        "MEMORYGUARD_STRICT_BINDING": "1",
        "MEMORYGUARD_SESSION_ID": "trusted-session",
        "MEMORYGUARD_SESSION_SOURCE": "host",
        "MEMORYGUARD_CONTEXT_HASH": "trusted-context",
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_RUNTIME_ROLE": "worker",
        "MEMORYGUARD_PROJECT_CWD": str(workspace / "project"),
    }
    previous = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        from memoryguard.mcp_server import execute_tool

        result = execute_tool(
            "memoryguard_context_bootstrap", {"task": task},
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result.get("isError"):
        raise RuntimeError(result["content"][0].get("text", "bootstrap failed"))
    packet = json.loads(result["content"][0]["text"])
    expected = set(packet.get("mandatory_rule_ids", []))
    raw_receipts = {
        str(item.get("memory_id", "")): item
        for item in packet.get("mandatory_match_receipts", [])
    }
    persisted = {
        receipt.memory_id: receipt
        for receipt in legacy.list_rule_match_receipts()
    }
    persistence = packet.get("receipt_persistence", {})
    missing = int(not expected)
    if (
        persistence.get("status") != "persisted"
        or int(persistence.get("count", 0)) != len(expected)
    ):
        missing += 1
    for memory_id in expected:
        raw = raw_receipts.get(memory_id)
        saved = persisted.get(memory_id)
        if (
            raw is None
            or saved is None
            or not str(saved.receipt_id or "")
            or str(raw.get("receipt_id", "")) != saved.receipt_id
            or raw.get("session_id") != env["MEMORYGUARD_SESSION_ID"]
            or raw.get("session_trusted") is not True
            or raw.get("session_source") != env["MEMORYGUARD_SESSION_SOURCE"]
            or not saved.session_id
            or not saved.session_trusted
            or saved.session_source != env["MEMORYGUARD_SESSION_SOURCE"]
            or saved.to_dict() != raw
        ):
            missing += 1
    return missing


def _evidence_fallback_loss() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "read-group", "agent-read", "read-rule"
    legacy = _seed_rule(
        workspace, group_id, memory_id, "提交前必须运行测试",
        agent_id=agent_id, with_receipt=True,
    )
    intel = RuleMergeStore(workspace)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group_id)
    # Req8: the canonical read only engages after group-level canonical
    # activation + readiness.  Establish it via the real saga, so a canonical
    # read that resolves every known source is what this metric observes.
    _reconcile_group(workspace, group_id)
    context = EffectiveAgentContext(
        agent_instance_id=agent_id,
        share_group_id=group_id,
        project_ref="project-a",
        provider="codex",
        runtime_role="worker",
        session_id="session-read-rule",
        context_hash="context-read-rule",
    )
    source_ids = {memory_id}
    read = RuleReadPath(workspace, group_id)
    mapping = read.resolve_canonical_map(
        known_memory_ids=source_ids,
        legacy_store=legacy,
        context=context,
    )
    from memoryguard.context_bootstrap import build_context_packet

    packet = build_context_packet(
        legacy,
        task="运行测试",
        effective_context=context,
        read_path="rule-intelligence",
    )
    if mapping is None:
        return len(source_ids)
    missing = source_ids - set(mapping.get("memory_to_definition", {}))
    if packet.get("read_path", {}).get("mode") != "rule-intelligence":
        return len(source_ids) + len(missing)
    return len(missing)


def _readiness_snapshot_diff() -> int:
    workspace = Path(tempfile.mkdtemp())
    store = RuleMergeStore(workspace)
    definition = build_definition("提交前必须运行测试", created_at=_aged(90))
    store.upsert_definition(definition)
    for index in range(3):
        store.upsert_evidence(build_evidence(
            definition_id=definition.definition_id,
            source_rule_id=f"readiness-source-{index}",
            agent_instance_id=f"readiness-agent-{index}",
            project_ref=f"readiness-project-{index}",
            session_id=f"readiness-session-{index}",
            session_trusted=True,
            content=definition.canonical_text,
            observed_at=_aged(60),
        ))
    store.upsert_runtime_feedback(
        feedback_id="readiness-feedback",
        definition_id=definition.definition_id,
        receipt_id="readiness-receipt",
        outcome="followed",
        agent_instance_id="readiness-agent-0",
        project_ref="readiness-project-0",
        session_id="readiness-session-0",
        session_trusted=1,
        created_at=_aged(45),
    )
    store.recompute_runtime_stats(definition.definition_id)
    store.upsert_agent_reputation(
        agent_id="readiness-agent-0", success_rate=0.98,
        rule_accuracy=0.98, sample_count=200, feedback_quality=0.95,
    )
    store.upsert_project_profile(
        project_ref="readiness-project-0", production_level=1.0,
        criticality=0.8, owner_verified=True,
    )

    def snapshot() -> dict[str, object]:
        current = store.get_definition(definition.definition_id)
        if current is None:
            raise RuntimeError("readiness definition disappeared")
        return build_readiness_snapshot(
            definition=current.to_dict(),
            evidence={
                "items": [
                    item.to_dict()
                    for item in store.list_evidence(definition.definition_id)
                ],
            },
            runtime=store.get_runtime_stats(definition.definition_id),
            reputation=store.get_agent_reputation("readiness-agent-0"),
            project=store.get_project_profile("readiness-project-0"),
            similarity={"duplicate_score": 0.9},
        )

    first = snapshot()
    second = snapshot()
    return int(first.get("digest") != second.get("digest"))


def _projection_watermark_regression() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, memory_id = "watermark-group", "watermark-rule"
    legacy = _seed_rule(
        workspace, group_id, memory_id, "提交前必须运行测试",
        agent_id="agent-watermark", with_receipt=True,
    )
    intel = RuleMergeStore(workspace)
    service = RuleMergeService(intel)
    service.backfill_group(legacy, group_id)
    before = legacy.rule_event_high_water()
    legacy.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id="watermark-feedback",
        receipt_id=f"receipt-{memory_id}",
        outcome="followed",
        actor="agent-watermark",
        source="agent",
        authority=3,
    ))
    queued = legacy.rule_event_high_water()
    service.consume_outbox(workspace)
    consumed = legacy.rule_event_high_water()
    service.consume_outbox(workspace)
    repeated = legacy.rule_event_high_water()
    status = intel.projection_status()
    regression = 0
    for left, right in ((before, queued), (queued, consumed), (consumed, repeated)):
        for field in ("total", "max_rowid", "latest_rowid"):
            if int(right.get(field, 0)) < int(left.get(field, 0)):
                regression += 1
    if int(consumed.get("pending", 0)) != 0:
        regression += int(consumed.get("pending", 0))
    if int(status.get("projection_lag", 0)) != 0 or status.get(
        "projection_error",
    ):
        regression += 1
    return regression


def _merge_pair(
    service: RuleMergeService,
    store: RuleMergeStore,
    definition_ids: list[str],
) -> dict[str, object]:
    proposals = service.scan_and_propose(definition_ids=definition_ids)
    candidates = [
        item for item in proposals
        if item.get("status") == "candidate"
        and set(item.get("definition_ids", [])) == set(definition_ids)
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected one merge candidate, got {len(candidates)}")
    proposal_id = candidates[0]["proposal_id"]
    context = _acceptance_context()
    capability = store.issue_merge_capability(proposal_id, context)
    store.approve_proposal(
        proposal_id,
        approved_by=context.principal,
        capability_token=capability,
        access_context=context,
    )
    result = service.merge_proposal(proposal_id, actor=context.principal)
    if not result.get("ok"):
        raise RuntimeError(
            f"merge candidate blocked: {result.get('blocked_reason', result)}"
        )
    return result


def _seed_merge_pair(
    workspace: Path,
    legacy: SharedMemoryStore,
    store: RuleMergeStore,
    service: RuleMergeService,
    group_id: str,
    pair_index: int,
    bodies: tuple[str, str],
) -> list[str]:
    records = [
        legacy.get_record(f"pair-{pair_index}-{index}")
        for index in range(2)
    ]
    if any(record is None for record in records):
        raise RuntimeError("merge pair record missing")
    service.backfill_group(legacy, group_id)
    wanted = {
        normalize_rule_text(body): body
        for body in bodies
    }
    definitions = [
        next(
            definition for definition in store.list_definitions()
            if definition.canonical_text == normalized
        )
        for normalized in wanted
    ]
    _seed_reputations(store)
    for definition, record, body in zip(definitions, records, bodies):
        for index in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=record.memory_id,
                agent_instance_id=f"agent-{index}",
                project_ref=f"project-{index}",
                session_id=f"pair-{pair_index}-session-{index}",
                session_trusted=True,
                content=body,
                observed_at=_aged(60),
            ))
        for index in range(20):
            store.upsert_runtime_feedback(
                feedback_id=f"pair-{pair_index}-{definition.definition_id}-{index}",
                definition_id=definition.definition_id,
                receipt_id=f"pair-{pair_index}-receipt-{definition.definition_id}-{index}",
                outcome="followed",
                agent_instance_id=f"agent-{index % 3}",
                project_ref=f"project-{index % 3}",
                session_id=f"pair-{pair_index}-runtime-{definition.definition_id}-{index}",
                source="user",
                authority=4,
                session_trusted=1,
                created_at=_aged(45),
            )
        store.recompute_runtime_stats(definition.definition_id)
    return [definition.definition_id for definition in definitions]


def _unrelated_undo_conflict() -> int:
    workspace = Path(tempfile.mkdtemp())
    independent_workspace = Path(tempfile.mkdtemp())
    group_id = "undo-group"
    independent_group_id = "independent-undo-group"
    agent_id = "agent-undo"
    independent_agent_id = "agent-independent-undo"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    AgentBindingStore(independent_workspace).bind_agent(
        independent_agent_id, independent_group_id,
    )
    legacy = SharedMemoryStore(workspace, group_id)
    independent_legacy = SharedMemoryStore(
        independent_workspace, independent_group_id,
    )
    bodies = (
        "提交代码前必须运行测试", "提交前必须执行测试",
        "发布代码前必须运行测试", "发布前必须执行测试",
    )
    for target, target_agent, pair_bodies in (
        (legacy, agent_id, bodies[:2]),
        (independent_legacy, independent_agent_id, bodies[2:]),
    ):
        for index, body in enumerate(pair_bodies):
            target.append_record(SharedMemoryRecord(
                memory_id=f"pair-0-{index}",
                body=body,
                kind=MemoryKind.PROCEDURE,
                status=SharedMemoryStatus.ACTIVE,
                injection_policy="always",
                priority=10,
                agent_instance_id=target_agent,
                created_at=_aged(90),
                updated_at=_aged(90),
            ), assignments=[{
                "target_type": "agent", "target_id": target_agent,
            }])
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store, judge=DiceJudge())
    first_ids = _seed_merge_pair(
        workspace, legacy, store, service, group_id, 0, bodies[:2],
    )
    independent_store = RuleMergeStore(independent_workspace)
    independent_service = RuleMergeService(independent_store, judge=DiceJudge())
    second_ids = _seed_merge_pair(
        independent_workspace, independent_legacy, independent_store,
        independent_service, independent_group_id, 0, bodies[2:],
    )
    first = _merge_pair(service, store, first_ids)
    second = _merge_pair(independent_service, independent_store, second_ids)
    first_decision = first.get("decision", {})
    second_decision = second.get("decision", {})
    undo = service.undo_decision(str(first_decision.get("decision_id", "")))
    second_canonical = independent_store.get_definition(
        str(second_decision.get("canonical_definition_id", "")),
    )
    second_merged_ids = second_decision.get("merged_definition_ids", [])
    second_merged = [
        independent_store.get_definition(str(item))
        for item in second_merged_ids
    ]
    independent_state_ok = bool(
        second_canonical is not None
        and second_canonical.status == "active"
        and all(item is not None and item.status == "merged" for item in second_merged)
    )
    return int(undo.get("status") != "undone" or not independent_state_ok)


def _v1_collision_leaks() -> tuple[int, int]:
    workspace = Path(tempfile.mkdtemp())
    group_id = "collision-group"
    AgentBindingStore(workspace).bind_agent("agent-collision", group_id)
    legacy = SharedMemoryStore(workspace, group_id)
    bodies = {"must": "必须运行测试", "should": "应该运行测试"}
    for source_id, body in bodies.items():
        legacy.append_record(SharedMemoryRecord(
            memory_id=source_id,
            body=body,
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            agent_instance_id=f"agent-{source_id}",
            created_at=_aged(90),
            updated_at=_aged(90),
        ), assignments=[{
            "target_type": "agent",
            "target_id": f"agent-{source_id}",
        }])
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store)
    expected = {
        source_id: service._definition_from_record(legacy.get_record(source_id))
        for source_id in bodies
    }
    legacy_id = stable_hash(
        "rule-definition", "canonical", normalize_rule_text(bodies["must"]),
    )
    store.upsert_definition(RuleDefinition.from_dict({
        **expected["must"].to_dict(),
        "definition_id": legacy_id,
        "rule_strength": "observation",
    }))
    for source_id, body in bodies.items():
        store.replace_source_contributions(group_id, source_id, [build_binding(
            legacy_id,
            share_group_id=group_id,
            target_type="agent",
            target_id=f"agent-{source_id}",
            owner_agent_id=f"agent-{source_id}",
            created_by="backfill",
        )])
        store.upsert_evidence(build_evidence(
            definition_id=legacy_id,
            source_rule_id=source_id,
            agent_instance_id=f"agent-{source_id}",
            project_ref=f"project-{source_id}",
            session_id=f"session-{source_id}",
            receipt_id=f"receipt-{source_id}",
            feedback_id=f"feedback-{source_id}",
            session_trusted=True,
            content=body,
            observed_at=_aged(60),
        ))
        store.upsert_runtime_feedback(
            feedback_id=f"feedback-{source_id}",
            definition_id=legacy_id,
            receipt_id=f"receipt-{source_id}",
            outcome="followed",
            agent_instance_id=f"agent-{source_id}",
            project_ref=f"project-{source_id}",
            session_id=f"session-{source_id}",
            session_trusted=1,
        )
        store.upsert_effective_feedback_projection(
            receipt_id=f"receipt-{source_id}",
            effective_feedback_id=f"feedback-{source_id}",
            definition_id=legacy_id,
            outcome="followed",
            session_trusted=1,
            session_source="host",
        )
    service.backfill_group(legacy, group_id)

    binding_leak = 0
    for source_id, definition in expected.items():
        rows = [
            row for row in store.list_binding_contributions(active=True)
            if row.get("source_memory_id") == source_id
        ]
        if len(rows) != 1 or rows[0].get("definition_id") != definition.definition_id:
            binding_leak += 1

    runtime_leak = 0
    for source_id, definition in expected.items():
        projection = store.get_effective_feedback_projection(
            f"receipt-{source_id}",
        )
        stats = store.recompute_runtime_stats(definition.definition_id)
        link = store.get_source_link(group_id, source_id)
        if (
            projection is None
            or projection.get("definition_id") != definition.definition_id
            or not stats
            or int(stats.get("followed", 0)) != 1
            or link is None
            or link.get("canonical_definition_id") != definition.definition_id
        ):
            runtime_leak += 1
    return binding_leak, runtime_leak


def _untrusted_merge_waiver() -> int:
    workspace = Path(tempfile.mkdtemp())
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store, judge=DiceJudge())
    definitions = [
        build_definition("提交代码前必须运行测试", created_at=_aged(90)),
        build_definition("提交前必须执行测试", created_at=_aged(90)),
    ]
    for definition in definitions:
        store.upsert_definition(definition)
        for index in range(3):
            store.upsert_evidence(build_evidence(
                definition_id=definition.definition_id,
                source_rule_id=f"untrusted-{definition.definition_id}-{index}",
                agent_instance_id=f"untrusted-agent-{index}",
                project_ref=f"untrusted-project-{index}",
                session_id=f"untrusted-session-{index}",
                session_trusted=False,
                content=definition.canonical_text,
                observed_at=_aged(60),
            ))
    _seed_reputations(store)
    proposals = service.scan_and_propose(
        definition_ids=[item.definition_id for item in definitions],
    )
    candidates = [item for item in proposals if item.get("status") == "candidate"]
    if not candidates:
        return int(bool(store.list_merge_decisions()))
    for proposal in candidates:
        try:
            context = _acceptance_context()
            capability = store.issue_merge_capability(
                proposal["proposal_id"], context,
            )
            store.approve_proposal(
                proposal["proposal_id"],
                approved_by=context.principal,
                capability_token=capability,
                access_context=context,
            )
            result = service.merge_proposal(
                proposal["proposal_id"], actor=context.principal,
            )
            if result.get("ok"):
                return 1
        except (RuntimeError, ValueError):
            continue
    return int(bool(store.list_merge_decisions()))


def _shadow_permission_false_positive() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "shadow-group", "agent-shadow", "shadow-rule"
    legacy = _seed_rule(
        workspace, group_id, memory_id, "提交前必须运行测试",
        agent_id=agent_id, with_receipt=True,
    )
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store)
    service.backfill_group(legacy, group_id)
    context = EffectiveAgentContext(
        agent_instance_id=agent_id,
        share_group_id=group_id,
        project_ref="project-a",
    )
    legacy_records = [
        (record.memory_id, legacy.list_rule_assignments(record.memory_id))
        for record in legacy.list_records()
    ]
    shadow = store.shadow_verify(context, legacy_records)
    return int(shadow.get("permission_diff", 0) != 0)


def _mcp_feedback(
    workspace: Path,
    group_id: str,
    agent_id: str,
    receipt_id: str,
    session_id: str,
    project_ref: str,
    outcome: str,
    *,
    evidence: str = "",
    context_hash: str = "",
) -> dict[str, object]:
    """Submit feedback through the production MCP boundary.

    The acceptance harness deliberately changes only the trusted launch
    context needed by the real resolver, then restores it.  It never calls a
    projection helper or writes a contribution row itself.
    """
    env = {
        "MEMORYGUARD_WORKSPACE": str(workspace),
        "MEMORYGUARD_AGENT_ID": agent_id,
        "MEMORYGUARD_STRICT_BINDING": "1",
        "MEMORYGUARD_ADMIN": "0",
        "MEMORYGUARD_SESSION_ID": session_id,
        "MEMORYGUARD_SESSION_SOURCE": "host",
        "MEMORYGUARD_CONTEXT_HASH": context_hash or f"context-{receipt_id}",
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_RUNTIME_ROLE": "worker",
        "MEMORYGUARD_PROJECT_CWD": project_ref,
    }
    previous = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        from memoryguard.mcp_server import execute_tool

        result = execute_tool(
            "memoryguard_rule_feedback",
            {
                "workspace": str(workspace),
                "receipt_id": receipt_id,
                "outcome": outcome,
                "actor": agent_id,
                "evidence": evidence,
                "idempotency_key": f"acceptance-{receipt_id}-{outcome}",
            },
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result.get("isError"):
        content = result.get("content", [])
        detail = content[0].get("text", "feedback failed") if content else "feedback failed"
        raise RuntimeError(str(detail))
    content = result.get("content", [])
    if not content:
        raise RuntimeError("feedback returned no public response")
    raw = content[0].get("text", "")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"feedback response is not JSON: {raw!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("feedback response is not an object")
    return payload


def _mcp_bootstrap_receipt(
    workspace: Path,
    *,
    group_id: str,
    agent_id: str,
    memory_id: str,
    task: str,
    session_id: str,
    project_ref: str,
    context_hash: str,
) -> dict[str, object]:
    """Create and persist one trusted receipt through the public MCP API."""
    env = {
        "MEMORYGUARD_WORKSPACE": str(workspace),
        "MEMORYGUARD_AGENT_ID": agent_id,
        "MEMORYGUARD_STRICT_BINDING": "1",
        "MEMORYGUARD_ADMIN": "0",
        "MEMORYGUARD_SESSION_ID": session_id,
        "MEMORYGUARD_SESSION_SOURCE": "host",
        "MEMORYGUARD_CONTEXT_HASH": context_hash,
        "MEMORYGUARD_PROVIDER": "codex",
        "MEMORYGUARD_RUNTIME_ROLE": "worker",
        "MEMORYGUARD_PROJECT_CWD": project_ref,
    }
    previous = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        from memoryguard.mcp_server import execute_tool

        result = execute_tool(
            "memoryguard_context_bootstrap",
            {"workspace": str(workspace), "task": task},
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result.get("isError"):
        content = result.get("content", [])
        detail = content[0].get("text", "bootstrap failed") if content else "bootstrap failed"
        raise RuntimeError(str(detail))
    content = result.get("content", [])
    if not content:
        raise RuntimeError("bootstrap returned no public response")
    try:
        payload = json.loads(content[0].get("text", ""))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("bootstrap response is not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("bootstrap response is not an object")
    receipts = [
        item for item in payload.get("mandatory_match_receipts", [])
        if isinstance(item, dict) and item.get("memory_id") == memory_id
    ]
    persistence = payload.get("receipt_persistence", {})
    if (
        len(receipts) != 1
        or not isinstance(persistence, dict)
        or persistence.get("status") != "persisted"
        or receipts[0].get("session_id") != session_id
        or receipts[0].get("session_source") != "host"
        or receipts[0].get("session_trusted") is not True
    ):
        raise RuntimeError("public bootstrap did not return one trusted persisted receipt")
    return receipts[0]


def _append_receipt(
    legacy: SharedMemoryStore,
    *,
    memory_id: str,
    group_id: str,
    agent_id: str,
    receipt_id: str,
    session_id: str,
    project_ref: str,
    context_hash: str = "",
) -> RuleMatchReceipt:
    receipt = RuleMatchReceipt(
        receipt_id=receipt_id,
        memory_id=memory_id,
        share_group_id=group_id,
        agent_instance_id=agent_id,
        task_hash=f"task-{receipt_id}",
        task="acceptance feedback task",
        session_id=session_id,
        session_trusted=True,
        session_source="host",
        project_ref=project_ref,
        provider="codex",
        runtime_role="worker",
        context_hash=context_hash or f"context-{receipt_id}",
        created_at=_now_iso(),
    )
    saved = legacy.append_rule_match_receipt(receipt)
    if saved.receipt_id != receipt_id:
        raise RuntimeError("receipt persistence changed the receipt identity")
    return saved


def _feedback_case(
    workspace: Path,
    *,
    group_id: str,
    agent_id: str,
    memory_id: str,
    body: str = "must run tests before commit",
    assignment: dict[str, str] | None = None,
    backfill: bool = True,
) -> tuple[SharedMemoryStore, RuleMergeStore, RuleMergeService, str]:
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    legacy = SharedMemoryStore(workspace, group_id)
    legacy.append_record(
        SharedMemoryRecord(
            memory_id=memory_id,
            body=body,
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            agent_instance_id=agent_id,
            created_at=_aged(30),
            updated_at=_aged(30),
        ),
        assignments=[assignment or {
            "target_type": "agent",
            "target_id": agent_id,
        }],
    )
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store, judge=DiceJudge())
    if backfill:
        service.backfill_group(legacy, group_id)
    return legacy, store, service, str(workspace / "project")


def _pending_unlinked_group() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "pending-group", "pending-agent", "pending-rule"
    legacy, store, service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
        backfill=False,
    )
    _append_receipt(
        legacy, memory_id=memory_id, group_id=group_id, agent_id=agent_id,
        receipt_id="pending-receipt", session_id="pending-session",
        project_ref=project_ref,
    )
    legacy.append_rule_match_feedback(RuleMatchFeedback(
        feedback_id="pending-feedback", receipt_id="pending-receipt",
        outcome="followed", actor=agent_id, source="agent", authority=3,
    ))
    service.consume_outbox(workspace, only_group=group_id)
    still_pending = any(
        item.get("receipt_id") == "pending-receipt"
        for item in legacy.list_unconsumed_rule_events()
    )
    if not still_pending or store.get_source_link(group_id, memory_id) is not None:
        return 1
    service.backfill_group(legacy, group_id)
    service.consume_outbox(workspace, only_group=group_id)
    return int(
        bool(legacy.list_unconsumed_rule_events())
        or store.get_source_link(group_id, memory_id) is None
    )


def _unlinked_negative_feedback() -> int:
    """Verify the public source-link route and the negative merge hard gate.

    ``_pending_unlinked_group`` covers the fail-closed pre-link barrier with a
    low-level event.  This probe must cover the complementary production path:
    a trusted receipt is created by public bootstrap, public feedback causes a
    durable source link, and the resulting negative evidence blocks a merge.
    """
    if _pending_unlinked_group():
        return 1
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id = "negative-public-group", "negative-public-agent"
    memory_id = "pair-0-0"
    bodies = _TEAM_A_BODIES
    legacy, store, service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
        body=bodies[0],
        backfill=False,
    )
    legacy.append_record(
        SharedMemoryRecord(
            memory_id="pair-0-1",
            body=bodies[1],
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            agent_instance_id=agent_id,
            created_at=_aged(30),
            updated_at=_aged(30),
        ),
        assignments=[{"target_type": "agent", "target_id": agent_id}],
    )
    definition_ids = _seed_merge_pair(
        workspace, legacy, store, service, group_id, 0, bodies,
    )
    receipt = _mcp_bootstrap_receipt(
        workspace,
        group_id=group_id,
        agent_id=agent_id,
        memory_id=memory_id,
        task=bodies[0],
        session_id="negative-public-session",
        project_ref=project_ref,
        context_hash="negative-public-context",
    )
    saved_receipt = legacy.get_rule_match_receipt(str(receipt["receipt_id"]))
    if (
        saved_receipt is None
        or not saved_receipt.session_trusted
        or saved_receipt.session_source != "host"
    ):
        return 1
    _mcp_feedback(
        workspace, group_id, agent_id, str(receipt["receipt_id"]),
        str(receipt["session_id"]), project_ref, "not_applicable",
        evidence="scope mismatch", context_hash=str(receipt["context_hash"]),
    )
    durable_links = [
        item for item in store._list_source_links()
        if item.get("share_group_id") == group_id
        and item.get("memory_id") == memory_id
        and item.get("status") == "active"
    ]
    if len(durable_links) != 1:
        return 1
    link = durable_links[0]
    negative = [
        item for item in store.list_negative_evidence()
        if item.source_rule_id == memory_id
        and item.receipt_id == receipt["receipt_id"]
    ]
    if (
        not negative
        or any(item.definition_id != link["canonical_definition_id"] for item in negative)
        or legacy.list_unconsumed_rule_events()
    ):
        return 1
    proposals = service.scan_and_propose(definition_ids=definition_ids)
    pair = next(
        (
            item for item in proposals
            if set(item.get("definition_ids", [])) == set(definition_ids)
        ),
        None,
    )
    if pair is None or pair.get("status") == "candidate":
        return 1
    blocked = service.merge_proposal(str(pair["proposal_id"]))
    return int(bool(blocked.get("ok")))


def _new_source_canonical_route() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id = "new-source-group", "new-source-agent"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    legacy = SharedMemoryStore(workspace, group_id)
    # Reuse the production acceptance pair: its distinct normalized wording is
    # known to be a real candidate under the current similarity policy.
    bodies = _TEAM_A_BODIES
    for index, body in enumerate(bodies):
        legacy.append_record(
            SharedMemoryRecord(
                memory_id=f"pair-0-{index}", body=body,
                kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
                injection_policy="always", priority=10,
                agent_instance_id=agent_id, created_at=_aged(30),
                updated_at=_aged(30),
            ),
            assignments=[{"target_type": "agent", "target_id": agent_id}],
        )
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store, judge=DiceJudge())
    definition_ids = _seed_merge_pair(
        workspace, legacy, store, service, group_id, 0, bodies,
    )
    merged = _merge_pair(service, store, definition_ids)
    decision = merged["decision"]
    canonical_id = str(decision["canonical_definition_id"])
    old_id = str(decision["merged_definition_ids"][0])
    old_definition = store.get_definition(old_id)
    if old_definition is None:
        raise RuntimeError("merged source Definition disappeared")
    old_source_id = f"pair-0-{definition_ids.index(old_id)}"
    merged_source_id = f"{old_source_id}-revision-2"
    old_record = legacy.get_record(old_source_id)
    if old_record is None:
        raise RuntimeError("merged source record disappeared")
    # A genuinely new source arrives after its previous source Definition was
    # merged.  Its source id and legacy dedup domain differ; canonical routing
    # must come from production identity/lifecycle resolution, not a test-made
    # source link.
    new_record = SharedMemoryRecord(
        memory_id=merged_source_id, body=old_record.body,
        kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
        injection_policy="always", priority=10, agent_instance_id=agent_id,
        created_at=_now_iso(), updated_at=_now_iso(),
    )
    legacy.append_record(
        new_record,
        assignments=[{"target_type": "agent", "target_id": agent_id}],
        dedup_domain="new-source-canonical-revision-2",
    )
    receipt = _append_receipt(
        legacy,
        memory_id=merged_source_id,
        group_id=group_id,
        agent_id=agent_id,
        receipt_id="new-source-revision-receipt",
        session_id="new-source-revision-session",
        project_ref="new-source-project",
    )
    sync = service.sync_rule(
        legacy, group_id, new_record,
        assignments=legacy.list_rule_assignments(new_record.memory_id),
        receipts=[receipt], created_by="outbox",
    )
    link = store.get_source_link(group_id, new_record.memory_id)
    target = store.get_definition(canonical_id)
    source_contributions = store.list_binding_contributions(
        source_memory_id=new_record.memory_id, active=True,
    )
    active_target_bindings = store.list_bindings(
        definition_id=canonical_id, share_group_id=group_id, status="active",
    )
    active_target_evidence = store.list_evidence(definition_id=canonical_id)
    new_source_evidence = [
        item for item in active_target_evidence
        if item.source_rule_id == new_record.memory_id
        and item.receipt_id == receipt.receipt_id
    ]
    inactive_target_bindings = store.list_bindings(
        definition_id=old_id, share_group_id=group_id, status="active",
    )
    inactive_target_evidence = store.list_evidence(definition_id=old_id)
    source_binding_ids = {
        str(item.get("binding_id", "")) for item in source_contributions
    }
    active_target_binding_ids = {
        item.binding_id for item in active_target_bindings
    }
    return int(
        sync.get("definition_id") != canonical_id
        or link is None
        or link.get("canonical_definition_id") != canonical_id
        or link.get("status") != "active"
        or store.get_source_link(group_id, old_source_id) is None
        or old_record.memory_id == new_record.memory_id
        or legacy.get_record(new_record.memory_id) is None
        or target is None
        or target.status != "active"
        or sync.get("bindings", 0) < 1
        or sync.get("evidence", 0) < 1
        or len(source_contributions) < 1
        or any(item.get("definition_id") != canonical_id for item in source_contributions)
        or not source_binding_ids.issubset(active_target_binding_ids)
        or len(new_source_evidence) != 1
        or store.get_definition(old_id) is None
        or store.get_definition(old_id).status == "active"
        or bool(inactive_target_bindings)
        or bool(inactive_target_evidence)
    )


def _inactive_binding_target() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id = "inactive-binding-group", "inactive-binding-agent"
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    legacy = SharedMemoryStore(workspace, group_id)
    for index in range(2):
        legacy.append_record(
            SharedMemoryRecord(
                memory_id=f"source-{index}", body="run tests before commit",
                kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
                injection_policy="always", priority=10,
                agent_instance_id=agent_id, created_at=_aged(30),
                updated_at=_aged(30),
            ),
            assignments=[{"target_type": "agent", "target_id": agent_id}],
            # Keep identical source bodies/audiences distinct in legacy.  This
            # changes only source deduplication, not the Definition identity.
            dedup_domain=f"inactive-binding-probe-source-{index}",
        )
    _append_receipt(
        legacy,
        memory_id="source-1",
        group_id=group_id,
        agent_id=agent_id,
        receipt_id="receipt-source-1",
        session_id="session-source-1",
        project_ref="project-inactive-binding",
    )
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store)
    service.backfill_group(legacy, group_id)
    legacy.delete("source-0", actor=agent_id, manual_override=False)
    service.consume_outbox(workspace, only_group=group_id)
    retained = store.list_binding_contributions(
        source_memory_id="source-1", active=True,
    )
    active_bindings = store.list_bindings(share_group_id=group_id, status="active")
    removed = store.list_binding_contributions(
        source_memory_id="source-0", active=True,
    )
    evidence = store.list_evidence()
    retained_evidence = [
        item for item in evidence if item.source_rule_id == "source-1"
    ]
    removed_evidence = [
        item for item in evidence if item.source_rule_id == "source-0"
    ]
    source_1_link = store.get_source_link(group_id, "source-1")
    canonical_id = ""
    if source_1_link is not None:
        canonical_id = str(source_1_link.get("canonical_definition_id", ""))
    canonical = store.get_definition(canonical_id) if canonical_id else None
    canonical_active = 0
    canonical_inactive = 0
    if canonical is not None:
        canonical_active = int(canonical.status == "active")
        canonical_inactive = int(canonical.status != "active")
    source_1_link_inactive = 0
    if source_1_link is not None:
        source_1_link_inactive = int(
            str(source_1_link.get("status", "")) != "active"
        )

    failures = {
        "source_count": int(len(legacy.list_records()) != 2),
        "source_1_link_missing": int(source_1_link is None),
        "source_1_link_inactive": source_1_link_inactive,
        "canonical_missing": int(canonical is None),
        "canonical_inactive": canonical_inactive,
        "source_1_binding_count": int(len(retained) != 1),
        "source_1_evidence_count": int(len(retained_evidence) != 1),
        "source_1_binding_target": sum(
            int(item.get("definition_id", "") != canonical_id)
            for item in retained
        ),
        "source_1_evidence_target": sum(
            int(item.definition_id != canonical_id)
            for item in retained_evidence
        ),
        "source_1_active_binding_count": int(len(active_bindings) != 1),
        "active_binding_target": sum(
            int(item.definition_id != canonical_id)
            for item in active_bindings
        ),
        "source_0_binding_not_removed": int(len(removed) != 0),
        "source_0_evidence_not_removed": int(len(removed_evidence) != 0),
    }
    return sum(failures.values())


def _strength_evolution_contribution_diff() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "strength-group", "strength-agent", "strength-rule"
    legacy, store, service, _project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
        body="suggestion run tests before commit",
    )
    old = next(
        item for item in store.list_definitions()
        if item.canonical_text == normalize_rule_text("suggestion run tests before commit")
    )
    before = {
        (item.get("source_memory_id"), item.get("legacy_assignment_hash"), item.get("binding_id"))
        for item in store.list_binding_contributions(active=True)
        if item.get("definition_id") == old.definition_id
    }
    evolved = service.evolve_strength(
        old.definition_id, "must", reason="acceptance strength evolution", actor="admin",
    )
    new_id = evolved["new_definition_id"]
    after_bindings = {
        (item.binding_id, item.definition_id)
        for item in store.list_bindings(status="active")
    }
    after_contributions = {
        (str(item.get("binding_id", "")), str(item.get("definition_id", "")))
        for item in store.list_binding_contributions(active=True)
    }
    source_after = {
        (item.get("source_memory_id"), item.get("legacy_assignment_hash"), item.get("binding_id"))
        for item in store.list_binding_contributions(active=True)
        if item.get("source_memory_id") == memory_id
    }
    expected_source = {
        (source, assignment_hash)
        for source, assignment_hash, _binding_id in before
    }
    observed_source = {
        (source, assignment_hash)
        for source, assignment_hash, _binding_id in source_after
    }
    diff = int(
        not before
        or not source_after
        or any(definition_id != new_id for _, definition_id in after_bindings)
        or after_bindings != after_contributions
        or observed_source != expected_source
    )
    return diff


def _public_state_digest(store: RuleMergeStore) -> str:
    payload = {
        "definitions": [item.to_dict() for item in store.list_definitions()],
        "bindings": [item.to_dict() for item in store.list_bindings(status=None)],
        "contributions": store.list_binding_contributions(active=None),
        "versions": store.list_definition_versions(),
    }
    return stable_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))


def _strength_evolution_rollback() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "strength-rollback-group", "strength-rollback-agent", "strength-rollback-rule"
    _legacy, store, service, _project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
        body="suggestion run tests before commit",
    )
    old = next(
        item for item in store.list_definitions()
        if item.canonical_text == normalize_rule_text("suggestion run tests before commit")
    )
    evolved = service.evolve_strength(old.definition_id, "must", actor="admin")
    before = _public_state_digest(store)
    rejected = False
    try:
        service.evolve_strength(old.definition_id, "observation", actor="admin")
    except (RuntimeError, ValueError):
        rejected = True
    after = _public_state_digest(store)
    return int(not rejected or before != after or store.get_definition(evolved["new_definition_id"]) is None)


def _public_runner_up(outcome: str) -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = f"runner-{outcome}-group", f"runner-{outcome}-agent", f"runner-{outcome}-rule"
    legacy, store, _service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
    )
    receipt_ids = (f"{outcome}-winner", f"{outcome}-runner")
    shared_context_hash = f"{outcome}-runner-up-context"
    for receipt_id in receipt_ids:
        _append_receipt(
            legacy, memory_id=memory_id, group_id=group_id, agent_id=agent_id,
            receipt_id=receipt_id, session_id=f"{outcome}-same-session",
            project_ref=project_ref, context_hash=shared_context_hash,
        )
        _mcp_feedback(
            workspace, group_id, agent_id, receipt_id, f"{outcome}-same-session",
            project_ref, outcome,
            evidence="scope mismatch" if outcome == "not_applicable" else "",
            context_hash=shared_context_hash,
        )
    definition_id = store.get_source_link(group_id, memory_id)["canonical_definition_id"]
    rows = (
        store.list_negative_evidence(definition_id)
        if outcome == "not_applicable" else store.list_evidence(definition_id)
    )
    if len(rows) != 1 or rows[0].receipt_id not in receipt_ids:
        return 1
    winner = rows[0].receipt_id
    runner = receipt_ids[1] if winner == receipt_ids[0] else receipt_ids[0]
    store.deactivate_evidence_contributions_for_receipt(winner)
    restored = (
        store.list_negative_evidence(definition_id)
        if outcome == "not_applicable" else store.list_evidence(definition_id)
    )
    return int(len(restored) != 1 or restored[0].receipt_id != runner)


def _all_evidence_writes_contributions() -> int:
    workspace = Path(tempfile.mkdtemp())
    store = RuleMergeStore(workspace)
    definition = build_definition("must run tests before commit")
    store.upsert_definition(definition)
    positive = build_evidence(
        definition_id=definition.definition_id, source_rule_id="direct-positive",
        agent_instance_id="direct-agent", project_ref="direct-project",
        session_id="direct-session-positive", receipt_id="direct-receipt-positive",
        feedback_id="direct-feedback-positive", content=definition.canonical_text,
        session_trusted=True,
    )
    negative = build_negative_evidence(
        definition_id=definition.definition_id, source_rule_id="direct-negative",
        agent_instance_id="direct-agent", project_ref="direct-project",
        session_id="direct-session-negative", receipt_id="direct-receipt-negative",
        feedback_id="direct-feedback-negative", content=definition.canonical_text,
        session_trusted=True,
    )
    store.upsert_evidence(positive)
    store.upsert_negative_evidence(negative)
    positive_rows = store.list_evidence(definition.definition_id)
    negative_rows = store.list_negative_evidence(definition.definition_id)
    if (
        {item.evidence_id for item in positive_rows} != {positive.evidence_id}
        or {item.evidence_id for item in negative_rows} != {negative.evidence_id}
    ):
        return 1
    # The public receipt deactivation path can only remove a row if its write
    # was materialized as a contribution.  Keep the opposite polarity live to
    # prove the operation is scoped to one evidence contribution.
    store.deactivate_evidence_contributions_for_receipt(positive.receipt_id)
    if store.list_evidence(definition.definition_id) or not store.list_negative_evidence(
        definition.definition_id
    ):
        return 1
    store.deactivate_evidence_contributions_for_receipt(negative.receipt_id)
    return int(bool(store.list_negative_evidence(definition.definition_id)))


def _duplicate_receipt_independence() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "duplicate-receipt-group", "duplicate-receipt-agent", "duplicate-receipt-rule"
    legacy, store, _service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
    )
    session_id = "duplicate-session"
    shared_context_hash = "duplicate-source-context"
    for receipt_id in ("duplicate-a", "duplicate-b"):
        _append_receipt(
            legacy, memory_id=memory_id, group_id=group_id, agent_id=agent_id,
            receipt_id=receipt_id, session_id=session_id, project_ref=project_ref,
            context_hash=shared_context_hash,
        )
        _mcp_feedback(
            workspace, group_id, agent_id, receipt_id, session_id,
            project_ref, "followed", context_hash=shared_context_hash,
        )
    definition_id = store.get_source_link(group_id, memory_id)["canonical_definition_id"]
    evidence = store.list_evidence(definition_id)
    return int(
        len(evidence) != 1
        or store.metrics().get("evidence_independence_violation", 0) != 0
        or evidence[0].receipt_id not in {"duplicate-a", "duplicate-b"}
    )


def _distinct_session_independence() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "distinct-session-group", "distinct-session-agent", "distinct-session-rule"
    legacy, store, _service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
    )
    for index, session_id in enumerate(("distinct-session-a", "distinct-session-b")):
        receipt_id = f"distinct-receipt-{index}"
        _append_receipt(
            legacy, memory_id=memory_id, group_id=group_id, agent_id=agent_id,
            receipt_id=receipt_id, session_id=session_id, project_ref=project_ref,
        )
        _mcp_feedback(
            workspace, group_id, agent_id, receipt_id, session_id,
            project_ref, "followed",
        )
    definition_id = store.get_source_link(group_id, memory_id)["canonical_definition_id"]
    evidence = store.list_evidence(definition_id)
    sessions = {item.session_id for item in evidence}
    return int(
        len(evidence) != 2
        or len(sessions) != 2
        or {
            item.receipt_id for item in evidence
        } != {"distinct-receipt-0", "distinct-receipt-1"}
    )


def _exact_wide_shadow_diff() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "wide-shadow-group", "wide-shadow-agent", "wide-shadow-rule"
    legacy, store, service, _project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
        assignment={"target_type": "group", "target_id": group_id},
    )
    _append_receipt(
        legacy, memory_id=memory_id, group_id=group_id, agent_id=agent_id,
        receipt_id="wide-shadow-receipt", session_id="wide-shadow-session",
        project_ref=str(workspace / "project"),
    )
    service.backfill_group(legacy, group_id)
    context = EffectiveAgentContext(
        agent_instance_id=agent_id, share_group_id=group_id,
        project_ref=str(workspace / "project"), provider="codex",
        runtime_role="worker",
    )
    read = RuleReadPath(workspace, group_id)
    shadow = read.shadow_compare(legacy, context) or {}
    return int(
        bool(shadow.get("missing"))
        or bool(shadow.get("extra"))
        or int(shadow.get("permission_diff", 0) or 0) != 0
    )


def _exact_system_migration_audience_diff() -> tuple[int, dict[str, int]]:
    """Compare real legacy system audiences before/after production backfill."""
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = (
        "system-migration-group", "system-migration-agent", "system-migration-rule",
    )
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)
    legacy = SharedMemoryStore(workspace, group_id)
    legacy.append_record(
        SharedMemoryRecord(
            memory_id=memory_id,
            body="must preserve system migration audience",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            priority=10,
            agent_instance_id=agent_id,
            created_at=_aged(30),
            updated_at=_aged(30),
        ),
        assignments=[
            {"target_type": "system", "target_id": "", "priority_override": 7},
            {"target_type": "agent", "target_id": agent_id, "priority_override": 3},
        ],
        dedup_domain="system-migration-fixture",
    )
    before = _audience_multiset(
        list(legacy.list_rule_assignments(memory_id)), group_id,
    )
    store = RuleMergeStore(workspace)
    RuleMergeService(store).backfill_group(legacy, group_id)
    link = store.get_source_link(group_id, memory_id)
    if link is None:
        raise RuntimeError("system migration source link missing")
    after = _audience_multiset(
        list(store.list_bindings(
            definition_id=str(link["canonical_definition_id"]),
            share_group_id=group_id,
            status="active",
        )),
        group_id,
    )
    diff = before - after
    diff.update(after - before)
    legacy_system_count = sum(
        count for key, count in before.items() if key[1] == "system"
    )
    migrated_system_count = sum(
        count for key, count in after.items() if key[1] == "system"
    )
    return int(bool(diff)), {
        "legacy_audience_count": sum(before.values()),
        "migrated_audience_count": sum(after.values()),
        "legacy_system_audience_count": legacy_system_count,
        "migrated_system_audience_count": migrated_system_count,
        "audience_multiset_delta_count": sum(diff.values()),
    }


def _true_permission_expansion() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = "permission-group", "permission-agent", "permission-rule"
    legacy, store, service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
    )
    definition = next(iter(store.list_definitions(status="active")))
    store.upsert_binding(build_binding(
        definition.definition_id, share_group_id=group_id,
        target_type="group", target_id=group_id,
        owner_agent_id=agent_id, created_by="manual",
    ))
    context = EffectiveAgentContext(
        agent_instance_id=agent_id, share_group_id=group_id,
        project_ref=project_ref, provider="codex", runtime_role="worker",
    )
    legacy_records = [
        (record.memory_id, legacy.list_rule_assignments(record.memory_id))
        for record in legacy.list_records()
    ]
    shadow = store.shadow_verify(context, legacy_records)
    return int(int(shadow.get("permission_diff", 0) or 0) <= 0)


def _true_system_permission_expansion() -> tuple[int, dict[str, int]]:
    """Detect a canonical system audience absent from legacy's agent snapshot."""
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id, memory_id = (
        "system-expansion-group", "system-expansion-agent", "system-expansion-rule",
    )
    legacy, store, service, project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id=memory_id,
    )
    definition = next(iter(store.list_definitions(status="active")))
    store.upsert_evidence(build_evidence(
        definition_id=definition.definition_id,
        source_rule_id=memory_id,
        agent_instance_id=agent_id,
        project_ref=project_ref,
        session_id="system-expansion-session",
        session_trusted=True,
        content="must run tests before commit",
    ))
    store.upsert_binding(build_binding(
        definition.definition_id,
        share_group_id=group_id,
        target_type="system",
        target_id="",
        owner_agent_id=agent_id,
        created_by="manual",
    ))
    context = EffectiveAgentContext(
        agent_instance_id=agent_id,
        share_group_id=group_id,
        project_ref=project_ref,
        provider="codex",
        runtime_role="worker",
    )
    legacy_records = [
        (record.memory_id, legacy.list_rule_assignments(record.memory_id))
        for record in legacy.list_records()
    ]
    shadow = store.shadow_verify(context, legacy_records)
    raw_detected = int(shadow.get("permission_diff", 0) or 0)
    legacy_system_count = sum(
        len([
            item for item in assignments
            if str(getattr(item, "target_type", "") or "") == "system"
        ])
        for _memory_id, assignments in legacy_records
    )
    canonical_system_count = len([
        binding for binding in store.list_bindings(
            share_group_id=group_id, status="active",
        )
        if binding.target_type == "system"
    ])
    missed = int(not (
        raw_detected > 0
        and canonical_system_count > legacy_system_count
        and not shadow.get("missing")
    ))
    return missed, {
        "raw_detected_expansion": raw_detected,
        "canonical_system_audience_count": canonical_system_count,
        "legacy_system_audience_count": legacy_system_count,
    }


def _backfill_real_migration_loss() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_id, agent_id = "migration-loss-group", "migration-loss-agent"
    legacy, store, service, _project_ref = _feedback_case(
        workspace, group_id=group_id, agent_id=agent_id, memory_id="original-rule",
    )
    clean_before = store.metrics()["migration_loss"]
    legacy.append_record(
        SharedMemoryRecord(
            memory_id="new-unbackfilled-rule", body="must run lint before commit",
            kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
            injection_policy="always", priority=10, agent_instance_id=agent_id,
            created_at=_now_iso(), updated_at=_now_iso(),
        ),
        assignments=[{"target_type": "agent", "target_id": agent_id}],
    )
    detected = store.metrics()["migration_loss"]
    service.backfill_group(legacy, group_id)
    repaired = store.metrics()["migration_loss"]
    return int(clean_before != 0 or int(detected) < 1 or repaired != 0)


def _unrelated_group_readiness() -> int:
    workspace = Path(tempfile.mkdtemp())
    group_a, group_b = "readiness-a", "readiness-b"
    legacy_a = _seed_rule(
        workspace, group_a, "readiness-rule-a", "must run tests before commit",
        agent_id="readiness-agent-a", with_receipt=True,
    )
    legacy_b = _seed_rule(
        workspace, group_b, "readiness-rule-b", "must run tests before commit",
        agent_id="readiness-agent-b", with_receipt=True,
    )
    store = RuleMergeStore(workspace)
    service = RuleMergeService(store)
    service.backfill_group(legacy_a, group_a)
    service.backfill_group(legacy_b, group_b)
    store.set_projection_state(group_a, projection_lag=1)
    store.set_projection_state(group_b, projection_lag=0)
    context_a = EffectiveAgentContext(
        agent_instance_id="agent-readiness-a", share_group_id=group_a,
        project_ref="project-a", provider="codex", runtime_role="worker",
    )
    context_b = EffectiveAgentContext(
        agent_instance_id="agent-readiness-b", share_group_id=group_b,
        project_ref="project-a", provider="codex", runtime_role="worker",
    )
    readiness_a = RuleReadPath(workspace, group_a).canonical_readiness(
        legacy_store=legacy_a, context=context_a,
    )
    readiness_b = RuleReadPath(workspace, group_b).canonical_readiness(
        legacy_store=legacy_b, context=context_b,
    )
    return int(bool(readiness_a.get("ready")) or not bool(readiness_b.get("ready")))


def _binding_projection_probe() -> int:
    workspace = Path(tempfile.mkdtemp())
    legacy = _seed_rule(
        workspace,
        "binding-group",
        "binding-rule",
        "提交前必须运行测试",
        agent_id="agent-binding",
    )
    store = RuleMergeStore(workspace)
    RuleMergeService(store).backfill_group(legacy, "binding-group")
    return _binding_source_projection_diff(store)


def _extended_acceptance() -> tuple[
    dict[str, int], list[dict[str, str]], dict[str, int]
]:
    errors: list[dict[str, str]] = []
    observations: dict[str, int] = {}

    def run(name: str, callback: object) -> int:
        try:
            value = callback()  # type: ignore[operator]
            if isinstance(value, tuple):
                result, observed = value
                if not isinstance(observed, dict):
                    raise TypeError(f"{name} observations are not an object")
                observations.update({
                    f"{name}_{key}": int(item)
                    for key, item in observed.items()
                })
                value = result
            return int(value)
        except Exception as exc:  # acceptance must report source blockers
            errors.append({"metric": name, "error": f"{type(exc).__name__}: {exc}"})
            return 1

    metrics = {
        "trusted_session_receipt_missing": run(
            "trusted_session_receipt_missing", _trusted_session_receipt_missing,
        ),
        "binding_source_projection_diff": run(
            "binding_source_projection_diff", _binding_projection_probe,
        ),
        "evidence_fallback_loss": run(
            "evidence_fallback_loss", _evidence_fallback_loss,
        ),
        "readiness_snapshot_diff": run(
            "readiness_snapshot_diff", _readiness_snapshot_diff,
        ),
        "projection_watermark_regression": run(
            "projection_watermark_regression", _projection_watermark_regression,
        ),
        "unrelated_undo_conflict": run(
            "unrelated_undo_conflict", _unrelated_undo_conflict,
        ),
        "untrusted_merge_waiver": run(
            "untrusted_merge_waiver", _untrusted_merge_waiver,
        ),
        "shadow_permission_false_positive": run(
            "shadow_permission_false_positive", _shadow_permission_false_positive,
        ),
        "pending_unlinked_group": run(
            "pending_unlinked_group", _pending_unlinked_group,
        ),
        "unlinked_negative_feedback": run(
            "unlinked_negative_feedback", _unlinked_negative_feedback,
        ),
        "new_source_canonical_route": run(
            "new_source_canonical_route", _new_source_canonical_route,
        ),
        "inactive_binding_target": run(
            "inactive_binding_target", _inactive_binding_target,
        ),
        "strength_evolution_contribution_diff": run(
            "strength_evolution_contribution_diff",
            _strength_evolution_contribution_diff,
        ),
        "strength_evolution_rollback": run(
            "strength_evolution_rollback", _strength_evolution_rollback,
        ),
        "public_positive_runner_up": run(
            "public_positive_runner_up", lambda: _public_runner_up("followed"),
        ),
        "public_negative_runner_up": run(
            "public_negative_runner_up",
            lambda: _public_runner_up("not_applicable"),
        ),
        "all_evidence_writes_contributions": run(
            "all_evidence_writes_contributions",
            _all_evidence_writes_contributions,
        ),
        "duplicate_receipt_independence": run(
            "duplicate_receipt_independence", _duplicate_receipt_independence,
        ),
        "distinct_session_independence": run(
            "distinct_session_independence", _distinct_session_independence,
        ),
        "exact_wide_shadow_diff": run(
            "exact_wide_shadow_diff", _exact_wide_shadow_diff,
        ),
        "true_permission_expansion": run(
            "true_permission_expansion", _true_permission_expansion,
        ),
        "exact_system_migration_audience_diff": run(
            "exact_system_migration_audience_diff",
            _exact_system_migration_audience_diff,
        ),
        "true_system_expansion_missed": run(
            "true_system_expansion_missed",
            _true_system_permission_expansion,
        ),
        "backfill_real_migration_loss": run(
            "backfill_real_migration_loss", _backfill_real_migration_loss,
        ),
        "unrelated_group_readiness": run(
            "unrelated_group_readiness", _unrelated_group_readiness,
        ),
    }
    try:
        metrics["v1_collision_binding_leak"], metrics["v1_collision_runtime_leak"] = (
            _v1_collision_leaks()
        )
    except Exception as exc:
        errors.append({
            "metric": "v1_collision_binding_leak",
            "error": f"{type(exc).__name__}: {exc}",
        })
        errors.append({
            "metric": "v1_collision_runtime_leak",
            "error": f"{type(exc).__name__}: {exc}",
        })
        metrics["v1_collision_binding_leak"] = 1
        metrics["v1_collision_runtime_leak"] = 1
    return metrics, errors, observations


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
    # team-a's definitions carry their real legacy memory ids so the canonical
    # read-path check can resolve them (a canonical read that silently falls
    # back is a failure, not a pass).
    intel = RuleMergeStore(workspace)
    for definition in intel.list_definitions():
        source_rule_id = ""
        for i, body in enumerate(_TEAM_A_BODIES):
            if normalize_rule_text(body) == definition.canonical_text:
                source_rule_id = f"team-a-{i}"
                break
        _seed_evidence(
            intel, definition.definition_id, definition.canonical_text,
            source_rule_id=source_rule_id,
        )
    _seed_reputations(intel)
    # Auto-maturity is fail-closed on runtime provenance as well as positive
    # evidence. Seed trusted, rule-specific executions so this scenario tests
    # the intended cooldown/first-merge gate instead of the earlier runtime
    # gate.
    for definition in intel.list_definitions():
        for i in range(20):
            intel.upsert_runtime_feedback(
                feedback_id=f"runtime-{definition.definition_id}-{i}",
                definition_id=definition.definition_id,
                receipt_id=f"receipt-{definition.definition_id}-{i}",
                outcome="followed",
                agent_instance_id=f"agent-{i % 3}",
                project_ref=f"project-{i % 3}",
                session_id=f"runtime-session-{definition.definition_id}-{i}",
                source="user", authority=4, session_trusted=1,
                created_at=_aged(45),
            )
        intel.recompute_runtime_stats(definition.definition_id)

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
            context = _acceptance_context()
            acknowledge_capability = intel.issue_merge_capability(
                proposal["proposal_id"], context,
            )
            intel.acknowledge_first_merge(
                proposal["proposal_id"], actor=context.principal,
                capability_token=acknowledge_capability,
                access_context=context,
            )
            cooldown_capability = intel.issue_merge_capability(
                proposal["proposal_id"], context,
            )
            intel.clear_proposal_cooldown(
                proposal["proposal_id"],
                capability_token=cooldown_capability,
                access_context=context,
            )
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

    # Phase5 read-path: with an intelligence layer present, the *explicitly
    # requested* canonical read must actually engage.  A silent fallback to
    # legacy when intelligence exists is a failure, not a pass (PR7).
    from memoryguard.context_bootstrap import build_context_packet
    from memoryguard.schema_v3 import EffectiveAgentContext
    from memoryguard.shared_memory_store import SharedMemoryStore

    read_path_mode = "legacy"
    read_path_dedup = 0
    try:
        # Req8: group-level canonical activation + readiness gate the canonical
        # read.  Establish team-a's canonical state via the real saga so the
        # explicitly requested canonical read engages (PR7), then resolve.
        _reconcile_group(workspace, "team-a")
        legacy = SharedMemoryStore(workspace, "team-a")
        packet = build_context_packet(
            legacy,
            task="运行测试",
            effective_context=EffectiveAgentContext("agent-team-a", "team-a"),
            read_path="rule-intelligence",
        )
        read_path_mode = packet.get("read_path", {}).get("mode", "legacy")
        read_path_dedup = int(packet.get("read_path", {}).get("deduplicated", 0))
    except Exception:
        read_path_mode = "legacy"

    # PR7: real machine-acceptance family computed from persisted state.
    acceptance = intel.governance_acceptance()
    migration_loss_real = intel.metrics()["migration_loss"]
    extended_metrics, extended_metric_errors, extended_observations = (
        _extended_acceptance()
    )

    report = {
        "auto_merge_precision": auto_merge_precision,
        "auto_merge_precision_status": acceptance["auto_merge_precision_status"],
        "merge_decision_count": acceptance["merge_decision_count"],
        "strength_conflict_merge": metrics["strength_conflict_merge"],
        "negative_evidence_leak": metrics["negative_evidence_leak"],
        "first_merge_human_approval": metrics["first_merge_human_approval"],
        "single_agent_dominance": metrics["single_agent_dominance"],
        "binding_expansion": binding_expansion,
        "system_auto_binding": metrics["system_auto_binding"],
        "auto_broad_binding": metrics["auto_broad_binding"],
        "merge_rollback_success": 1 if undo_ok else 0,
        "migration_loss": migration_loss_real,
        "judge_audited": judge_audited,
        "read_path_mode": read_path_mode,
        "read_path_dedup": read_path_dedup,
        "candidate_count": len(candidates),
        "conflicted_count": len(conflicted),
        "strength_conflict_found": bool(strength_conflict_found),
        "negative_blocked_candidate": bool(suggestion_never_candidate),
        "auto_blocked_on_first_attempt": bool(auto_blocked_first),
        "merged_count": int(merge_ok),
        "definitions_before": canonical_before,
        "binding_count": metrics["binding_count"],
        "definition_strength_identity_collision": acceptance["definition_strength_identity_collision"],
        "canonical_read_context_diff": acceptance["canonical_read_context_diff"],
        "backfill_resurrection_count": acceptance["backfill_resurrection_count"],
        "proposal_duplicate_count": acceptance["proposal_duplicate_count"],
        "human_hard_gate_bypass_count": acceptance["human_hard_gate_bypass_count"],
        "evidence_independence_violation": acceptance["evidence_independence_violation"],
        "migration_binding_multiset_diff": acceptance["migration_binding_multiset_diff"],
        "undo_state_digest_diff": acceptance["undo_state_digest_diff"],
        "rule_intelligence_event_lag": acceptance["rule_intelligence_event_lag"],
        "acceptance_passed": bool(acceptance["passed"]),
        **extended_metrics,
        **extended_observations,
        "extended_metric_errors": extended_metric_errors,
        "passed": bool(
            auto_merge_precision >= 0.995
            and acceptance["auto_merge_precision_status"] == "observed"
            and acceptance["merge_decision_count"] > 0
            and metrics["strength_conflict_merge"] == 0
            and metrics["negative_evidence_leak"] == 0
            and metrics["first_merge_human_approval"] == 0
            and metrics["single_agent_dominance"] == 0
            and binding_expansion == 0
            and metrics["system_auto_binding"] == 0
            and metrics["auto_broad_binding"] == 0
            and undo_ok
            and migration_loss_real == 0
            and judge_audited
            and read_path_mode == "rule-intelligence"
            and acceptance["passed"]
            and strength_conflict_found
            and suggestion_never_candidate
            and auto_blocked_first
            and merge_ok
            and all(value == 0 for value in extended_metrics.values())
            and extended_observations.get(
                "true_system_expansion_missed_raw_detected_expansion", 0,
            ) > 0
            and not extended_metric_errors
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
