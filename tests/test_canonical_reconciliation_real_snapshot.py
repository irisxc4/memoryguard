# -*- coding: utf-8 -*-
"""Real-snapshot acceptance for canonical reconciliation (two builds).

A deterministic synthetic replica of the acceptance baseline is built in a
**source** workspace, snapshotted into an isolated **run** workspace via
``sqlite3.Connection.backup()`` (the sanctioned online backup API -- never a
raw WAL file copy), and the ``RuleReconciliationService`` saga is driven twice
with the heuristic bundle plan.

Accepted baseline
-----------------
* active mandatory: 6 (always policy, active status)
* shadowed: 3
* P2 outbox pending: 15
* canonical definitions: 0
* projection graph: not built
* applied heuristic enrichment: 187

Semantics
---------
* 1 Merak project rule  -> kept separate (stays active with its own def)
* 2 four-agent shared caveman/rtk -> 1 shared baseline (priority = max(10, 10))
* 3 Codex-specific rules (100 / 20 / 10) -> 1 Codex overlay (priority = 100)

Build 1
-------
* active mandatory 6 -> 3 (2 canonical records + 1 kept-separate Merak)
* shadowed +5 (2 folded into the baseline, 3 folded into the Codex overlay)
* canonical definitions = 3
* Codex + project visible rules = 2 (shared baseline + Codex overlay; the
  Merak project rule is excluded because its project differs)
* unlinked sources = 0, outbox pending = 0, projection lag = 0,
  projection error = "", graph built, canonical_ready, read path
  rule-intelligence

Build 2
-------
Fully idempotent: the job short-circuits on ``canonical_ready`` (read-only
confirm).  Every source link, canonical digest, projection version and
canonical record timestamp is unchanged; no decision/proposal growth; no
second job row.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.host_enrichment import _pending_path
from memoryguard.rule_binding import RuleBinding
from memoryguard.rule_merge import RuleMergeService
from memoryguard.rule_merge_store import RuleMergeStore
from memoryguard.rule_reconciliation import (
    REASON_LEGACY_HEURISTIC,
    RuleReconciliationService,
    _active_mandatory,
    _plan_mandatory,
    build_bundles,
    canonical_reconciliation_status,
    ensure_reconciliation_job,
)
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore

GROUP = "shared-9b8b5d020a74b2fd"
AGENTS = ["agent-alpha", "agent-beta", "agent-gamma", "agent-delta"]
CODEX = "agent-alpha"  # mirrors the real Codex agent (6627...) in the snapshot
MERAK_PROJECT = "h:/ai/workspace/me"
CODEX_PROJECT = "h:/ai/workspace/codex-project"

# (memory_id, body, priority, assignments)
ACTIVE_SOURCES = [
    (
        "src-caveman",
        "use caveman terse mode",
        10,
        [{"target_type": "agent", "target_id": a} for a in AGENTS],
    ),
    (
        "src-rtk",
        "use rtk for shell output",
        10,
        [{"target_type": "agent", "target_id": a} for a in AGENTS],
    ),
    (
        "src-codex-luna",
        "codex luna max output",
        100,
        [{"target_type": "agent", "target_id": CODEX}],
    ),
    (
        "src-codex-xhigh",
        "codex xhigh reasoning",
        20,
        [{"target_type": "agent", "target_id": CODEX}],
    ),
    (
        "src-codex-nokill",
        "never kill processes",
        10,
        [{"target_type": "agent", "target_id": CODEX}],
    ),
    (
        "src-merak",
        "merak project rule",
        20,
        [{
            "target_type": "agent_project", "target_id": CODEX,
            "project_ref": MERAK_PROJECT,
        }],
    ),
]

SHADOWED_SOURCES = ["shadow-1", "shadow-2", "shadow-3"]

HEURISTIC_APPLIED = 187
OUTBOX_PENDING = 15


def _now() -> str:
    return _now_iso()


def _seed_record(
    store: SharedMemoryStore,
    memory_id: str,
    body: str,
    *,
    priority: int = 10,
    agent: str = "",
    status: str = SharedMemoryStatus.ACTIVE.value,
    assignments: list[dict] | None = None,
) -> None:
    store.append_record(
        SharedMemoryRecord(
            memory_id=memory_id,
            body=body,
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus(status),
            injection_policy="always",
            priority=priority,
            agent_instance_id=agent,
            created_at=_now(),
            updated_at=_now(),
        ),
        assignments=assignments or [
            {"target_type": "agent", "target_id": agent},
        ],
    )


def _seed_baseline(src_ws: Path) -> None:
    """Build the accepted snapshot baseline in the source workspace.

    The shared-memory group's creator set spans all four agents (mirroring the
    real DB where ``agent_instance_id`` covers every member), which is what
    lets ``build_bundles`` recognize the four-agent audience as a group-wide
    ``shared_baseline`` rather than an ``agent_overlay``.
    """
    legacy = SharedMemoryStore(src_ws, GROUP)
    creator_agents = {
        "src-caveman": AGENTS[0],
        "src-rtk": AGENTS[1],
        "src-codex-luna": CODEX,
        "src-codex-xhigh": CODEX,
        "src-codex-nokill": CODEX,
        "src-merak": CODEX,
    }
    for memory_id, body, priority, assignments in ACTIVE_SOURCES:
        _seed_record(
            legacy, memory_id, body, priority=priority,
            agent=creator_agents[memory_id],
            assignments=assignments,
        )
    shadow_creators = [AGENTS[2], AGENTS[3], AGENTS[2]]
    for memory_id, agent in zip(SHADOWED_SOURCES, shadow_creators):
        _seed_record(
            legacy, memory_id, f"shadowed {memory_id}",
            agent=agent, status=SharedMemoryStatus.SHADOWED.value,
            assignments=[{"target_type": "agent", "target_id": agent}],
        )
    _seed_outbox(legacy, OUTBOX_PENDING)
    AgentBindingStore(src_ws).bind_agents_to_group(
        AGENTS, GROUP, allow_empty_group_creation=True,
    )


def _seed_outbox(legacy: SharedMemoryStore, count: int) -> None:
    """Seed ``count`` pending P2 outbox events via receipt + feedback."""
    source_ids = [memory_id for memory_id, *_ in ACTIVE_SOURCES]
    for i in range(count):
        memory_id = source_ids[i % len(source_ids)]
        receipt = RuleMatchReceipt(
            receipt_id=f"snap-receipt-{i}",
            memory_id=memory_id,
            share_group_id=GROUP,
            agent_instance_id=CODEX,
            task_hash=f"snap-task-{i}",
            task="real snapshot acceptance",
            session_id=f"snap-session-{i}",
            session_trusted=True,
            session_source="host",
            project_ref="",
            provider="codex",
            runtime_role="worker",
            context_hash=f"snap-context-{i}",
            created_at=_now(),
        )
        legacy.append_rule_match_receipt(receipt)
        legacy.append_rule_match_feedback(RuleMatchFeedback(
            feedback_id=f"snap-feedback-{i}",
            receipt_id=receipt.receipt_id,
            outcome="followed",
            actor=CODEX,
            source="agent",
            authority=3,
        ))


def _seed_heuristic_enrichment(run_ws: Path) -> None:
    """Seed 187 applied legacy heuristic record-enrichment tasks."""
    lines: list[str] = []
    for i in range(HEURISTIC_APPLIED):
        lines.append(json.dumps({
            "task_id": f"legacy-heuristic-{i}",
            "task_type": "record_enrichment",
            "status": "applied",
            "scope": {"share_group_id": GROUP},
            "result": {
                "kind": "preference",
                "title": f"legacy title {i}",
                "body": f"legacy body {i}",
                "confidence": 0.9,
                "rationale": "heuristic classify/translate",
            },
        }, ensure_ascii=False))
    ppath = _pending_path(run_ws)
    ppath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _snapshot_db(src: Path, dst: Path) -> None:
    """Isolated source-DB copy via ``sqlite3.Connection.backup()``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _build_isolated_run(src_ws: Path, run_ws: Path) -> None:
    """Copy both DBs (shared-memory + rule-intelligence) into the run ws."""
    group_dir = src_ws / ".memoryguard" / "shared-memory" / GROUP
    ri_dir = src_ws / ".memoryguard" / "rule-intelligence"
    _snapshot_db(
        group_dir / "memory.db",
        run_ws / ".memoryguard" / "shared-memory" / GROUP / "memory.db",
    )
    _snapshot_db(
        ri_dir / "memory.db",
        run_ws / ".memoryguard" / "rule-intelligence" / "memory.db",
    )
    src_bindings = src_ws / ".memoryguard" / "agent-bindings"
    if src_bindings.exists():
        shutil.copytree(
            src_bindings,
            run_ws / ".memoryguard" / "agent-bindings",
            dirs_exist_ok=True,
        )


