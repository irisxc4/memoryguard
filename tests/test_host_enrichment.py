"""Native V2 host-enrichment workflow tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from _publish_helpers import (
    build_projection,
    native_context,
    projection_scope,
    seed_atom,
)
from memoryguard.content import ContentStore
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.memory.store import stable_digest
from memoryguard.runtime_v2.extraction_native import NativeExtractionEnrichmentService
from memoryguard.runtime_v2.projection_build import ProjectionBuildService


def _memory_scope(workspace: Path, agent_id: str = "agent-A") -> MemoryReadScope:
    return MemoryReadScope(
        workspace_id=str(workspace.resolve()),
        share_group_id="group-test",
        agent_instance_id=agent_id,
        project_ref=str(workspace.resolve()),
        provider="test",
        runtime_role="test",
    )


def _setup_workspace(tmp_path: Path, agent_id: str = "agent-A") -> tuple[Path, object]:
    workspace = tmp_path / f"workspace-{agent_id}"
    workspace.mkdir()
    ContentStore(workspace)
    seed_atom(
        workspace,
        "mem-en-001",
        "The project uses pnpm as the package manager. Always run pnpm install and pnpm add.",
        kind="fact",
        confidence=0.5,
        agent_id=agent_id,
        metadata={"title": "Use pnpm instead of npm for package management", "scope": "project"},
    )
    seed_atom(
        workspace,
        "mem-zh-001",
        "用户喜欢简洁的回复风格，不需要过多解释。",
        kind="preference",
        confidence=0.9,
        agent_id=agent_id,
        metadata={"title": "用户偏好简洁回复", "scope": "project"},
    )
    seed_atom(
        workspace,
        "mem-model-001",
        "已整理的流程",
        kind="procedure",
        confidence=0.8,
        agent_id=agent_id,
        metadata={"title": "已整理的流程", "scope": "project", "enrichment_mode": "host"},
    )
    return workspace, native_context(workspace, agent_id=agent_id)


def _build(workspace: Path, context: object) -> tuple[NativeExtractionEnrichmentService, dict]:
    service = NativeExtractionEnrichmentService(workspace)
    return service, service.build_and_enrich({}, context=context)


def _pending(service: NativeExtractionEnrichmentService, context: object) -> list[dict]:
    return service.list_pending({"limit": 50}, context=context)["tasks"]


def test_enqueue_idempotent(tmp_path: Path) -> None:
    workspace, context = _setup_workspace(tmp_path)
    service, first = _build(workspace, context)
    tasks_first = _pending(service, context)
    _service, second = _build(workspace, context)
    tasks_second = _pending(service, context)

    assert first["queued_or_pending"] >= 1
    assert second["queued_or_pending"] >= 1
    assert len(tasks_first) == 1
    assert [task["task_id"] for task in tasks_second] == [tasks_first[0]["task_id"]]
    assert tasks_first[0]["memory_id"] == "mem-en-001"
    assert set(tasks_first[0]["ops"]) == {"classify", "translate"}


def test_build_projection_integrated_enrich(tmp_path: Path) -> None:
    workspace, context = _setup_workspace(tmp_path)
    service, summary = _build(workspace, context)

    assert summary["projection_built"] is True
    assert summary["projection_mode"] == "v2_native_memory"
    assert summary["host_action_required"] is True
    assert _pending(service, context)
    projection = build_projection(
        workspace,
        mode="native",
        scope=projection_scope(workspace, agent_id="agent-A"),
        runtime_role="test",
    )
    assert projection["status"] == "succeeded"


def test_apply_valid_kind() -> None:
    with tempfile.TemporaryDirectory(prefix="mg_host_valid_") as raw:
        workspace, context = _setup_workspace(Path(raw))
        service, _summary = _build(workspace, context)
        task = _pending(service, context)[0]

        result = service.apply_enrichments(
            {
                "results": [{
                    "task_id": task["task_id"],
                    "kind": "preference",
                    "title": "使用 pnpm 而非 npm",
                    "body": "项目使用 pnpm 作为包管理器。始终运行 pnpm install 和 pnpm add。",
                    "confidence": 0.9,
                    "rationale": "translated by host AI",
                }],
            },
            context=context,
        )
        atom = MemoryAtomStore(workspace, readonly=True).get_atom(
            "mem-en-001", scope=_memory_scope(workspace), include_building=True
        )

        assert result["applied"] == 1
        assert result["rejected"] == 0
        assert result["rebuild_suggested"] is False
        assert atom is not None
        assert atom.kind == "preference"
        assert atom.body.startswith("使用 pnpm 而非 npm")
        assert atom.metadata["enrichment_mode"] == "host"
        assert atom.canonical_hash == stable_digest(atom.body)
        assert atom.metadata["enrichment_terminal_fp"]
        assert _pending(service, context) == []
        rebuilt = service.build_and_enrich({}, context=context)
        assert rebuilt["host_action_required"] is False
        assert rebuilt["pending_tasks"] == []
        assert _pending(service, context) == []


def test_apply_invalid_kind() -> None:
    with tempfile.TemporaryDirectory(prefix="mg_host_invalid_") as raw:
        workspace, context = _setup_workspace(Path(raw))
        service, _summary = _build(workspace, context)
        task = _pending(service, context)[0]
        before = MemoryAtomStore(workspace, readonly=True).get_atom(
            "mem-en-001", scope=_memory_scope(workspace), include_building=True
        )

        result = service.dispatch(
            "apply",
            {"results": [{"task_id": task["task_id"], "kind": "invalid_kind", "title": "测试", "body": "测试"}]},
            context=context,
        )
        after = MemoryAtomStore(workspace, readonly=True).get_atom(
            "mem-en-001", scope=_memory_scope(workspace), include_building=True
        )

        assert result["ok"] is False
        assert result["code"] == "invalid_enrichment_result"
        assert before is not None and after is not None
        assert after.to_dict() == before.to_dict()
        assert _pending(service, context)


def test_cross_agent_scope() -> None:
    with tempfile.TemporaryDirectory(prefix="mg_host_scope_") as raw:
        workspace, context_a = _setup_workspace(Path(raw), "agent-A")
        service, _summary = _build(workspace, context_a)
        task = _pending(service, context_a)[0]
        context_b = native_context(workspace, agent_id="agent-B")

        assert service.list_pending({}, context=context_b)["pending_count"] == 0
        result = service.dispatch(
            "apply",
            {"results": [{"task_id": task["task_id"], "kind": "preference", "title": "hijack", "body": "hijack"}]},
            context=context_b,
        )
        assert result["ok"] is False
        assert result["code"] == "enrichment_task_not_found"


def test_native_tools_smoke(tmp_path: Path) -> None:
    workspace, context = _setup_workspace(tmp_path)
    service, _summary = _build(workspace, context)

    listed = service.dispatch("list_pending", {"limit": 50}, context=context)
    status = service.dispatch("status", {}, context=context)

    assert listed["ok"] is True
    assert listed["data"]["pending_count"] >= 1
    assert "tasks" in listed["data"]
    assert status["ok"] is True
    assert status["data"]["pending"] >= 1


def test_get_status(tmp_path: Path) -> None:
    workspace, context = _setup_workspace(tmp_path)
    service, _summary = _build(workspace, context)

    status = service.enrichment_status({}, context=context)

    assert status["pending"] >= 1
    assert status["total"] >= 1
    assert status["mode"] == "v2_content_plane"


def test_heuristic_source_not_model() -> None:
    with tempfile.TemporaryDirectory(prefix="mg_host_heuristic_") as raw:
        workspace, context = _setup_workspace(Path(raw))
        service, _summary = _build(workspace, context)
        task = _pending(service, context)[0]

        result = service.apply_enrichments(
            {"results": [{
                "task_id": task["task_id"],
                "kind": "fact",
                "title": "heuristic result",
                "body": "heuristic body",
                "confidence": 0.4,
                "source": "heuristic",
            }]},
            context=context,
        )
        atom = MemoryAtomStore(workspace, readonly=True).get_atom(
            "mem-en-001", scope=_memory_scope(workspace), include_building=True
        )

        assert result["applied"] == 1
        assert atom is not None
        assert atom.metadata["enrichment_mode"] == "host"
        assert "heuristic result" in atom.body


def test_apply_then_reload_atom() -> None:
    with tempfile.TemporaryDirectory(prefix="mg_host_reload_") as raw:
        workspace, context = _setup_workspace(Path(raw))
        service, _summary = _build(workspace, context)
        task = _pending(service, context)[0]
        service.apply_enrichments(
            {"results": [{
                "task_id": task["task_id"],
                "kind": "preference",
                "title": "translated title",
                "body": "translated body",
                "confidence": 0.9,
            }]},
            context=context,
        )

        reloaded = MemoryAtomStore(workspace, readonly=True).get_atom(
            "mem-en-001", scope=_memory_scope(workspace), include_building=True
        )

        assert reloaded is not None
        assert reloaded.body.startswith("translated title")
        assert "translated body" in reloaded.body
        assert reloaded.kind == "preference"
        assert reloaded.metadata["enrichment_mode"] == "host"
        assert _pending(service, context) == []


def test_batch_index_no_offset() -> None:
    """Regression: the second host batch keeps its local result indices."""
    from memoryguard.host_agent_backend import batch_enrich_via_cli

    with tempfile.TemporaryDirectory(prefix="mg_batch_") as raw:
        tasks = [{
            "task_id": f"task-{index:03d}",
            "memory_id": f"mem-{index:03d}",
            "input": {"title": f"English title {index}", "body": f"English body {index}", "kind_hint": "fact"},
        } for index in range(25)]
        call_log: list[dict] = []

        def mock_call(agent, cli, system, user, timeout=60, expect_array=False):
            data = json.loads(user)
            result = [{
                "index": index,
                "kind": "fact",
                "title": f"translated-{item['task_id']}",
                "body": "body",
                "confidence": 0.8,
            } for index, item in enumerate(data)]
            call_log.append({"batch_size": len(data), "task_ids": [item["task_id"] for item in data]})
            return result

        with patch("memoryguard.host_agent_backend._call_llm_json", side_effect=mock_call):
            results = batch_enrich_via_cli(tasks, agent="mock", cli_path="mock", workspace=raw)

        task_ids = {task["task_id"] for task in tasks}
        result_ids = {result["task_id"] for result in results}
        assert result_ids.issubset(task_ids)
        assert len(result_ids) == len(results)
        assert len(results) == 25
        result_map = {result["task_id"]: result for result in results}
        for index in range(25):
            task_id = f"task-{index:03d}"
            assert result_map[task_id]["title"] == f"translated-{task_id}"
        assert [item["batch_size"] for item in call_log] == [20, 5]
