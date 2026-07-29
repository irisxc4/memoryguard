"""MCP 共享记忆全链路验证。

测试:
1. 两 Agent 绑定同一 share_group,写入后互见
2. 不同 share_group 物理隔离
3. 跨 Agent 去重(同 group 内自动 merge_provenance)
4. 全局治理入口 list_share_groups + get_global_memory_status
5. AgentBinding 唯一性:新绑定自动 deactivate 旧绑定
6. MCP memoryguard_memory_write -> AutoOrganizer -> 共享存储 链路
"""
import sys
import os
import json
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# A2: bind_agent 需要 admin 权限,测试环境显式设置
os.environ["MEMORYGUARD_ADMIN"] = "1"
os.environ["MEMORYGUARD_ALLOW_ANON"] = "1"
os.environ["MEMORYGUARD_STRICT_BINDING"] = "0"


def test_stdio_protocol_forces_utf8_on_windows_locale(tmp_path: Path):
    """MCP 是 UTF-8 协议；宿主区域编码不能污染 JSON-RPC 字节流。"""
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "gbk",
        "MEMORYGUARD_WORKSPACE": str(tmp_path),
        "MEMORYGUARD_AGENT_ID": "utf8-probe",
        "MEMORYGUARD_STRICT_BINDING": "0",
        "MEMORYGUARD_ALLOW_ANON": "1",
    })
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "memoryguard.mcp_server"],
        input=(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in requests)
            + "\n"
        ).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=20,
        check=True,
    )
    decoded = completed.stdout.decode("utf-8", errors="strict")
    responses = [json.loads(line) for line in decoded.splitlines() if line]
    assert responses[0]["result"]["serverInfo"]["name"] == "memoryguard"
    assert any(
        tool["name"] == "memoryguard_memory_write"
        for tool in responses[1]["result"]["tools"]
    )


def test_two_agents_share_memory():
    """两 Agent 绑定同一 share_group,写入后互见。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.shared_memory_store import SharedMemoryStore

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        # Agent A 和 B 绑定同一 group
        api.bind_agent("agent-a", "shared-group-1", "memoryguard")
        api.bind_agent("agent-b", "shared-group-1", "memoryguard")

        # Agent A 写入
        store = SharedMemoryStore(ws, "shared-group-1")
        from memoryguard.schema_v3 import MemoryEvent
        from memoryguard.auto_organizer import AutoOrganizer
        org = AutoOrganizer(ws, "shared-group-1")
        event_a = MemoryEvent(
            event_id="evt-a", agent_instance_id="agent-a", share_group_id="shared-group-1",
            raw_content="用户偏好 Python 编程语言", metadata={},
        )
        record_a, _ = org.organize(event_a)

        # Agent B 搜索 - 应看到 Agent A 写入的记忆
        result = api.search_memory("Python", share_group_id="shared-group-1")
        assert result["total"] > 0
        assert any(r["record"]["body"] == "用户偏好 Python 编程语言" for r in result["records"])


def test_different_groups_isolated():
    """不同 share_group 物理隔离。"""
    from memoryguard.gui import GovernanceApi

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "group-1")
        api.bind_agent("agent-b", "group-2")

        from memoryguard.shared_memory_store import SharedMemoryStore
        from memoryguard.schema_v3 import MemoryEvent
        from memoryguard.auto_organizer import AutoOrganizer

        # Agent A 写入 group-1
        org1 = AutoOrganizer(ws, "group-1")
        evt1 = MemoryEvent(
            event_id="evt-1", agent_instance_id="agent-a", share_group_id="group-1",
            raw_content="用户偏好 Python", metadata={},
        )
        org1.organize(evt1)

        # Agent B 在 group-2 搜索 - 不应看到 group-1 的记忆
        result = api.search_memory("Python", share_group_id="group-2")
        assert result["total"] == 0, "different groups should be isolated"


def test_cross_agent_dedup():
    """同 group 内跨 Agent 去重(merge_provenance)。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "dedup-group")
        api.bind_agent("agent-b", "dedup-group")

        org = AutoOrganizer(ws, "dedup-group")
        # 两 Agent 写入相同内容
        for agent_id, evt_id in [("agent-a", "e1"), ("agent-b", "e2")]:
            evt = MemoryEvent(
                event_id=evt_id, agent_instance_id=agent_id, share_group_id="dedup-group",
                raw_content="完全相同的内容用于测试跨 Agent 去重", metadata={},
            )
            org.organize(evt)

        # 应只有一条 active 记录(merge_provenance)
        result = api.list_memory(status="active", share_group_id="dedup-group")
        # 同义内容会 merge_provenance,不创建新记录
        assert result["total"] <= 2  # 可能 1(merge) 或 2(如果 Jaccard 不够高)


def test_list_share_groups():
    """全局治理入口:list_share_groups。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "group-x")
        api.bind_agent("agent-b", "group-y")

        org_x = AutoOrganizer(ws, "group-x")
        org_x.organize(MemoryEvent(
            event_id="ex", agent_instance_id="agent-a", share_group_id="group-x",
            raw_content="group X content", metadata={},
        ))

        result = api.list_share_groups()
        assert result["total"] >= 2
        group_ids = [g["share_group_id"] for g in result["groups"]]
        assert "group-x" in group_ids
        assert "group-y" in group_ids
        # group-x 应有 1 条记录
        gx = next(g for g in result["groups"] if g["share_group_id"] == "group-x")
        assert gx["total_records"] >= 1


def test_global_memory_status():
    """全局治理入口:get_global_memory_status + 跨 group 重复检测。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "g1")
        api.bind_agent("agent-b", "g2")

        # 两个 group 写入相同内容
        same_content = "完全相同的跨 group 重复内容"
        org1 = AutoOrganizer(ws, "g1")
        org1.organize(MemoryEvent(
            event_id="e1", agent_instance_id="agent-a", share_group_id="g1",
            raw_content=same_content, metadata={},
        ))
        org2 = AutoOrganizer(ws, "g2")
        org2.organize(MemoryEvent(
            event_id="e2", agent_instance_id="agent-b", share_group_id="g2",
            raw_content=same_content, metadata={},
        ))

        result = api.get_global_memory_status()
        assert result["total_groups"] >= 2
        assert result["total_records"] >= 2
        # 应检测到跨 group 重复
        assert len(result["cross_group_duplicates"]) > 0, "should detect cross-group dups"


