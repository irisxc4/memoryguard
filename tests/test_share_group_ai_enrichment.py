"""多 Agent：共享组构建内 LLM 整理 + MCP 残留兜底。"""
from __future__ import annotations

import os
from unittest.mock import patch
import pytest


@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")


def test_share_group_build_enriches_then_graphs(tmp_path):
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer
    from memoryguard.host_enrichment import list_pending

    ws = tmp_path
    api = GovernanceApi(ws)
    gid = "sg-enrich-1"
    api.bind_agent("agent-a", gid)
    api.bind_agent("agent-b", gid)

    org = AutoOrganizer(ws, gid)
    org.organize(MemoryEvent(
        event_id="e1", agent_instance_id="agent-a", share_group_id=gid,
        raw_content="User prefers concise pytest based workflows always",
        metadata={},
    ))

    def fake_enrich(workspace, tasks, on_progress=None, *, allow_host_cli=True,
                    llm_agent="", llm_cli=""):
        out = []
        for t in tasks:
            out.append({
                "task_id": t["task_id"],
                "kind": "preference",
                "title": "偏好：简洁 pytest 工作流",
                "body": "用户偏好简洁的 pytest 工作流。",
                "confidence": 0.9,
                "source": "host_cli",
            })
        return out

    with patch("memoryguard.gui._auto_enrich_tasks", side_effect=fake_enrich):
        graph = api.build_projection(
            confirmed=True,
            mode="reconstructed",
            scope={"mode": "share_group", "share_group_id": gid},
            llm_agent="codex",
            llm_cli="codex.exe",
        )

    assert graph.get("built") or graph.get("nodes") is not None
    enr = graph.get("enrichment") or {}
    assert enr.get("mode") == "build_integrated"
    assert enr.get("auto_applied", 0) >= 1
    # apply 后 pending 应清空（或极少残留）
    pending = list_pending(ws, share_group_id=gid, limit=50)
    assert len(pending) == 0


def test_share_group_mcp_list_share_group_filter(tmp_path):
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer
    from memoryguard.host_enrichment import enqueue_from_shared_store, list_pending
    from memoryguard.mcp_server import execute_tool
    import json

    ws = tmp_path
    api = GovernanceApi(ws)
    gid = "sg-mcp-1"
    api.bind_agent("agent-a", gid)
    AutoOrganizer(ws, gid).organize(MemoryEvent(
        event_id="e2", agent_instance_id="agent-a", share_group_id=gid,
        raw_content="Always prefer English docs in CI pipelines carefully",
        metadata={},
    ))
    enqueue_from_shared_store(ws, gid)
    pending = list_pending(ws, share_group_id=gid, limit=50)
    assert len(pending) >= 1

    result = execute_tool(
        "memoryguard_list_pending_enrichments",
        {"workspace": str(ws), "share_group_id": gid},
    )
    assert not result.get("isError"), result
    data = json.loads(result["content"][0]["text"])
    assert data.get("pending_count", 0) >= 1