def _shadowed_ids(legacy: SharedMemoryStore) -> set[str]:
    return {
        record.memory_id for record in legacy.list_records()
        if str(getattr(record.status, "value", record.status) or "")
        == SharedMemoryStatus.SHADOWED.value
    }


def _record_matches_context(
    legacy: SharedMemoryStore, memory_id: str, agent_id: str, project_ref: str,
) -> bool:
    """Legacy-style audience match: does the rule inject into this context?"""
    target_project = canonical_project_ref(project_ref)
    for assignment in legacy.list_rule_assignments(memory_id):
        target_type = str(assignment.target_type or "")
        target_id = str(assignment.target_id or "")
        ref = canonical_project_ref(assignment.project_ref or "")
        if target_type == "agent" and target_id == agent_id and not ref:
            return True
        if target_type == "agent_project" and target_id == agent_id:
            if ref == target_project:
                return True
        if target_type == "project" and ref == target_project:
            return True
    return False


def _table_count(store: RuleMergeStore, table: str) -> int:
    with store._read_conn() as conn:
        return int(conn.execute(
            f"SELECT COUNT(*) FROM {table}",  # noqa: S608 - fixed literal
        ).fetchone()[0])


def _idempotency_snapshot(
    run_ws: Path, store: RuleMergeStore, legacy: SharedMemoryStore,
) -> dict:
    service = RuleReconciliationService(store, workspace=run_ws)
    active = _active_mandatory(legacy)
    canonical = [record for record in active if not record.agent_instance_id]
    return {
        "canonical_digest": service.canonical_digest(GROUP),
        "projection_version": service._projection_version(GROUP),
        "links": sorted(
            (link["memory_id"], link["canonical_definition_id"],
             link["source_revision"], link["status"])
            for link in store.list_source_links(share_group_id=GROUP)
        ),
        "canonical_timestamps": sorted(
            (record.memory_id, record.updated_at) for record in canonical
        ),
        "proposals": _table_count(store, "rule_merge_proposals"),
        "decisions": _table_count(store, "rule_merge_decisions"),
        "outbox_total": int(legacy.outbox_high_water()["total"]),
        "job_count": len(service.jobs.list_jobs(share_group_id=GROUP)),
        "job_row": service.jobs.latest_job(GROUP),
    }


