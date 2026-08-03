"""GUI 权限+只读+脱敏对齐验证。"""
import sys
import os
import json
import tempfile
import sqlite3
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
@pytest.fixture(autouse=True)
def _isolated_test_env(monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")


def test_gui_edit_memory_secret_redacted():
    """GUI edit_memory 脱敏:secret 不入持久层。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        # 先写入一条正常记忆
        store = SharedMemoryStore(ws, "edit-secret-group")
        store.append_record(SharedMemoryRecord(
            memory_id="r1", body="正常内容", kind=MemoryKind.FACT,
            status=SharedMemoryStatus.ACTIVE,
        ))
        # 用 edit_memory 更新为含 secret 的内容
        secret_body = "api_key=sk-gui-edit-test123def456ghi789"
        result = api.edit_memory("r1", secret_body, "edit-secret-group")
        # 硬断言:SQLite 不含原始 secret
        db_path = Path(ws) / ".memoryguard" / "shared-memory" / "edit-secret-group" / "memory.db"
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT body FROM records").fetchall()
        for row in rows:
            assert "sk-gui-edit-test123" not in str(row), \
                f"secret found in records after edit: {row}"
        conn.close()


def test_gui_write_ops_require_admin():
    """GUI 写操作非 admin 被拒。"""
    from memoryguard.gui import GovernanceApi

    old_env = dict(os.environ)
    try:
        os.environ.pop("MEMORYGUARD_ADMIN", None)
        with tempfile.TemporaryDirectory() as ws:
            api = GovernanceApi(ws)
            # edit_memory
            r = api.edit_memory("r1", "body", "g1")
            assert "error" in r and "admin" in r["error"]
            # delete_memory
            r = api.delete_memory("r1", "g1")
            assert "error" in r and "admin" in r["error"]
            # lock_memory
            r = api.lock_memory("r1", "g1")
            assert "error" in r and "admin" in r["error"]
            # rollback_memory
            r = api.rollback_memory("v1", "g1")
            assert "error" in r and "admin" in r["error"]
            # resolve_conflict
            r = api.resolve_conflict("c1", "r1", "g1")
            assert "error" in r and "admin" in r["error"]
            # release_quarantine
            r = api.release_quarantine("q1", "g1")
            assert "error" in r and "admin" in r["error"]
            # delete_quarantine
            r = api.delete_quarantine("q1", "g1")
            assert "error" in r and "admin" in r["error"]
            # unbind_agent
            r = api.unbind_agent("b1")
            assert "error" in r and "admin" in r["error"]
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_gui_write_ops_rejects_admin_override_forgery():
    """GUI 写操作不能用请求参数伪造 admin capability。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    old_env = dict(os.environ)
    try:
        os.environ.pop("MEMORYGUARD_ADMIN", None)
        with tempfile.TemporaryDirectory() as ws:
            api = GovernanceApi(ws)
            store = SharedMemoryStore(ws, "override-group")
            store.append_record(SharedMemoryRecord(
                memory_id="r1", body="test", kind=MemoryKind.FACT,
                status=SharedMemoryStatus.ACTIVE,
            ))
            # A browser/local caller cannot turn the legacy keyword into admin.
            r = api.lock_memory("r1", "override-group", _admin_override=True)
            assert r == {
                "ok": False,
                "error": "admin capability required (set MEMORYGUARD_ADMIN=1)",
            }
            assert store.get_record("r1").status == SharedMemoryStatus.ACTIVE
    finally:
        os.environ.clear()
        os.environ.update(old_env)


def test_gui_readonly_no_side_effects():
    """GUI 只读操作不创建空 group。"""
    from memoryguard.gui import GovernanceApi

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        # 查询不存在的 group
        result = api.list_memory(share_group_id="nonexistent-readonly")
        assert "error" in result or result.get("total", 0) == 0
        # 硬断言:目录没被创建
        p = Path(ws) / ".memoryguard" / "shared-memory" / "nonexistent-readonly"
        assert not p.exists(), "readonly list_memory should not create group dir"

        result = api.get_memory_status("nonexistent-readonly-2")
        assert "error" in result
        p2 = Path(ws) / ".memoryguard" / "shared-memory" / "nonexistent-readonly-2"
        assert not p2.exists(), "readonly get_memory_status should not create group dir"

        result = api.get_governance_snapshot("nonexistent-readonly-3")
        assert "error" in result
        p3 = Path(ws) / ".memoryguard" / "shared-memory" / "nonexistent-readonly-3"
        assert not p3.exists()


def test_gui_search_memory_readonly():
    """GUI search_memory 用 read_only,不存在返回 error。"""
    from memoryguard.gui import GovernanceApi

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        result = api.search_memory("test", share_group_id="nonexistent-search")
        assert "error" in result or result.get("total") == 0
        p = Path(ws) / ".memoryguard" / "shared-memory" / "nonexistent-search"
        assert not p.exists()


if __name__ == "__main__":
    test_gui_edit_memory_secret_redacted()
    print("OK: GUI edit_memory secret redacted")
    test_gui_write_ops_require_admin()
    print("OK: GUI write ops require admin")
    test_gui_write_ops_rejects_admin_override_forgery()
    print("OK: GUI rejects forged admin override")
    test_gui_readonly_no_side_effects()
    print("OK: GUI readonly no side effects")
    test_gui_search_memory_readonly()
    print("OK: GUI search_memory readonly")
    print("\nAll GUI alignment tests passed.")
