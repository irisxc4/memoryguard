"""Host AI Enrichment 队列测试。

测试覆盖:
1. enqueue 幂等 / 入队条件
2. apply 合法 kind 写回 localization_mode=model
3. apply 非法 kind -> rejected, IR 不变
4. build_projection 返回 pending_count(冒烟)
5. MCP list/apply 工具形状冒烟
6. 跨 agent scope: B 不能 apply A 的 task
"""
import sys
import os
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from memoryguard.host_enrichment import (
    enqueue_from_ir, list_pending, apply_results, get_status,
)
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.schema_v3 import MemoryRecord, MemoryKind, MemoryStatus, Completeness, Provenance, stable_hash, _now_iso


def _make_ir():
    """创建测试用 IR。"""
    records = [
        MemoryRecord(
            memory_id="mem-en-001",
            kind=MemoryKind.FACT,
            title="Use pnpm instead of npm for package management",
            body="The project uses pnpm as the package manager. Always run pnpm install and pnpm add.",
            scope="project",
            original_title="",
            original_body="",
            display_language="en",
            localization_mode="heuristic",
            confidence=0.5,
            provenance=[Provenance(source_object_id="src-1", locator="file.md#L1", excerpt_hash="h1", source_revision="r1")],
            status=MemoryStatus.CANDIDATE,
            completeness=Completeness.VERIFIABLE,
        ),
        MemoryRecord(
            memory_id="mem-zh-001",
            kind=MemoryKind.PREFERENCE,
            title="用户偏好简洁回复",
            body="用户喜欢简洁的回复风格，不需要过多解释。",
            scope="project",
            localization_mode="none",
            confidence=0.9,
            provenance=[Provenance(source_object_id="src-2", locator="file.md#L2", excerpt_hash="h2", source_revision="r2")],
            status=MemoryStatus.CANDIDATE,
            completeness=Completeness.VERIFIABLE,
        ),
        MemoryRecord(
            memory_id="mem-model-001",
            kind=MemoryKind.PROCEDURE,
            title="已整理的流程",
            body="This is already model-enriched.",
            scope="project",
            localization_mode="model",
            confidence=0.8,
            provenance=[Provenance(source_object_id="src-3", locator="file.md#L3", excerpt_hash="h3", source_revision="r3")],
            status=MemoryStatus.CANDIDATE,
            completeness=Completeness.VERIFIABLE,
        ),
    ]
    return MemoryIR(records=records, snapshot_id="test-snap", created_at=_now_iso())


def _setup_workspace():
    """创建临时 workspace 并写入 IR。"""
    ws = tempfile.mkdtemp(prefix="mg_test_")
    norm = MemoryNormalizer(ws)
    ir = _make_ir()
    norm.save(ir)
    return ws, ir