def test_canonical_reconciliation_two_builds_idempotent(tmp_path):
    src_ws = tmp_path / "source"
    run_ws = tmp_path / "run"

    # ---- build the accepted baseline + isolate the run copy ---------------
    _seed_baseline(src_ws)
    # Bootstrap the rule-intelligence DB in the source workspace so the backup
    # below carries a complete schema (incl. rule_reconciliation_jobs).
    RuleMergeStore(src_ws)
    src_legacy = SharedMemoryStore(src_ws, GROUP)
    src_high = src_legacy.outbox_high_water()
    assert int(src_high["pending"]) == OUTBOX_PENDING
    assert len(_active_mandatory(src_legacy)) == 6
    assert len(_shadowed_ids(src_legacy)) == 3

    _build_isolated_run(src_ws, run_ws)
    _seed_heuristic_enrichment(run_ws)

    store = RuleMergeStore(run_ws)
    legacy = SharedMemoryStore(run_ws, GROUP)
    service = RuleReconciliationService(store, workspace=run_ws)
    jobs = service.jobs

    # ---- Req6 gate: legacy-heuristic history -> migration job -------------
    gate = ensure_reconciliation_job(run_ws, GROUP, store=store)
    assert gate["created"] is True, gate
    assert gate["reason"] == REASON_LEGACY_HEURISTIC
    assert jobs.latest_job(GROUP)["status"] == "pending_model"

    # ---- heuristic bundle plan matches the accepted semantics -------------
    plan = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    kinds = [bundle.bundle_kind for bundle in plan["bundles"]]
    assert kinds == ["shared_baseline", "agent_overlay"]
    baseline = plan["bundles"][0]
    overlay = plan["bundles"][1]
    assert baseline.source_memory_ids == ["src-caveman", "src-rtk"]
    assert baseline.priority == 10
    assert overlay.source_memory_ids == [
        "src-codex-luna", "src-codex-nokill", "src-codex-xhigh",
    ]
    assert overlay.priority == 100
    assert plan["kept_separate"] == ["src-merak"]

    # ---- build 1 ----------------------------------------------------------
    job1 = service.run(GROUP, bundle_plan=plan, model_mode="scripted")
    assert job1["status"] == "canonical_ready"
    assert job1["phase"] == "canonical_ready"
    assert jobs.latest_job(GROUP)["status"] == "canonical_ready"
    assert jobs.list_jobs(share_group_id=GROUP), "exactly one job expected"
    assert len(jobs.list_jobs(share_group_id=GROUP)) == 1

    # -- first-build semantics ----------------------------------------------
    active = _active_mandatory(legacy)
    assert len(active) == 3, f"active mandatory 6 -> 3, got {len(active)}"
    canonical = [record for record in active if not record.agent_instance_id]
    merak = [record for record in active if record.agent_instance_id]
    assert len(canonical) == 2
    assert len(merak) == 1 and merak[0].memory_id == "src-merak"
    assert merak[0].status.value == SharedMemoryStatus.ACTIVE.value

    shadowed = _shadowed_ids(legacy)
    assert len(shadowed) == 3 + 5, f"shadowed +5, got {len(shadowed) - 3}"
    assert {"src-caveman", "src-rtk", "src-codex-luna", "src-codex-xhigh",
            "src-codex-nokill"} <= shadowed
    assert "src-merak" not in shadowed

    status = canonical_reconciliation_status(run_ws, GROUP, store=store)
    assert status["canonical_ready"] is True, status["failures"]
    assert status["read_path"] == "rule-intelligence"
    assert status["checks"]["canonical_definitions"] == 3
    assert status["checks"]["active_mandatory"] == 3
    assert status["checks"]["unlinked_sources"] == 0
    assert status["checks"]["outbox_pending"] == 0
    assert status["checks"]["projection_lag"] == 0
    assert status["checks"]["projection_error"] == ""
    assert status["checks"]["graph_built"] is True
    assert status["checks"]["canonical_activation"] == "active"

    # Codex + project visible rules = 2 (baseline + overlay; Merak excluded).
    visible = [
        record for record in active
        if _record_matches_context(legacy, record.memory_id, CODEX, CODEX_PROJECT)
    ]
    assert len(visible) == 2, [
        record.memory_id for record in visible
    ]

    # Each active canonical record carries an active group binding (the read
    # path fail-closes otherwise).
    bound = {
        binding.definition_id for binding in store.list_bindings(
            share_group_id=GROUP, status="active",
        )
    }
    links = store.list_source_links(share_group_id=GROUP, status="active")
    for link in links:
        canonical_id = (
            link.get("canonical_definition_id")
            or link.get("original_definition_id") or ""
        )
        if link["memory_id"] in {record.memory_id for record in active}:
            assert canonical_id in bound, link["memory_id"]

    # Req6 gate now refuses re-creation (idempotent).  Either reason is valid:
    # the link-completeness gate is checked before the canonical_ready-job gate.
    gate2 = ensure_reconciliation_job(run_ws, GROUP, store=store)
    assert gate2["created"] is False
    assert gate2["reason"] in {"canonical_ready_job_exists",
                               "canonical_already_built"}

    # ---- build 2 (idempotency) --------------------------------------------
    before = _idempotency_snapshot(run_ws, store, legacy)
    job2 = service.run(GROUP, bundle_plan=plan, model_mode="scripted")
    assert job2["status"] == "canonical_ready"
    assert len(jobs.list_jobs(share_group_id=GROUP)) == 1
    after = _idempotency_snapshot(run_ws, store, legacy)

    assert before["canonical_digest"] == after["canonical_digest"]
    assert before["projection_version"] == after["projection_version"]
    assert before["links"] == after["links"]
    assert before["canonical_timestamps"] == after["canonical_timestamps"]
    assert before["proposals"] == after["proposals"]
    assert before["decisions"] == after["decisions"]
    assert before["outbox_total"] == after["outbox_total"]
    # Second run left a single job row whose every field is frozen -- the
    # read-only confirm never re-wrote it (no updated_at bump, no re-minted
    # digest).  The recorded after-digest still matches the live state.
    assert before["job_row"] == after["job_row"]
    assert job2["job_id"] == before["job_row"]["job_id"]
    assert job2["status"] == "canonical_ready"
    assert job2["canonical_digest_after"] == after["canonical_digest"]
    assert job2["canonical_digest_after"] == before["job_row"]["canonical_digest_after"]
    assert job2["projection_version"]
    assert job2["projection_version"] == before["job_row"]["projection_version"]

    # ---- source workspace untouched ---------------------------------------
    src_store = RuleMergeStore(src_ws)
    src_status = canonical_reconciliation_status(src_ws, GROUP, store=src_store)
    assert src_status["canonical_ready"] is False
    assert src_status["checks"]["canonical_definitions"] == 0
    assert len(_active_mandatory(src_legacy)) == 6
    assert not src_store.list_source_links(share_group_id=GROUP)


