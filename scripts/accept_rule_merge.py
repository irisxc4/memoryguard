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

Exits non-zero when any gate fails, mirroring ``accept_rule_lifecycle.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
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


def _extended_acceptance() -> tuple[dict[str, int], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []

    def run(name: str, callback: object) -> int:
        try:
            value = callback()  # type: ignore[operator]
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
    return metrics, errors


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
    extended_metrics, extended_metric_errors = _extended_acceptance()

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
        "extended_metric_errors": extended_metric_errors,
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
            and migration_loss_real == 0
            and judge_audited
            and read_path_mode == "rule-intelligence"
            and acceptance["passed"]
            and strength_conflict_found
            and suggestion_never_candidate
            and auto_blocked_first
            and merge_ok
            and all(value == 0 for value in extended_metrics.values())
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