def test_enqueue_idempotent():
    """1. enqueue 幂等 / 入队条件。"""
    ws, ir = _setup_workspace()
    try:
        scope = {"agent_instance_id": "agent-A"}
        count1 = enqueue_from_ir(ws, ir, scope, reason="test")
        # mem-en-001: 英文 heuristic conf=0.5 -> 入队
        # mem-zh-001: 中文 conf=0.9 但 kind=preference -> 不入队(不满足条件)
        # mem-model-001: 已 model -> 跳过
        assert count1 >= 1, f"expected >=1, got {count1}"
        
        # 第二次入队应幂等(0 新增)
        count2 = enqueue_from_ir(ws, ir, scope, reason="test2")
        assert count2 == 0, f"expected 0 (idempotent), got {count2}"
        
        # list 验证
        tasks = list_pending(ws, agent_instance_id="agent-A")
        assert len(tasks) >= 1
        
        # 验证英文记录入了队
        en_tasks = [t for t in tasks if t["memory_id"] == "mem-en-001"]
        assert len(en_tasks) == 1
        assert en_tasks[0]["task_id"].startswith("enr-")
        assert "classify" in en_tasks[0]["ops"]
        assert "translate" in en_tasks[0]["ops"]
        
        print("OK: enqueue idempotent + condition")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_build_projection_integrated_enrich():
    """构建路径：入队后自动整理，返回 build_integrated。"""
    ws, ir = _setup_workspace()
    try:
        from memoryguard.gui import GovernanceApi
        from unittest.mock import patch

        api = GovernanceApi(ws)
        # 最小 agent scope：直接塞 IR 后构建会因无 authorized roots 失败；
        # 这里只测 _enrich_pending_during_build + enrichment 摘要拼装用 share_group 更稳。
        # 改测：enqueue + enrich helper。
        from memoryguard.gui import _enrich_pending_during_build

        enqueue_from_ir(ws, ir, {"agent_instance_id": "agent-A"})
        pending = list_pending(ws, agent_instance_id="agent-A")
        assert len(pending) >= 1

        def fake_enrich(workspace, tasks, on_progress=None, *, allow_host_cli=True,
                        llm_agent="", llm_cli=""):
            return [{
                "task_id": tasks[0]["task_id"],
                "kind": "preference",
                "title": "使用 pnpm",
                "body": "项目使用 pnpm。",
                "confidence": 0.9,
                "source": "host_cli",
            }]

        with patch("memoryguard.gui._auto_enrich_tasks", side_effect=fake_enrich):
            stats = _enrich_pending_during_build(
                ws, agent_instance_id="agent-A", llm_agent="codex", llm_cli="x",
            )
        assert stats.get("applied", 0) == 1
        assert stats.get("engine") == "host_cli"
        assert list_pending(ws, agent_instance_id="agent-A") == []
        print("OK: build-integrated enrich helper")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_apply_valid_kind():
    """2. apply 合法 kind 写回 localization_mode=model。"""
    ws, ir = _setup_workspace()
    try:
        scope = {"agent_instance_id": "agent-A"}
        enqueue_from_ir(ws, ir, scope)
        tasks = list_pending(ws, agent_instance_id="agent-A")
        en_task = next(t for t in tasks if t["memory_id"] == "mem-en-001")
        
        results = [{
            "task_id": en_task["task_id"],
            "kind": "preference",
            "title": "使用 pnpm 而非 npm",
            "body": "项目使用 pnpm 作为包管理器。始终运行 pnpm install 和 pnpm add。",
            "confidence": 0.9,
            "rationale": "translated by host AI",
        }]
        stats = apply_results(ws, results, agent_instance_id="agent-A")
        assert stats["applied"] == 1, f"expected 1 applied, got {stats}"
        assert stats["rejected"] == 0
        assert stats["rebuild_suggested"] is True
        
        # 验证 IR 已更新
        norm = MemoryNormalizer(ws)
        ir2 = norm.load()
        rec = next(r for r in ir2.records if r.memory_id == "mem-en-001")
        assert rec.localization_mode == "model"
        assert rec.kind == MemoryKind.PREFERENCE
        assert "pnpm" in rec.title
        assert rec.original_title  # 原文保留
        assert rec.confidence == 0.9
        
        # 验证 task 已标记 applied
        tasks2 = list_pending(ws, agent_instance_id="agent-A")
        assert len(tasks2) == 0  # pending 为空
        
        print("OK: apply valid kind -> localization_mode=model")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_apply_invalid_kind():
    """3. apply 非法 kind -> rejected, IR 不变。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": "agent-A"})
        tasks = list_pending(ws, agent_instance_id="agent-A")
        en_task = next(t for t in tasks if t["memory_id"] == "mem-en-001")
        
        results = [{
            "task_id": en_task["task_id"],
            "kind": "invalid_kind",
            "title": "测试",
            "body": "测试",
        }]
        stats = apply_results(ws, results, agent_instance_id="agent-A")
        assert stats["applied"] == 0
        assert stats["rejected"] == 1
        
        # IR 不变
        norm = MemoryNormalizer(ws)
        ir2 = norm.load()
        rec = next(r for r in ir2.records if r.memory_id == "mem-en-001")
        assert rec.localization_mode == "heuristic"  # 未改
        
        print("OK: apply invalid kind -> rejected")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_cross_agent_scope():
    """6. 跨 agent scope: B 不能 apply A 的 task。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": "agent-A"})
        tasks = list_pending(ws, agent_instance_id="agent-A")
        en_task = next(t for t in tasks if t["memory_id"] == "mem-en-001")
        
        # agent-B 尝试 apply agent-A 的 task
        results = [{
            "task_id": en_task["task_id"],
            "kind": "preference",
            "title": "hijack",
            "body": "hijack",
        }]
        stats = apply_results(ws, results, agent_instance_id="agent-B")
        assert stats["applied"] == 0
        assert stats["rejected"] >= 1
        
        print("OK: cross-agent scope blocked")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_mcp_tools_smoke():
    """5. MCP list/apply 工具形状冒烟。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": ""})
        
        # 模拟 MCP execute_tool
        from memoryguard.mcp_server import execute_tool
        
        # list
        result = execute_tool("memoryguard_list_pending_enrichments", {"workspace": ws})
        assert not result.get("isError"), f"list failed: {result}"
        text = result["content"][0]["text"]
        data = json.loads(text)
        assert "pending_count" in data
        assert "tasks" in data
        
        # status
        result2 = execute_tool("memoryguard_enrichment_status", {"workspace": ws})
        assert not result2.get("isError")
        data2 = json.loads(result2["content"][0]["text"])
        assert "pending" in data2
        
        print("OK: MCP tools smoke test")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_get_status():
    """status 返回正确计数。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": ""})
        status = get_status(ws)
        assert status["pending"] >= 1
        assert status["total"] >= 1
        
        print("OK: get_status")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_heuristic_source_not_model():
    """回归: source=heuristic 时 localization_mode 应为 heuristic,不是 model。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": ""})
        tasks = list_pending(ws, agent_instance_id="")
        en_task = next(t for t in tasks if t["memory_id"] == "mem-en-001")

        results = [{
            "task_id": en_task["task_id"],
            "kind": "fact",
            "title": "heuristic result",
            "body": "heuristic body",
            "confidence": 0.4,
            "source": "heuristic",
        }]
        stats = apply_results(ws, results, agent_instance_id="")
        assert stats["applied"] == 1

        norm = MemoryNormalizer(ws)
        ir2 = norm.load()
        rec = next(r for r in ir2.records if r.memory_id == "mem-en-001")
        assert rec.localization_mode == "heuristic", f"expected heuristic, got {rec.localization_mode}"
        assert rec.confidence == 0.4

        print("OK: heuristic source -> localization_mode=heuristic")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_apply_then_reload_ir():
    """回归: apply 后重新 load IR,新 IR 应包含整理后的数据。"""
    ws, ir = _setup_workspace()
    try:
        enqueue_from_ir(ws, ir, {"agent_instance_id": ""})
        tasks_before = list_pending(ws, agent_instance_id="")
        en_task = next(t for t in tasks_before if t["memory_id"] == "mem-en-001")

        # apply
        results = [{
            "task_id": en_task["task_id"],
            "kind": "preference",
            "title": "translated title",
            "body": "translated body",
            "confidence": 0.9,
            "source": "host_cli",
        }]
        apply_results(ws, results, agent_instance_id="")

        # reload IR -- 模拟 build_projection 的 P0-1 修复
        norm = MemoryNormalizer(ws)
        ir_reloaded = norm.load()

        rec = next(r for r in ir_reloaded.records if r.memory_id == "mem-en-001")
        assert rec.title == "translated title", f"expected translated title, got {rec.title}"
        assert rec.body == "translated body"
        assert rec.localization_mode == "model"
        assert rec.kind.value == "preference"

        # pending 应为空
        tasks_after = list_pending(ws, agent_instance_id="")
        assert len(tasks_after) == 0

        print("OK: apply -> reload IR -> new data visible")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_batch_index_no_offset():
    """回归: batch_enrich_via_cli 的 index 不应有偏移。

    用 mock 验证:第 2 批的 task 不会被错绑到第 1 批的 task。
    """
    import tempfile
    ws = tempfile.mkdtemp(prefix="mg_batch_")
    try:
        from memoryguard.host_agent_backend import batch_enrich_via_cli
        from unittest.mock import patch

        # 构造 25 条英文 task(超过 batch_size=20)
        tasks = []
        for i in range(25):
            tasks.append({
                "task_id": f"task-{i:03d}",
                "memory_id": f"mem-{i:03d}",
                "input": {"title": f"English title {i}", "body": f"English body {i}", "kind_hint": "fact"},
            })

        # mock _call_llm_json: 返回的 index 是批内局部下标
        call_log = []
        def mock_call(agent, cli, system, user, timeout=60, expect_array=False):
            data = json.loads(user)
            result = [{"index": i, "kind": "fact", "title": f"translated-{item['task_id']}",
                       "body": "body", "confidence": 0.8} for i, item in enumerate(data)]
            call_log.append({"batch_size": len(data), "task_ids": [d["task_id"] for d in data]})
            return result

        with patch("memoryguard.host_agent_backend._call_llm_json", side_effect=mock_call):
            results = batch_enrich_via_cli(tasks, agent="mock", cli_path="mock", workspace=ws)

        # 验证: 每个 result 的 task_id 对应正确的 task
        task_ids = {t["task_id"] for t in tasks}
        result_ids = {r["task_id"] for r in results}
        assert result_ids.issubset(task_ids), "batch result has unknown task_id (index offset bug)"
        assert len(result_ids) == len(results), "duplicate task_id in results"
        assert len(results) == 25, f"expected 25 results, got {len(results)}"

        # 验证: 第 2 批(21-24)的 result title 对应正确的 task
        result_map = {r["task_id"]: r for r in results}
        for i in range(25):
            tid = f"task-{i:03d}"
            assert tid in result_map, f"missing task {tid}"
            assert result_map[tid]["title"] == f"translated-{tid}", \
                f"task {tid} has wrong title: {result_map[tid]['title']}"

        # 验证: 调了 2 批(20 + 5)
        assert len(call_log) == 2, f"expected 2 batches, got {len(call_log)}"
        assert call_log[0]["batch_size"] == 20
        assert call_log[1]["batch_size"] == 5

        print(f"OK: batch index no offset (25 tasks, 2 batches, all task_ids match)")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    test_enqueue_idempotent()
    test_build_projection_integrated_enrich()
    test_apply_valid_kind()
    test_apply_invalid_kind()
    test_cross_agent_scope()
    test_mcp_tools_smoke()
    test_get_status()
    test_heuristic_source_not_model()
    test_apply_then_reload_ir()
    test_batch_index_no_offset()
    print("\nAll host enrichment tests passed.")