def test_resume_from_retryable_failed_phase(tmp_path):
    """A mid-saga failure lands the job retryable_failed at the real phase and
    ``run()`` resumes from there instead of restarting at ``model``."""
    run_ws = tmp_path / "run"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan = build_bundles(
        store, legacy, GROUP, _active_mandatory(legacy),
    )

    # Inject a failure at the write_canonical boundary.  Stored as an instance
    # attribute, so the function is called without ``self`` binding; deleting
    # the attribute afterwards restores the class's bound method.
    def boom(share_group_id, legacy, bundle_plan):
        raise RuntimeError("injected-canonical-write-failure")

    service._write_canonical = boom  # type: ignore[assignment]
    try:
        try:
            service.run(GROUP, bundle_plan=plan, model_mode="scripted")
            raise AssertionError("expected saga failure")
        except RuntimeError as exc:
            assert "injected-canonical-write-failure" in str(exc)
    finally:
        del service._write_canonical

    job = service.jobs.latest_job(GROUP)
    assert job["status"] == "retryable_failed"
    assert job["phase"] == "write_canonical"
    assert job["attempt_count"] >= 1
    assert "injected-canonical-write-failure" in job["last_error"]

    # Resume: the same job rows re-enter applying (not a fresh job), and the
    # saga completes to canonical_ready.
    job2 = service.run(GROUP, bundle_plan=plan, model_mode="scripted")
    assert job2["status"] == "canonical_ready"
    assert len(service.jobs.list_jobs(share_group_id=GROUP)) == 1
    status = canonical_reconciliation_status(run_ws, GROUP, store=store)
    assert status["canonical_ready"] is True


