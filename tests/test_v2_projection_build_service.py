from __future__ import annotations

import time
from pathlib import Path

from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2 import ProjectionReadScope
from memoryguard.runtime_v2.projection_build import ProjectionBuildService, V2ReleaseService
from memoryguard.runtime_v2.task_coordinator import TaskCoordinator


def _context(tmp_path: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(tmp_path.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="test",
        authority="manual",
    )


def _scope(tmp_path: Path) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref=str(tmp_path.resolve()),
        provider="gui",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
    )


def _seed_atom(tmp_path: Path, memory_id: str = "m1") -> None:
    memory = MemoryAtomStore(tmp_path, readonly=False)
    governance = GovernanceV2(tmp_path, memory_store=memory)
    ctx = _context(tmp_path)
    evidence, _ = governance.put_evidence(
        context=ctx,
        reason="projection fixture evidence",
        source_ref=f"fixture:{memory_id}",
        digest=(memory_id.encode("utf-8").hex() * 64)[:64].ljust(64, "0"),
        authority="governance",
        evidence_type="reference",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=f"private body {memory_id}",
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=str(tmp_path.resolve()),
            provider="gui",
            runtime_role="gui",
            workspace_id=str(tmp_path.resolve()),
        ),
        context=ctx,
        evidence=[evidence.to_dict()],
        reason="projection fixture atom",
        idempotency_key=f"fixture:{memory_id}",
    )
    for _ in range(4):
        state = memory.project_evidence(governance.evidence)
        if int(state.get("pending", 0)) == 0:
            break
    assert memory.pending_outbox(include_failed=True) == []
    memory.set_visibility("active", atom_ids=[atom.atom_id])


def test_projection_build_reads_v2_memory_and_evidence_without_body_copy(tmp_path: Path) -> None:
    _seed_atom(tmp_path)
    service = ProjectionBuildService(tmp_path)
    result = service.build(mode="reconstructed", scope=_scope(tmp_path), runtime_role="gui")
    assert result["status"] == "succeeded"
    assert result["atom_count"] == 1
    assert result["evidence_count"] >= 1
    current = service.current(mode="reconstructed", scope=_scope(tmp_path))
    assert current["projection"] is not None

    store = service._projection(write=False)
    with store.connection("scenario") as conn:
        payloads = "\n".join(str(row[0]) for row in conn.execute("SELECT payload_json FROM scenario_projections"))
    assert "private body" not in payloads
    assert store.integrity_check("scenario") == ["ok"]
    assert store.foreign_key_check("scenario") == []


def test_projection_build_is_idempotent_for_unchanged_inputs(tmp_path: Path) -> None:
    _seed_atom(tmp_path)
    service = ProjectionBuildService(tmp_path)
    first = service.build(mode="native", scope=_scope(tmp_path), runtime_role="gui")
    second = service.build(mode="native", scope=_scope(tmp_path), runtime_role="gui")
    assert first["projection"]["projection_id"] == second["projection"]["projection_id"]
    assert service._projection(write=False).counts("profile")["projections"] == 1


def test_projection_task_status_is_durable_and_delete_tombstones_head(tmp_path: Path) -> None:
    _seed_atom(tmp_path)
    service = ProjectionBuildService(tmp_path)
    coordinator = TaskCoordinator(tmp_path)
    task_scope = coordinator.scope_from_context(
        tmp_path,
        {
            "agent_instance_id": "agent-a",
            "project_ref": str(tmp_path.resolve()),
            "share_group_id": "group-a",
            "provider": "gui",
            "runtime_role": "gui",
        },
    )
    accepted = coordinator.start(
        operation="projection_build",
        idempotency_key="build-1",
        scope=task_scope,
        worker=lambda execution: service.build(
            mode="reconstructed",
            scope=_scope(tmp_path),
            runtime_role="gui",
            execution=execution,
        ),
    )
    run_id = accepted["task"]["run_id"]
    for _ in range(100):
        status = coordinator.status(run_id, task_scope)
        if status["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    assert status["status"] == "succeeded", status
    recovered = TaskCoordinator(tmp_path).status(run_id, task_scope)
    assert recovered["status"] == "succeeded"

    deleted = service.delete(mode="reconstructed", scope=_scope(tmp_path))
    assert deleted["deleted"] is True
    assert service.current(mode="reconstructed", scope=_scope(tmp_path))["projection"] is None


def test_release_plan_apply_verify_and_rollback_are_digest_guarded(tmp_path: Path) -> None:
    _seed_atom(tmp_path)
    projection = ProjectionBuildService(tmp_path)
    projection.build(mode="reconstructed", scope=_scope(tmp_path), runtime_role="gui")
    release = V2ReleaseService(tmp_path)
    target = release.resolve_target(scope=_scope(tmp_path), target_path="published/memoryguard.json")
    plan = release.create_plan(
        str(target),
        scope=_scope(tmp_path),
        mode="reconstructed",
        runtime_role="gui",
    )
    assert plan["memory_count"] == 1
    applied = release.apply(
        plan["plan_id"],
        str(target),
        scope=_scope(tmp_path),
        confirmed=True,
        runtime_role="gui",
    )
    assert target.is_file()
    assert release.verify(applied["release_id"], str(target), scope=_scope(tmp_path))["ok"] is True
    body = target.read_text(encoding="utf-8")
    assert "private body m1" in body

    with release._store(write=False).connection("scenario") as conn:
        ledger = "\n".join(str(row[0]) for row in conn.execute("SELECT detail FROM projection_ledger"))
    assert "private body m1" not in ledger

    rolled = release.rollback(
        applied["release_id"],
        str(target),
        scope=_scope(tmp_path),
        confirmed=True,
    )
    assert rolled["release_id"] == applied["release_id"]
    assert not target.exists()


def test_release_apply_rejects_target_drift(tmp_path: Path) -> None:
    _seed_atom(tmp_path)
    ProjectionBuildService(tmp_path).build(mode="reconstructed", scope=_scope(tmp_path), runtime_role="gui")
    release = V2ReleaseService(tmp_path)
    target = release.resolve_target(scope=_scope(tmp_path), target_path="published/memoryguard.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("before", encoding="utf-8")
    plan = release.create_plan(str(target), scope=_scope(tmp_path), mode="reconstructed", runtime_role="gui")
    target.write_text("changed-after-plan", encoding="utf-8")
    import pytest
    from memoryguard.runtime_v2.projection_build import ProjectionBuildError

    with pytest.raises(ProjectionBuildError, match="release_target_drift"):
        release.apply(plan["plan_id"], str(target), scope=_scope(tmp_path), confirmed=True, runtime_role="gui")


def test_projection_source_toggle_uses_v2_content_connector(tmp_path: Path) -> None:
    from memoryguard.content import ContentStore

    content = ContentStore(tmp_path)
    content.upsert_source_connector(
        source_id="src-1",
        provider="local",
        source_type="directory",
        external_root_key="docs",
    )
    service = ProjectionBuildService(tmp_path)
    before = service.source_map(scope=_scope(tmp_path))
    assert before["entries"][0]["enabled"] is True
    changed = service.set_source_enabled("src-1", False, scope=_scope(tmp_path))
    assert changed["changed"] is True
    after = service.source_map(scope=_scope(tmp_path))
    assert after["entries"][0]["enabled"] is False
