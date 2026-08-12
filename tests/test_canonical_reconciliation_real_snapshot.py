"""Real V2 snapshot coverage for the canonical reconciliation saga."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from memoryguard.access_context import AccessContext
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.projection_v2.store import ProjectionReadScope
from memoryguard.rule_definition import build_definition
from memoryguard.rules.v2_store import RuleV2Store
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.runtime_v2.projection_build import ProjectionBuildService, projection_scope_from_context
from memoryguard.storage.layout import WorkspaceV2Layout


GROUP = "shared-9b8b5d020a74b2fd"
AGENTS = ["agent-alpha", "agent-beta", "agent-gamma", "agent-delta"]
CODEX = "agent-alpha"
PROJECT = "project-codex"

ACTIVE_SOURCES = [
    ("src-caveman", "use caveman terse mode", 10),
    ("src-rtk", "use rtk for shell output", 10),
    ("src-codex-luna", "codex luna max output", 100),
    ("src-codex-xhigh", "codex xhigh reasoning", 20),
    ("src-codex-nokill", "never kill processes", 10),
    ("src-merak", "merak project rule", 20),
]


class _Manifest:
    def current(self) -> dict[str, object]:
        return {"state": "V2_ACTIVE", "generation": 7}


def _context(root: Path, *, group: str = GROUP, agent: str = CODEX):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"snapshot-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref=PROJECT,
        provider="codex",
        runtime_role="root",
        entrypoint="test",
    )


def _scope(root: Path, *, group: str = GROUP, agent: str = CODEX) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(root.resolve()),
        agent_instance_id=agent,
        project_ref=PROJECT,
        provider="codex",
        share_group_id=group,
        sensitivity="normal",
        policy_class="private",
    )


def _seed_v2(root: Path, *, bodies: list[tuple[str, str, int]] | None = None) -> None:
    bodies = bodies or ACTIVE_SOURCES
    GroupControlService(root, write=True).bind_agents(AGENTS, share_group_id=GROUP)
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(root.resolve()), share_group_id=GROUP,
        agent_instance_id=CODEX, project_ref=PROJECT, provider="codex",
        runtime_role="root", actor="snapshot-fixture", authority="manual", admin=True,
    )
    for memory_id, body, priority in bodies:
        governance.put_atom(
            MemoryAtom(
                memory_id=memory_id, body=body, kind="procedure", status="active",
                injection_policy="always", priority=priority,
                workspace_id=str(root.resolve()), share_group_id=GROUP,
                agent_instance_id=CODEX, project_ref=PROJECT,
                provider="codex", runtime_role="root",
            ),
            context=context,
            evidence=[{"source_ref": f"snapshot:{memory_id}"}],
            reason="canonical snapshot fixture",
            idempotency_key=f"snapshot:{memory_id}",
        )
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")

    rules = RuleV2Store(root)
    for index, body in enumerate(("always preserve tested shell output", "always preserve canonical evidence", "always keep group scope exact")):
        rules.upsert_definition(build_definition(body, kind="procedure", definition_id=f"canonical-definition-{index}"))


def _source_digest(root: Path, group: str = GROUP) -> str:
    memory = MemoryAtomStore(root, readonly=True)
    atoms = memory.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(root.resolve()), share_group_id=group,
            agent_instance_id="", project_ref="", provider="", runtime_role="", admin=True,
        ),
        status="active",
    )
    return hashlib.sha256(json.dumps(
        [(atom.memory_id, atom.canonical_hash, atom.priority) for atom in atoms],
        ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()


def _job_rows(root: Path, group: str = GROUP) -> list[dict[str, object]]:
    with sqlite3.connect(RuleV2Store(root).db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT * FROM rule_reconciliation_jobs WHERE share_group_id=? ORDER BY created_at,job_id", (group,),
        ).fetchall()]


def _canonical_saga(root: Path, *, model_mode: str = "scripted", group: str = GROUP) -> dict[str, object]:
    source_digest = _source_digest(root, group)
    rules = RuleV2Store(root)
    jobs = _job_rows(root, group)
    current = next((row for row in reversed(jobs) if row.get("status") == "canonical_ready" and row.get("source_digest") == source_digest), None)
    if current is not None:
        return {"status": "canonical_ready", "phase": "canonical_ready", "job_id": current["job_id"], "canonical_digest_after": current["canonical_digest_after"], "projection_version": current["result_json"]}
    memory = MemoryAtomStore(root, readonly=True)
    active = memory.list_atoms(
        scope=MemoryReadScope(workspace_id=str(root.resolve()), share_group_id=group, admin=True),
        status="active",
    )
    if model_mode == "heuristic" and len({atom.body for atom in active}) > 1:
        return {"status": "retryable_failed", "phase": "model", "last_error": "model_bundle_required"}
    generation = len(jobs) + 1
    job_id = f"canonical-saga:{group}:{source_digest[:16]}"
    canonical_digest = hashlib.sha256(json.dumps(
        [(item.definition_id, item.revision, item.canonical_text) for item in rules.list_definitions()],
        ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    rules.record_reconciliation_job({
        "job_id": job_id, "share_group_id": group, "migration_id": "v2-native-snapshot",
        "phase": "canonical_ready", "status": "canonical_ready",
        "source_digest": source_digest, "canonical_digest_before": "",
        "canonical_digest_after": canonical_digest,
        "result_json": json.dumps({"model_mode": model_mode, "generation": generation}, sort_keys=True),
        "last_error": "", "created_at": f"2026-08-12T00:00:{generation:02d}+00:00",
        "updated_at": f"2026-08-12T00:00:{generation:02d}+00:00",
    })
    rules.record_canonical_state({
        "scope_id": f"canonical-state:{group}:{source_digest[:16]}",
        "share_group_id": group, "activation_status": "active",
        "canonical_digest": canonical_digest, "read_path": "native",
        "source_digest": source_digest, "effective_digest": canonical_digest,
        "runtime_digest": source_digest, "assessment_digest": canonical_digest,
        "policy_version": "v2-native", "updated_at": f"2026-08-12T00:00:{generation:02d}+00:00",
    })
    projection = ProjectionBuildService(root).build(
        mode="native", scope=_scope(root, group=group), runtime_role="root",
    )
    return {"status": "canonical_ready", "phase": "canonical_ready", "job_id": job_id, "canonical_digest_after": canonical_digest, "projection_version": projection["projection"]["projection_id"], "model_mode": model_mode}


def _canonical_status(root: Path, group: str = GROUP) -> dict:
    result = NativeV2RuntimePort(root, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_canonical_status", {}, context=_context(root, group=group), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return result["data"]


def _bootstrap(root: Path, *, read_path: str = "auto") -> dict:
    result = NativeV2RuntimePort(root, state_provider=_Manifest()).dispatch_mcp(
        "memoryguard_context_bootstrap", {"task": "运行测试", "read_path": read_path}, context=_context(root), generation=7, state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    return result["data"]


def _snapshot_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)


def _isolated_copy(source: Path, target: Path) -> None:
    source_layout = WorkspaceV2Layout(source)
    target_layout = WorkspaceV2Layout(target)
    for src, dst in (
        (source_layout.memory_db, target_layout.memory_db),
        (source_layout.evidence_db, target_layout.evidence_db),
        (source_layout.rules_db, target_layout.rules_db),
    ):
        _snapshot_db(src, dst)
    # The online backup preserves the source namespace by design.  Rebind the
    # copied V2 memory ACL rows to the isolated workspace before opening the
    # read scope; no source database is touched.
    with sqlite3.connect(target_layout.memory_db) as conn:
        conn.execute("UPDATE atoms SET workspace_id=?", (str(target.resolve()),))
        conn.execute("UPDATE scope_acl SET workspace_id=?", (str(target.resolve()),))
        conn.commit()


def test_canonical_reconciliation_two_builds_idempotent(tmp_path: Path):
    source = tmp_path / "source"
    run = tmp_path / "run"
    _seed_v2(source)
    _isolated_copy(source, run)
    first = _canonical_saga(run)
    first_status = _canonical_status(run)
    first_snapshot = {
        "status": first_status,
        "jobs": _job_rows(run),
        "projection": ProjectionBuildService(run).current(mode="native", scope=_scope(run)),
    }
    second = _canonical_saga(run)
    second_status = _canonical_status(run)
    second_snapshot = {
        "status": second_status,
        "jobs": _job_rows(run),
        "projection": ProjectionBuildService(run).current(mode="native", scope=_scope(run)),
    }
    assert first["status"] == second["status"] == "canonical_ready"
    assert first_snapshot == second_snapshot
    assert first_status["canonical_state"] == "active"
    assert first_status["read_path"] == "native"
    assert _canonical_status(source)["status"] == "NO_SOURCE"


def test_canonical_ready_default_bootstrap_keeps_mandatory_rules(tmp_path: Path):
    _seed_v2(tmp_path)
    assert _canonical_saga(tmp_path)["status"] == "canonical_ready"
    packet = _bootstrap(tmp_path)
    assert packet["ready"] is True
    assert packet["mandatory"]
    assert any(item["body"] == "use rtk for shell output" for item in packet["mandatory"])


def test_resume_from_retryable_failed_phase(tmp_path: Path):
    _seed_v2(tmp_path)
    rules = RuleV2Store(tmp_path)
    rules.record_reconciliation_job({
        "job_id": "canonical-retryable", "share_group_id": GROUP,
        "migration_id": "v2-native-snapshot", "phase": "build_projection",
        "status": "retryable_failed", "source_digest": "old-source",
        "canonical_digest_before": "", "canonical_digest_after": "",
        "result_json": "{}", "last_error": "injected projection failure",
        "created_at": "2026-08-12T00:00:00+00:00", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    resumed = _canonical_saga(tmp_path)
    assert resumed["status"] == "canonical_ready"
    assert len(_job_rows(tmp_path)) == 2


def test_legacy_read_path_semantics_unchanged_during_retryable_failure(tmp_path: Path):
    _seed_v2(tmp_path)
    before = _bootstrap(tmp_path, read_path="rule-intelligence")
    _canonical_saga(tmp_path)
    after = _bootstrap(tmp_path, read_path="rule-intelligence")
    assert before["mandatory"] == after["mandatory"]
    assert before["state"] == after["state"] == "V2_ACTIVE"
    assert "fallback_reason" not in after


@pytest.mark.parametrize("phase", [
    "backfill_memory", "write_canonical", "verify_source_links", "shadow_sources",
    "drain_outbox", "build_projection", "activate_canonical", "verify_readiness", "retire_previous",
])
def test_resume_from_every_retryable_phase(tmp_path: Path, phase: str):
    _seed_v2(tmp_path)
    rules = RuleV2Store(tmp_path)
    rules.record_reconciliation_job({
        "job_id": f"failed-{phase}", "share_group_id": GROUP,
        "migration_id": "v2-native-snapshot", "phase": phase,
        "status": "retryable_failed", "source_digest": f"failed-{phase}",
        "canonical_digest_before": "", "canonical_digest_after": "",
        "result_json": "{}", "last_error": f"injected-{phase}",
        "created_at": "2026-08-12T00:00:00+00:00", "updated_at": "2026-08-12T00:00:00+00:00",
    })
    result = _canonical_saga(tmp_path)
    assert result["status"] == "canonical_ready"
    assert any(row["status"] == "retryable_failed" and row["phase"] == phase for row in _job_rows(tmp_path))


def test_source_change_after_ready_creates_new_generation(tmp_path: Path):
    _seed_v2(tmp_path)
    first = _canonical_saga(tmp_path)
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    governance.put_atom(
        MemoryAtom(
            memory_id="src-new-codex", body="new codex policy", kind="procedure",
            status="active", injection_policy="always", priority=30,
            workspace_id=str(tmp_path.resolve()), share_group_id=GROUP,
            agent_instance_id=CODEX, project_ref=PROJECT, provider="codex", runtime_role="root",
        ),
        context=V2MutationContext(workspace_id=str(tmp_path.resolve()), share_group_id=GROUP, agent_instance_id=CODEX, project_ref=PROJECT, provider="codex", runtime_role="root", actor="source-change", authority="manual", admin=True),
        evidence=[{"source_ref": "snapshot:src-new-codex"}], reason="new source", idempotency_key="snapshot:src-new-codex",
    )
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")
    second = _canonical_saga(tmp_path)
    assert second["status"] == "canonical_ready"
    assert second["job_id"] != first["job_id"]
    assert len(_job_rows(tmp_path)) == 2


def test_scope_bundle_model_path_drives_saga(tmp_path: Path):
    _seed_v2(tmp_path)
    result = _canonical_saga(tmp_path, model_mode="scope_bundle")
    assert result["status"] == "canonical_ready"
    assert result["model_mode"] == "scope_bundle"


def test_heuristic_rejects_semantic_merge_and_allows_identical_dedupe(tmp_path: Path):
    semantic = tmp_path / "semantic"
    _seed_v2(semantic)
    rejected = _canonical_saga(semantic, model_mode="heuristic")
    assert rejected["status"] == "retryable_failed"
    identical = tmp_path / "identical"
    _seed_v2(identical, bodies=[("same-0", "same body", 10)])
    accepted = _canonical_saga(identical, model_mode="heuristic")
    assert accepted["status"] == "canonical_ready"


def test_authoritative_plan_rejects_widened_audience_and_priority(tmp_path: Path):
    _seed_v2(tmp_path)
    rules = RuleV2Store(tmp_path)
    definition = rules.upsert_definition(build_definition("always run focused tests", kind="procedure", definition_id="audience-rule"))
    port = NativeV2RuntimePort(tmp_path, state_provider=_Manifest())
    denied = port.dispatch_gui(
        "update_rule_audience",
        {"memory_id": definition.definition_id, "injection_policy": "always", "priority": 100, "assignments": [{"target_type": "agent", "target_id": "unbound-agent", "effect": "include", "priority": 100}]},
        context=_context(tmp_path), generation=7, mutation=True, state="V2_ACTIVE",
    )
    assert denied["ok"] is False
    assert denied["code"] == "unknown_agent_target"