def test_legacy_read_path_semantics_unchanged_during_retryable_failure(tmp_path):
    from memoryguard.context_bootstrap import build_context_packet
    from memoryguard.schema_v3 import EffectiveAgentContext

    run_ws = tmp_path / "shadow-window"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan = build_bundles(
        store, legacy, GROUP, _active_mandatory(legacy),
    )
    context = EffectiveAgentContext(
        agent_instance_id=CODEX,
        share_group_id=GROUP,
        project_ref=CODEX_PROJECT,
        provider="codex",
        runtime_role="worker",
    )
    baseline = build_context_packet(
        legacy,
        task="运行测试",
        effective_context=context,
        read_path="rule-intelligence",
    )
    assert baseline["effective_read_path"] == "legacy"
    baseline_ids = set(baseline["mandatory_rule_ids"])
    baseline_bodies = [
        item["body"]
        for item in baseline["context_packet"]["mandatory_items"]
    ]

    def boom(*args, **kwargs):
        raise RuntimeError("injected-after-shadow-window-failure")

    service._retire_previous_canonical = boom  # type: ignore[assignment]
    try:
        with pytest.raises(
            RuntimeError, match="injected-after-shadow-window-failure",
        ):
            service.run(GROUP, bundle_plan=plan, model_mode="scripted")
    finally:
        del service._retire_previous_canonical

    failed = service.jobs.latest_job(GROUP)
    assert failed["status"] == "retryable_failed"
    assert failed["phase"] == "retire_previous"
    # The window is real: source rules were shadowed before the failed phase,
    # while only the canonical projection records remain active.
    all_records = list(legacy.list_records())
    folded_sources = {
        "src-caveman", "src-rtk",
        "src-codex-luna", "src-codex-xhigh", "src-codex-nokill",
    }
    assert folded_sources <= set(_shadowed_ids(legacy))
    assert all(
        not record.agent_instance_id
        and record.status == SharedMemoryStatus.ACTIVE
        for record in all_records
        if str(record.dedup_domain or "").startswith("canonical:")
    )

    packet = build_context_packet(
        legacy,
        task="运行测试",
        effective_context=context,
        read_path="rule-intelligence",
    )
    assert packet["effective_read_path"] == "legacy"
    assert set(packet["mandatory_rule_ids"]) == baseline_ids
    assert [
        item["body"]
        for item in packet["context_packet"]["mandatory_items"]
    ] == baseline_bodies
    assert not any(
        str(item.get("dedup_domain") or "").startswith("canonical:")
        for item in packet["context_packet"]["mandatory_items"]
    )


