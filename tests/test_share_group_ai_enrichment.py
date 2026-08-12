"""多 Agent：共享组 V2 enrich → graph 顺序 + MCP group filter。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")


def _native_context(workspace, share_group_id, agent_instance_id="agent-a"):
    from memoryguard.access_context import AccessContext
    from memoryguard.runtime_v2.native_ports import bind_native_transport_context

    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent_instance_id,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"test-{agent_instance_id}",
            session_source="test",
            session_trusted=True,
        ),
        workspace_id=str(workspace),
        share_group_id=share_group_id,
        project_ref="",
        provider="",
        runtime_role="",
        entrypoint="test",
    )


def test_share_group_build_enriches_then_graphs(tmp_path):
    from memoryguard.content import ContentStore
    from memoryguard.host_enrichment import apply_results, enqueue_from_shared_store, list_pending
    from memoryguard.projection_v2 import ProjectionReadScope
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.runtime_v2.projection_build import ProjectionBuildService
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    ws = tmp_path
    gid = "sg-enrich-1"
    ContentStore(ws)
    bound = GroupControlService(ws, write=True).bind_agents(
        ["agent-a", "agent-b"], share_group_id=gid,
    )
    assert bound["ok"] is True

    # Public facade now assembles the native V2 store/governance dependencies.
    org = AutoOrganizer(ws, gid)
    atom, _actions = org.organize(MemoryEvent(
        event_id="e1", agent_instance_id="agent-a", share_group_id=gid,
        raw_content="User prefers concise pytest based workflows always",
        metadata={},
    ))
    org.store.project_evidence(org.governance.evidence)
    org.store.set_visibility("active", atom_ids=[atom.atom_id])
    context = _native_context(ws, gid)

    assert enqueue_from_shared_store(ws, gid, context=context) == 1
    pending = list_pending(ws, share_group_id=gid, limit=50, context=context)
    assert len(pending) == 1
    applied = apply_results(
        ws,
        [{
            "task_id": pending[0]["task_id"],
            "kind": "preference",
            "title": "偏好：简洁 pytest 工作流",
            "body": "用户偏好简洁的 pytest 工作流。",
            "confidence": 0.9,
            "source": "host_cli",
        }],
        share_group_id=gid,
        context=context,
    )
    assert applied["applied"] == 1
    assert list_pending(ws, share_group_id=gid, limit=50, context=context) == []

    # Graph build is a separate V2 phase and happens only after enrichment.
    graph = ProjectionBuildService(ws).build(
        mode="reconstructed",
        scope=ProjectionReadScope(
            workspace_id=str(ws),
            agent_instance_id="agent-a",
            project_ref="",
            provider="",
            share_group_id=gid,
            sensitivity="normal",
            policy_class="private",
        ),
        runtime_role="",
    )
    assert graph["status"] == "succeeded"
    assert graph["atom_count"] >= 1


def test_share_group_mcp_list_share_group_filter(tmp_path):
    from memoryguard.content import ContentStore
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer
    from memoryguard.host_enrichment import enqueue_from_shared_store, list_pending
    from memoryguard.runtime_v2.group_native import GroupControlService
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort

    ws = tmp_path
    gid = "sg-mcp-1"
    ContentStore(ws)
    bound = GroupControlService(ws, write=True).bind_agents(
        ["agent-a", "agent-b"], share_group_id=gid,
    )
    assert bound["ok"] is True
    org = AutoOrganizer(ws, gid)
    atom, _actions = org.organize(MemoryEvent(
        event_id="e2", agent_instance_id="agent-a", share_group_id=gid,
        raw_content="Always prefer English docs in CI pipelines carefully",
        metadata={},
    ))
    org.store.project_evidence(org.governance.evidence)
    org.store.set_visibility("active", atom_ids=[atom.atom_id])
    context = _native_context(ws, gid)
    enqueue_from_shared_store(ws, gid, context=context)
    pending = list_pending(ws, share_group_id=gid, limit=50, context=context)
    assert len(pending) >= 1

    class _V2Active:
        def current(self):
            return {"state": "V2_ACTIVE", "generation": 7}

    result = NativeV2RuntimePort(ws, state_provider=_V2Active()).dispatch_mcp(
        "memoryguard_list_pending_enrichments",
        {"workspace": str(ws), "share_group_id": gid},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    assert result["data"]["pending_count"] >= 1

    foreign = NativeV2RuntimePort(ws, state_provider=_V2Active()).dispatch_mcp(
        "memoryguard_list_pending_enrichments",
        {"share_group_id": "other-group"},
        context=context,
        generation=7,
        state="V2_ACTIVE",
    )
    assert foreign["ok"] is False
