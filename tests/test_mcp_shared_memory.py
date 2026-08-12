"""MCP 共享记忆全链路验证。

测试:
1. 两 Agent 绑定同一 share_group,写入后互见
2. 不同 share_group 物理隔离
3. 跨 Agent 去重(同 group 内自动 merge_provenance)
4. 全局治理入口 list_share_groups + get_global_memory_status
5. AgentBinding 唯一性:新绑定自动 deactivate 旧绑定
6. MCP memoryguard_memory_write -> V2 AutoOrganizer -> V2 记忆域链路
"""
import sys
import os
import json
import subprocess
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryguard.access_context import AccessContext
from memoryguard.evidence.store import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.gui import GovernanceApi
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.mcp_server import execute_tool
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState


def _activate_v2_workspace(root: Path) -> None:
    """Create the real V2 domains and persist a V2_ACTIVE manifest."""
    layout = WorkspaceV2Layout(root)
    initialize_all(layout)
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    GovernanceV2(root, memory_store=memory, evidence_store=evidence)

    manager = ManifestManager(root)
    if manager.current().state is ManifestState.V2_ACTIVE:
        return
    manager.transition(ManifestState.V2_BUILDING, migration_id="mcp-shared-memory-fixture")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="mcp-shared-memory-source",
        target_digest="mcp-shared-memory-target",
        manifest_digest="mcp-shared-memory-manifest",
        digests={"validator_passed": True, "checkpoints": {"mcp": True}},
    )
    active = manager.transition(ManifestState.V2_ACTIVE)
    assert active.state is ManifestState.V2_ACTIVE


def _bind_v2_group(root: Path, group: str, agents: list[str]) -> None:
    result = GroupControlService(root, write=True).bind_agents(
        agents,
        share_group_id=group,
    )
    assert result["share_group_id"] == group
    assert result["member_count"] == len(agents)


def _configure_mcp_identity(monkeypatch, root: Path, agent: str) -> None:
    workspace = str(root.resolve())
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", workspace)
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", agent)
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_SESSION_ID", f"mcp-shared-{agent}")
    monkeypatch.setenv("MEMORYGUARD_SESSION_SOURCE", "transport")
    monkeypatch.setenv("MEMORYGUARD_SESSION_TRUSTED", "1")
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", workspace)


def _mcp_data(result: dict) -> dict:
    assert result.get("isError") is not True, result
    payload = json.loads(result["content"][0]["text"])
    return payload["data"]


def _mcp_write(
    monkeypatch,
    root: Path,
    agent: str,
    group: str,
    memory_id: str,
    body: str,
) -> dict:
    _configure_mcp_identity(monkeypatch, root, agent)
    return _mcp_data(execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(root.resolve()),
            "memory_id": memory_id,
            "body": body,
            "kind": "preference",
            "visibility": "active",
            "evidence_ids": [f"evidence-{memory_id}"],
            "audience": {"target_type": "group", "target_id": group},
            "idempotency_key": f"write-{memory_id}",
        },
    ))


def _gui_api(root: Path, agent: str) -> GovernanceApi:
    return GovernanceApi(
        str(root.resolve()),
        _trusted_access_context=AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"gui-shared-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
    )

# A2: bind_agent 需要 admin 权限。保持测试能力隔离，避免模块导入时
# 污染后续跨 Agent 权限测试的进程环境。
@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")


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


def test_two_agents_share_memory(tmp_path: Path, monkeypatch):
    """两 Agent 绑定同一 share_group,写入后互见。"""
    group = "shared-group-1"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, group, ["agent-a", "agent-b"])

    body = "用户偏好 Python 编程语言"
    written = _mcp_write(monkeypatch, tmp_path, "agent-a", group, "memory-a", body)
    assert written["atom"]["share_group_id"] == group

    result = _gui_api(tmp_path, "agent-b").search_memory(
        "Python",
        share_group_id=group,
    )
    assert result["ok"] is True, result
    assert any(
        item["share_group_id"] == group and item["body"] == body
        for item in result["data"]
    )


def test_different_groups_isolated(tmp_path: Path, monkeypatch):
    """不同 share_group 物理隔离。"""
    group_a = "group-1"
    group_b = "group-2"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, group_a, ["agent-a", "agent-a-peer"])
    _bind_v2_group(tmp_path, group_b, ["agent-b", "agent-b-peer"])

    _mcp_write(monkeypatch, tmp_path, "agent-a", group_a, "memory-group-a", "用户偏好 Python")
    result = _gui_api(tmp_path, "agent-b").search_memory(
        "Python",
        share_group_id=group_b,
    )
    assert result["ok"] is True, result
    assert result["data"] == [], "different groups should be isolated"


