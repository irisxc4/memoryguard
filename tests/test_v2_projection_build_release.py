from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from memoryguard.content import ContentReadScope, ContentStore
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtom, MemoryAtomStore
from memoryguard.projection_v2 import ProjectionReadScope, ProjectionStore
from memoryguard.runtime_v2.projection_build import ProjectionBuildService, V2ReleaseService
from memoryguard.runtime_v2.task_coordinator import TaskCancelled, TaskCoordinator
from memoryguard.runtime_v2.working_memory import RuntimeScope


def _scope(tmp_path: Path) -> ProjectionReadScope:
    return ProjectionReadScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        project_ref="project-a",
        provider="local",
        share_group_id="group-a",
        sensitivity="normal",
        policy_class="private",
    )


def _runtime_scope(scope: ProjectionReadScope) -> RuntimeScope:
    return RuntimeScope(
        workspace_id=scope.workspace_id,
        agent_instance_id=scope.agent_instance_id,
        project_ref=scope.project_ref,
        provider=scope.provider,
        share_group_id=scope.share_group_id,
        runtime_scope="gui",
    )


def _governed_atom(tmp_path: Path, scope: ProjectionReadScope, memory_id: str, body: str, *, source_ref: str = "manual:test", source_record_id: str = ""):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    atom = MemoryAtom(
        memory_id=memory_id,
        body=body,
        kind="fact",
        status="active",
        confidence=0.8,
        injection_policy="relevant",
        workspace_id=scope.workspace_id,
        agent_instance_id=scope.agent_instance_id,
        project_ref=scope.project_ref,
        provider=scope.provider,
        share_group_id=scope.share_group_id,
        runtime_role="gui",
        metadata={"origin": "test"},
        provenance=[{"source_ref": source_ref, "source_digest": f"digest-{memory_id}"}],
    )
    persisted, _decision = governance.put_atom(
        atom,
        context={
            "workspace_id": scope.workspace_id,
            "agent_instance_id": scope.agent_instance_id,
            "project_ref": scope.project_ref,
            "provider": scope.provider,
            "share_group_id": scope.share_group_id,
            "runtime_role": "gui",
            "actor": scope.agent_instance_id,
            "authority": "manual",
        },
        evidence=[{
            "source_ref": source_ref,
            "revision": f"rev-{memory_id}",
            "digest": f"digest-{memory_id}",
            "authority": "governance",
            "metadata": {"memory_id": memory_id},
        }],
        source_mappings=[{
            "source_domain": "content" if source_ref.startswith("content:") else "manual",
            "source_ref": source_ref,
            "source_record_id": source_record_id,
            "source_revision": f"rev-{memory_id}",
            "digest": f"digest-{memory_id}",
            "metadata": {"memory_id": memory_id},
        }],
        reason="test projection input",
        confidence=1.0,
        idempotency_key=f"test:{memory_id}",
    )
    memory.project_evidence(evidence)
    memory.set_visibility("active", atom_ids=[persisted.atom_id])
    return persisted