# ---------------------------------------------------------------------------
# Req5 fault-injection: every retryable phase resumes with the same job row.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase,fail_target", [
    ("backfill_p3", "backfill_p3"),
    ("write_canonical", "write_canonical"),
    ("verify_source_links", "verify_source_links"),
    ("shadow_legacy", "shadow_legacy"),
    ("drain_outbox", "drain_outbox"),
    ("build_projection", "build_projection"),
    ("activate_canonical", "activate_canonical"),
    ("verify_readiness", "verify_readiness"),
    ("retire_previous", "retire_previous"),
])
def test_resume_from_every_retryable_phase(
    tmp_path, monkeypatch, phase, fail_target,
):
    from memoryguard import rule_merge as rule_merge_module
    from memoryguard import rule_reconciliation as recon_module
    from memoryguard.rule_reconciliation import RuleReconciliationStore

    run_ws = tmp_path / phase
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))

    message = f"injected-{fail_target}-failure"

    # Fail exactly once; every later invocation delegates to the real method.
    fail_active = {"active": True}
    original = None

    def boom(*_args, **_kwargs):
        if fail_active["active"]:
            fail_active["active"] = False
            raise RuntimeError(message)
        if original is not None:
            return original(*_args, **_kwargs)
        return None

    if fail_target == "backfill_p3":
        original = rule_merge_module.RuleMergeService.backfill_group
        monkeypatch.setattr(
            rule_merge_module.RuleMergeService, "backfill_group", boom,
        )
    elif fail_target == "write_canonical":
        original = service._write_canonical
        monkeypatch.setattr(service, "_write_canonical", boom)
    elif fail_target == "verify_source_links":
        original = service._verify_source_links
        monkeypatch.setattr(service, "_verify_source_links", boom)
    elif fail_target == "shadow_legacy":
        original = service._shadow_legacy
        monkeypatch.setattr(service, "_shadow_legacy", boom)
    elif fail_target == "drain_outbox":
        original = rule_merge_module.RuleMergeService.consume_outbox
        monkeypatch.setattr(
            rule_merge_module.RuleMergeService, "consume_outbox", boom,
        )
    elif fail_target == "build_projection":
        original = service._build_projection
        monkeypatch.setattr(service, "_build_projection", boom)
    elif fail_target == "activate_canonical":
        original = RuleReconciliationStore.set_canonical_activation
        monkeypatch.setattr(
            RuleReconciliationStore, "set_canonical_activation", boom,
        )
    elif fail_target == "verify_readiness":
        original = recon_module.canonical_reconciliation_status
        monkeypatch.setattr(
            recon_module, "canonical_reconciliation_status", boom,
        )
    elif fail_target == "retire_previous":
        original = service._retire_previous_canonical
        monkeypatch.setattr(service, "_retire_previous_canonical", boom)

    with pytest.raises(RuntimeError, match=message):
        service.run(GROUP, bundle_plan=plan, model_mode="scripted")

    failed = service.jobs.latest_job(GROUP)
    assert failed["status"] == "retryable_failed"
    assert failed["phase"] == phase, (phase, failed["phase"])
    assert message in failed["last_error"]
    job_id = failed["job_id"]

    # Restoring the target lets the same generation resume in-place.  The
    # persisted plan is reused; no second job row is created.
    job2 = service.run(GROUP, bundle_plan=plan, model_mode="scripted")
    assert job2["status"] == "canonical_ready"
    assert job2["job_id"] == job_id
    assert len(service.jobs.list_jobs(share_group_id=GROUP)) == 1
    status = canonical_reconciliation_status(run_ws, GROUP, store=store)
    assert status["canonical_ready"] is True, status["failures"]