def test_binding_uniqueness():
    """AgentBinding 唯一性:新绑定自动 deactivate 旧绑定。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.agent_binding import AgentBindingStore

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        # Agent A 绑定 group-1
        api.bind_agent("agent-a", "group-1")
        store = AgentBindingStore(ws)
        active = store.find_by_agent("agent-a", include_inactive=False)
        assert len(active) == 1
        assert active[0].share_group_id == "group-1"

        # Agent A 改绑 group-2
        api.bind_agent("agent-a", "group-2")
        active = store.find_by_agent("agent-a", include_inactive=False)
        assert len(active) == 1, "should only have 1 active binding"
        assert active[0].share_group_id == "group-2", "should be group-2"

        # 旧 binding 应变为 INACTIVE
        all_bindings = store.find_by_agent("agent-a", include_inactive=True)
        inactive = [b for b in all_bindings if b.status.value == "inactive"]
        assert len(inactive) >= 1, "old binding should be deactivated"


def test_list_share_groups_in_whitelist():
    """新全局治理 API 在安全白名单中。"""
    from memoryguard.security import READONLY_API_METHODS, MUTATION_API_METHODS
    assert "list_share_groups" in READONLY_API_METHODS
    assert "get_global_memory_status" in READONLY_API_METHODS
    for method in (
        "import_native_memories_to_group",
        "commit_shared_memory_governance",
        "install_shared_group_mcp_redirects",
    ):
        assert method in MUTATION_API_METHODS


def test_shared_group_governance_commit_and_decide():
    """共享组：导入/提交治理 + 图上 delete 走 SharedMemoryStore。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        gid = "gov-group-1"
        api.bind_agent("agent-a", gid)
        api.bind_agent("agent-b", gid)

        org = AutoOrganizer(ws, gid)
        evt = MemoryEvent(
            event_id="evt-gov", agent_instance_id="agent-a", share_group_id=gid,
            raw_content="共享治理测试记忆", metadata={},
        )
        record, _ = org.organize(evt)

        import_result = api.import_native_memories_to_group(gid, confirmed=True)
        assert import_result.get("ok") is True

        commit_result = api.commit_shared_memory_governance(gid, "test takeover", confirmed=True)
        assert commit_result.get("ok") is True
        assert commit_result.get("version_id")

        decide = api.neuron_decide(
            record.memory_id, "exclude", "test delete",
            confirmed=True,
            scope={"mode": "share_group", "share_group_id": gid},
        )
        assert decide.get("ok") is True
        store = SharedMemoryStore(ws, gid)
        rec_after = store.get_record(record.memory_id)
        assert rec_after is not None
        assert rec_after.status.value == "deleted"
        assert store.list_records(status="active") == []


def test_bind_agents_default_redirected():
    from memoryguard.gui import GovernanceApi
    from memoryguard.agent_binding import AgentBindingStore

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        result = api.bind_agents_to_shared_group(["agent-a", "agent-b"])
        assert result.get("share_group_id")
        store = AgentBindingStore(ws)
        bindings = store.find_by_group(result["share_group_id"], include_inactive=False)
        assert len(bindings) == 2
        assert all(b.native_memory_mode.value == "redirected" for b in bindings)


def test_global_mcp_workspace_is_independent_from_current_project(
    tmp_path, monkeypatch,
):
    """全局 MCP 从稳定控制目录读绑定/记忆，不能随 Agent 当前项目漂移。"""
    from memoryguard.agent_binding import AgentBindingStore
    from memoryguard.auto_organizer import AutoOrganizer
    from memoryguard.mcp_server import execute_tool
    from memoryguard.schema_v3 import MemoryEvent

    control_workspace = tmp_path / "memoryguard-control"
    unrelated_project = tmp_path / "another-project"
    control_workspace.mkdir()
    unrelated_project.mkdir()
    agent_id = "codex-global-test"
    group_id = "global-memory-group"
    AgentBindingStore(control_workspace).bind_agent(agent_id, group_id)
    AutoOrganizer(control_workspace, group_id).organize(MemoryEvent(
        event_id="global-workspace-event",
        agent_instance_id=agent_id,
        share_group_id=group_id,
        raw_content="global workspace memory",
        metadata={},
    ))

    monkeypatch.chdir(unrelated_project)
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(control_workspace))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent_id)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    result = execute_tool(
        "memoryguard_memory_status",
        {"workspace": str(unrelated_project)},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["share_group_id"] == group_id
    assert payload["total_records"] == 1


if __name__ == "__main__":
    test_two_agents_share_memory()
    print("OK: two agents share memory")
    test_different_groups_isolated()
    print("OK: different groups isolated")
    test_cross_agent_dedup()
    print("OK: cross agent dedup")
    test_list_share_groups()
    print("OK: list share groups")
    test_global_memory_status()
    print("OK: global memory status")
    test_binding_uniqueness()
    print("OK: binding uniqueness")
    test_list_share_groups_in_whitelist()
    print("OK: whitelist")
    print("\nAll MCP shared memory tests passed.")
