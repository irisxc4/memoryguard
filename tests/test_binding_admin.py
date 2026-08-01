"""binding_create admin 校验 + 匿名拒绝 补测。"""
import sys
import json
import tempfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_non_admin_binding_create_denied(monkeypatch):
    """无 admin 创建 binding 必须失败。"""
    from memoryguard.mcp_server import execute_tool

    monkeypatch.setenv("MEMORYGUARD_ADMIN", "")  # 非 admin
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "attacker")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    with tempfile.TemporaryDirectory() as ws:
        result = execute_tool("memoryguard_binding_create", {
            "workspace": ws,
            "agent_instance_id": "attacker",
            "share_group_id": "group-a",
        })
        assert result.get("isError"), "non-admin binding_create must fail"
        assert "admin" in result["content"][0]["text"]


def test_self_bind_then_read_still_denied(monkeypatch):
    """自助 bind 后仍不能读目标 group(因为 bind 本身被拒)。"""
    from memoryguard.mcp_server import execute_tool

    monkeypatch.setenv("MEMORYGUARD_ADMIN", "")  # 非 admin
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "attacker")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    with tempfile.TemporaryDirectory() as ws:
        # 先用 admin 创建 group-a 并写入(模拟合法场景)
        monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
        monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "victim")
        execute_tool("memoryguard_binding_create", {
            "workspace": ws,
            "agent_instance_id": "victim",
            "share_group_id": "group-a",
        })
        execute_tool("memoryguard_memory_write", {
            "workspace": ws,
            "body": "victim 私有记忆",
            "agent_instance_id": "victim",
        })

        # 攻击者切换身份,非 admin 尝试 self-bind
        monkeypatch.setenv("MEMORYGUARD_ADMIN", "")
        monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "attacker")
        bind_result = execute_tool("memoryguard_binding_create", {
            "workspace": ws,
            "agent_instance_id": "attacker",
            "share_group_id": "group-a",
        })
        assert bind_result.get("isError"), "attacker self-bind must be denied"

        # 即使 bind 失败,尝试读 group-a 也应被拒(无 binding)
        read_result = execute_tool("memoryguard_memory_read", {
            "workspace": ws,
            "memory_id": "any",
            "agent_instance_id": "attacker",
        })
        assert read_result.get("isError"), "attacker read must be denied"


def test_no_agent_id_denied(monkeypatch):
    """未设置 MEMORYGUARD_AGENT_ID 时读写被拒。"""
    from memoryguard.access_context import load_access_context

    # 确保关键环境变量未设置
    for key in ["MEMORYGUARD_AGENT_ID", "MEMORYGUARD_ALLOW_ANON", "MEMORYGUARD_ADMIN"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    ctx = load_access_context()
    ok, err = ctx.check_agent("anyone")
    assert not ok, "should deny when MEMORYGUARD_AGENT_ID not set"
    assert "anonymous" in err or "not set" in err


def test_allow_anon_explicit(monkeypatch):
    """MEMORYGUARD_ALLOW_ANON=1 时允许匿名(显式放开兼容)。"""
    from memoryguard.access_context import load_access_context

    monkeypatch.delenv("MEMORYGUARD_AGENT_ID", raising=False)
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    ctx = load_access_context()
    ok, err = ctx.check_agent("anyone")
    assert ok, f"allow_anon should permit: {err}"


if __name__ == "__main__":
    test_non_admin_binding_create_denied()
    print("OK: non-admin binding_create denied")
    test_self_bind_then_read_still_denied()
    print("OK: self-bind then read still denied")
    test_no_agent_id_denied()
    print("OK: no agent_id denied")
    test_allow_anon_explicit()
    print("OK: allow_anon explicit")
    print("\nAll binding admin tests passed.")