def test_source_change_after_ready_creates_new_generation(tmp_path):
    """A canonical_ready group must re-converge when a new source appears."""
    run_ws = tmp_path / "run"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan1 = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    job1 = service.run(GROUP, bundle_plan=plan1, model_mode="scripted")
    assert job1["status"] == "canonical_ready"

    _seed_record(
        legacy,
        "src-new-codex",
        "new codex policy",
        priority=30,
        agent=CODEX,
        assignments=[{"target_type": "agent", "target_id": CODEX}],
    )
    links = store.list_source_links(share_group_id=GROUP, status="active")
    plan2 = build_bundles(
        store, legacy, GROUP, _plan_mandatory(
            legacy, {link["memory_id"] for link in links},
        ),
    )
    assert any("src-new-codex" in b.source_memory_ids for b in plan2["bundles"])

    job2 = service.run(GROUP, bundle_plan=plan2, model_mode="scripted")
    assert job2["status"] == "canonical_ready"
    assert job2["job_id"] != job1["job_id"]

    jobs = service.jobs.list_jobs(share_group_id=GROUP)
    by_id = {job["job_id"]: job for job in jobs}
    assert by_id[job1["job_id"]]["status"] == "superseded"
    assert by_id[job2["job_id"]]["status"] == "canonical_ready"
    status = canonical_reconciliation_status(run_ws, GROUP, store=store)
    assert status["canonical_ready"] is True, status["failures"]
    # The old canonical record is retired, not deleted.
    shadowed = _shadowed_ids(legacy)
    assert any(
        str(record.dedup_domain or "").startswith("canonical:")
        and record.memory_id in shadowed
        for record in legacy.list_records()
    )