def _wait(coordinator: TaskCoordinator, run_id: str, scope: RuntimeScope, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = coordinator.status(run_id, scope)
        if result.get("status") in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    raise AssertionError("task did not terminate")


def test_projection_build_uses_enabled_content_sources_and_compensates_partial_failure(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    content = ContentStore(tmp_path, workspace_id=scope.workspace_id)
    content_scope = ContentReadScope(
        namespace_id="projection-content",
        workspace_id=scope.workspace_id,
        agent_instance_id=scope.agent_instance_id,
        project_ref=scope.project_ref,
        provider=scope.provider,
        share_group_id=scope.share_group_id,
        sensitivity=scope.sensitivity,
        policy_class=scope.policy_class,
    )
    ns = content.ensure_namespace(
        namespace_id=content_scope.namespace_id,
        workspace_id=scope.workspace_id,
        trust_domain="projection-test",
        sensitivity=scope.sensitivity,
    )
    content.upsert_source_connector(
        source_id="source-1", provider=scope.provider, source_type="knowledge",
        external_root_key=str(tmp_path / "source"), workspace_id=scope.workspace_id,
    )
    blob_id = content.put_blob("content-backed atom", namespace_id=ns.namespace_id)
    occurrence_id = content.upsert_occurrence(
        source_object_id="object-1", occurrence_key="0", blob_id=blob_id,
        namespace_id=ns.namespace_id, source_id="source-1", source_kind="knowledge",
        external_object_key="doc.md", object_type="document", source_revision="r1",
        content_role="knowledge", sensitivity=scope.sensitivity,
        workspace_id=scope.workspace_id, agent_instance_id=scope.agent_instance_id,
        project_ref=scope.project_ref, share_group_id=scope.share_group_id,
        provider=scope.provider, policy_class=scope.policy_class,
        access_scope={"mode": "knowledge"},
    )
    _governed_atom(tmp_path, scope, "manual-1", "manual atom")
    _governed_atom(tmp_path, scope, "content-1", "content atom", source_ref=f"content:{blob_id}", source_record_id=occurrence_id)

    service = ProjectionBuildService(tmp_path)
    first = service.build(mode="reconstructed", scope=scope, runtime_role="gui")
    assert first["atom_count"] == 2
    store = ProjectionStore(tmp_path, initialize=False)
    projection_key = first["projection"]["key"]
    scenario_first = store.get_projection("scenario", projection_key, scope=scope)
    assert scenario_first is not None

    service.set_source_enabled("source-1", False, scope=scope)
    second = service.build(mode="reconstructed", scope=scope, runtime_role="gui")
    assert second["atom_count"] == 1
    scenario_second = store.get_projection("scenario", projection_key, scope=scope)
    assert scenario_second is not None and scenario_second.projection_id != scenario_first.projection_id

    _governed_atom(tmp_path, scope, "manual-2", "another manual atom")
    before_scenario = store.get_projection("scenario", projection_key, scope=scope)

    class _CancelAfterCommit:
        stage = ""

        def progress(self, _percent, stage, **_values):
            self.stage = str(stage)

        def check_cancelled(self):
            if self.stage == "save":
                raise TaskCancelled("cancelled after immutable projection commit")

    with pytest.raises(TaskCancelled):
        service.build(
            mode="reconstructed",
            scope=scope,
            runtime_role="gui",
            execution=_CancelAfterCommit(),
        )
    after_scenario = store.get_projection("scenario", projection_key, scope=scope)
    assert after_scenario is not None and before_scenario is not None
    assert after_scenario.projection_id == before_scenario.projection_id


def test_release_plan_apply_verify_and_rollback_use_runtime_receipt_and_content_backup(tmp_path: Path) -> None:
    scope = _scope(tmp_path)
    _governed_atom(tmp_path, scope, "release-atom", "release body")
    target = tmp_path / "MEMORY.md"
    target.write_text("previous release body\n", encoding="utf-8")

    ProjectionBuildService(tmp_path).build(
        mode="reconstructed", scope=scope, runtime_role="gui"
    )
    release = V2ReleaseService(tmp_path)
    plan = release.create_plan(
        str(target), scope=scope, mode="reconstructed", runtime_role="gui"
    )
    assert plan["plan_id"].startswith("release-plan-")
    assert str(target) not in str(plan)

    coordinator = TaskCoordinator(tmp_path)
    rscope = _runtime_scope(scope)

    def worker(execution):
        return release.apply(
            plan["plan_id"], str(target), scope=scope, execution=execution,
            confirmed=True, runtime_role="gui",
        )

    accepted = coordinator.start(
        operation="release_apply", idempotency_key="release-request-1",
        scope=rscope, worker=worker,
    )
    final = _wait(coordinator, accepted["task"]["run_id"], rscope)
    assert final["status"] == "succeeded", final
    assert "release body" in target.read_text(encoding="utf-8")

    listed = release.list_releases(scope=scope)
    assert listed["total"] == 1
    receipt = listed["releases"][0]
    assert receipt["previous_blob_id"]
    assert receipt["previous_occurrence_id"]
    assert str(target) not in str(receipt)

    verified = release.verify(receipt["release_id"], str(target), scope=scope)
    assert verified["hashes_match"] is True

    rolled = release.rollback(receipt["release_id"], str(target), scope=scope, confirmed=True)
    assert rolled["rolled_back"] is True
    assert target.read_text(encoding="utf-8") == "previous release body\n"

    with sqlite3.connect(tmp_path / ".memoryguard" / "content" / "content.db") as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM content_holds WHERE blob_id=? AND active=1",
            (receipt["previous_blob_id"],),
        ).fetchone()[0] == 1