def test_cross_agent_dedup(tmp_path: Path, monkeypatch):
    """同 group 内跨 Agent 去重(merge_provenance)。"""
    group = "dedup-group"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, group, ["agent-a", "agent-b"])

    body = "完全相同的内容用于测试跨 Agent 去重"
    first = _mcp_write(monkeypatch, tmp_path, "agent-a", group, "memory-dedup-a", body)
    second = _mcp_write(monkeypatch, tmp_path, "agent-b", group, "memory-dedup-b", body)
    assert first["atom"]["memory_id"] == second["atom"]["memory_id"]
    assert any(action["action"] == "merge_provenance" for action in second["actions"])

    memory = MemoryAtomStore(tmp_path)
    atoms = memory.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(tmp_path.resolve()),
            share_group_id=group,
            admin=True,
        ),
        include_building=True,
    )
    assert len(atoms) == 1
    assert {item["agent_instance_id"] for item in atoms[0].provenance} == {
        "agent-a",
        "agent-b",
    }
    assert "body" not in atoms[0].metadata


def test_list_share_groups(tmp_path: Path, monkeypatch):
    """全局治理入口:list_share_groups。"""
    group_x = "group-x"
    group_y = "group-y"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, group_x, ["agent-a", "agent-x-peer"])
    _bind_v2_group(tmp_path, group_y, ["agent-b", "agent-y-peer"])
    _mcp_write(monkeypatch, tmp_path, "agent-a", group_x, "memory-group-x", "group X content")

    result = _gui_api(tmp_path, "agent-a").list_share_groups()
    assert result["ok"] is True, result
    data = result["data"]
    assert data["total_groups"] >= 2
    group_ids = [group["share_group_id"] for group in data["groups"]]
    assert group_x in group_ids
    assert group_y in group_ids
    group_x_data = next(group for group in data["groups"] if group["share_group_id"] == group_x)
    assert group_x_data["total_records"] >= 1


def test_global_memory_status(tmp_path: Path, monkeypatch):
    """全局治理入口:get_global_memory_status + 跨 group 重复检测。"""
    group_a = "g1"
    group_b = "g2"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, group_a, ["agent-a", "agent-a-peer"])
    _bind_v2_group(tmp_path, group_b, ["agent-b", "agent-b-peer"])

    same_content = "完全相同的跨 group 重复内容"
    first = _mcp_write(monkeypatch, tmp_path, "agent-a", group_a, "memory-global-a", same_content)
    second = _mcp_write(monkeypatch, tmp_path, "agent-b", group_b, "memory-global-b", same_content)
    assert first["atom"]["share_group_id"] == group_a
    assert second["atom"]["share_group_id"] == group_b
    assert first["atom"]["memory_id"] != second["atom"]["memory_id"]

    result = _gui_api(tmp_path, "agent-a").get_global_memory_status()
    assert result["ok"] is True, result
    data = result["data"]
    assert data["total_groups"] >= 2
    assert data["total_records"] >= 2
    duplicates = data["cross_group_duplicates"]
    assert duplicates, "should detect cross-group dups"
    exact = next(candidate for candidate in duplicates if candidate["match_type"] == "exact")
    assert exact["share_group_ids"] == [group_a, group_b]
    assert exact["record_count"] == 2
    assert exact["canonical_hash"]
    assert "body" not in exact
    assert all("body" not in record for record in exact["records"])


def test_binding_uniqueness(tmp_path: Path):
    """V2 binding 唯一性:新绑定自动 deactivate 旧绑定。"""
    _activate_v2_workspace(tmp_path)
    # V2 GUI mutations require a trusted, binding-backed caller identity.
    # Bootstrap that identity through the real V2 service with a dummy peer;
    # the actual bind and rebind assertions remain on the public GUI API.
    _bind_v2_group(tmp_path, "binding-bootstrap", ["agent-a", "agent-a-peer"])
    api = _gui_api(tmp_path, "agent-a")

    first = api.bind_agent("agent-a", "group-1")
    assert first["ok"] is True, first
    assert first["data"]["share_group_id"] == "group-1"
    first_binding_id = first["data"]["binding_id"]

    second = api.bind_agent("agent-a", "group-2")
    assert second["ok"] is True, second
    assert second["data"]["share_group_id"] == "group-2"
    second_binding_id = second["data"]["binding_id"]
    assert second_binding_id != first_binding_id

    service = GroupControlService(tmp_path, write=False)
    active = [
        binding
        for binding in service.list_bindings(include_inactive=False)["bindings"]
        if binding["agent_instance_id"] == "agent-a"
    ]
    assert len(active) == 1, "should only have 1 active binding"
    assert active[0]["binding_id"] == second_binding_id
    assert active[0]["share_group_id"] == "group-2"
    history = [
        binding
        for binding in service.list_bindings(include_inactive=True)["bindings"]
        if binding["agent_instance_id"] == "agent-a"
    ]
    assert len(history) == 3
    assert any(
        binding["binding_id"] == first_binding_id
        and binding["status"] == "inactive"
        and binding["share_group_id"] == "group-1"
        for binding in history
    )
    assert any(
        binding["binding_id"] == second_binding_id
        and binding["status"] == "active"
        and binding["share_group_id"] == "group-2"
        for binding in history
    )


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