def test_scope_bundle_model_path_drives_saga(tmp_path, monkeypatch):
    """The production model entry is wired into ``run()``.

    A scope_bundle result returned by ``batch_bundle_via_cli`` is validated by
    the saga and may contain a semantic merged body; it must not be rejected by
    the deterministic-safe heuristic gate.
    """
    from memoryguard import host_agent_backend as hab

    run_ws = tmp_path / "run"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    calls = {"count": 0}

    def fake_batch(records, assignments_by_memory_id=None, workspace=None, **kwargs):
        calls["count"] += 1
        plan = build_bundles(
            store, legacy, GROUP, list(records), workspace=workspace,
        )
        return {
            "bundles": [bundle.to_dict() for bundle in plan["bundles"]],
            "kept_separate": plan["kept_separate"],
            "model_mode": "scope_bundle",
        }

    monkeypatch.setattr(hab, "batch_bundle_via_cli", fake_batch)
    job = service.run(GROUP, model_mode="scope_bundle")
    assert calls["count"] == 1
    assert job["status"] == "canonical_ready", job
    assert job["model_mode"] == "scope_bundle", job
    status = canonical_reconciliation_status(run_ws, GROUP, store=store)
    assert status["canonical_ready"] is True, status["failures"]


def test_heuristic_rejects_semantic_merge_and_allows_identical_dedupe(tmp_path):
    """Heuristic plans may only fold byte-identical sources."""
    run_ws = tmp_path / "run"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    # The snapshot baseline folds different bodies -> heuristic must refuse.
    rejected = service.run(
        GROUP, bundle_plan=plan, model_mode="heuristic",
    )
    assert rejected["status"] == "retryable_failed"
    assert "model_bundle_required" in rejected["last_error"]

    identical_ws = tmp_path / "identical"
    legacy2 = SharedMemoryStore(identical_ws, GROUP)
    for i, agent in enumerate(AGENTS[:2]):
        _seed_record(
            legacy2, f"same-{i}", "same body", priority=10,
            agent=agent,
            assignments=[{"target_type": "agent", "target_id": a} for a in AGENTS[:2]],
        )
    AgentBindingStore(identical_ws).bind_agents_to_group(
        AGENTS[:2], GROUP, allow_empty_group_creation=True,
    )
    store2 = RuleMergeStore(identical_ws)
    service2 = RuleReconciliationService(store2, workspace=identical_ws)
    plan2 = build_bundles(
        store2, legacy2, GROUP, _active_mandatory(legacy2),
    )
    job = service2.run(
        GROUP, bundle_plan=plan2, model_mode="heuristic",
    )
    assert job["status"] == "canonical_ready", job


def test_authoritative_plan_rejects_widened_audience_and_priority(tmp_path):
    """Saga validation refuses plans that widen or reweight persisted scope."""
    run_ws = tmp_path / "run"
    _seed_baseline(run_ws)
    store = RuleMergeStore(run_ws)
    service = RuleReconciliationService(store, workspace=run_ws)
    legacy = service._legacy(GROUP)
    plan = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    overlay = plan["bundles"][1]
    assert overlay.bundle_kind == "agent_overlay"
    # Widen the Codex overlay to another agent that has no source assignment.
    overlay.bundle_kind = "shared_baseline"
    with pytest.raises(ValueError, match="shared_baseline_member_mismatch"):
        service.run(GROUP, bundle_plan=plan, model_mode="scripted")

    plan2 = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    overlay2 = plan2["bundles"][1]
    overlay2.priority = 5  # source max is 100
    with pytest.raises(ValueError, match="bundle_priority_not_source_max"):
        service.run(GROUP, bundle_plan=plan2, model_mode="scripted")

    plan3 = build_bundles(store, legacy, GROUP, _active_mandatory(legacy))
    baseline3 = plan3["bundles"][0]
    baseline3.provider = "claude"  # no source has a provider assignment
    with pytest.raises(ValueError, match="shared_baseline_widens_audience"):
        service.run(GROUP, bundle_plan=plan3, model_mode="scripted")