def test_shared_group_governance_commit_and_decide(tmp_path: Path, monkeypatch):
    """共享组：导入/提交治理 + 图上 exclude 走 V2 公共入口。"""
    gid = "gov-group-1"
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, gid, ["agent-a", "agent-b"])
    written = _mcp_write(monkeypatch, tmp_path, "agent-a", gid, "memory-governance", "共享治理测试记忆")
    memory_id = written["atom"]["memory_id"]
    api = _gui_api(tmp_path, "agent-a")

    import_result = api.import_native_memories_to_group(gid, confirmed=True)
    assert import_result["ok"] is True, import_result
    assert import_result["data"]["share_group_id"] == gid
    assert import_result["data"]["records_written"] == 0

    commit_result = api.commit_shared_memory_governance(gid, "test takeover", confirmed=True)
    assert commit_result["ok"] is True, commit_result
    assert commit_result["data"]["share_group_id"] == gid
    assert commit_result["data"]["version_id"].startswith("governance-")

    decide = api.neuron_decide(
        memory_id,
        "exclude",
        "test delete",
        confirmed=True,
        scope={"mode": "share_group", "share_group_id": gid},
    )
    assert decide["ok"] is True, decide
    assert decide["data"]["memory_id"] == memory_id
    assert decide["data"]["memory_status"] == "rejected"

    memory = MemoryAtomStore(tmp_path)
    scope = MemoryReadScope(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=gid,
        admin=True,
    )
    record_after = memory.get_atom(memory_id, scope=scope, include_building=True)
    assert record_after is not None
    assert record_after.status == "rejected"
    assert memory.list_atoms(scope=scope, status="active") == []


def test_bind_agents_default_redirected(tmp_path: Path):
    """默认共享组使用 V2 正式 shared-* ID，并将 native memory 重定向。"""
    _activate_v2_workspace(tmp_path)
    _bind_v2_group(tmp_path, "default-bootstrap", ["agent-a", "agent-a-peer"])
    result = _gui_api(tmp_path, "agent-a").bind_agents_to_shared_group(["agent-a", "agent-b"])
    assert result["ok"] is True, result
    data = result["data"]
    group_id = data["share_group_id"]
    assert group_id.startswith("shared-")
    assert data["member_count"] == 2

    bindings = GroupControlService(tmp_path, write=False).list_bindings(include_inactive=False)["bindings"]
    group_bindings = [binding for binding in bindings if binding["share_group_id"] == group_id]
    assert len(group_bindings) == 2
    assert all(binding["native_memory_mode"] == "redirected" for binding in group_bindings)


def test_global_mcp_workspace_is_independent_from_current_project(
    tmp_path: Path, monkeypatch,
):
    """全局 MCP 从稳定控制目录读绑定/记忆，不能随 Agent 当前项目漂移。"""
    control_workspace = tmp_path / "memoryguard-control"
    unrelated_project = tmp_path / "another-project"
    control_workspace.mkdir()
    unrelated_project.mkdir()
    agent_id = "codex-global-test"
    group_id = "global-memory-group"
    _activate_v2_workspace(control_workspace)
    _bind_v2_group(control_workspace, group_id, [agent_id, "global-memory-peer"])
    _mcp_write(
        monkeypatch,
        control_workspace,
        agent_id,
        group_id,
        "memory-global-workspace",
        "global workspace memory",
    )

    monkeypatch.chdir(unrelated_project)
    _configure_mcp_identity(monkeypatch, control_workspace, agent_id)
    monkeypatch.setenv("MEMORYGUARD_PROJECT_CWD", str(unrelated_project.resolve()))

    result = execute_tool(
        "memoryguard_memory_status",
        {"workspace": str(unrelated_project)},
    )
    data = _mcp_data(result)
    assert data["scope"]["share_group_id"] == group_id
    assert data["total_records"] == 1
    assert WorkspaceV2Layout(control_workspace).memory_db.is_file()
    assert not WorkspaceV2Layout(unrelated_project).memory_db.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
