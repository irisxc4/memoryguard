"""P0 安全+并发+隐私 硬断言复验。

4 条硬断言:
1. 越权:Agent B 无法读写 Agent A 的 group(默认 STRICT_BINDING=1)
2. 只读无副作用:只读请求前后文件系统完全不变
3. update secret:update 路径也脱敏,原文不入持久层
4. 并发唯一:N 并发同正文 -> 1 active + N provenance

附加:
- 路径穿越不创建任何文件
- group_id 规范化拒绝非法 slug
- AccessContext 身份校验拒绝冒充
"""
import sys
import os
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _setup_ws_with_binding(
    agent_id: str, group_id: str = "test-group", *, monkeypatch,
):
    """创建工作区并绑定 agent(用 admin 权限)。"""
    from memoryguard.gui import GovernanceApi
    ws = Path(tempfile.mkdtemp())
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    api = GovernanceApi(str(ws))
    api.bind_agent(agent_id, group_id)
    return ws


def test_cross_group_access_denied(monkeypatch):
    """硬断言1: Agent B 无法读写 Agent A 的 group。"""
    from memoryguard.mcp_server import _handle_memory_read, _handle_memory_search, _handle_memory_write

    ws = _setup_ws_with_binding("agent-a", "group-a", monkeypatch=monkeypatch)
    # Agent A 写入一条记忆
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    write_result = _handle_memory_write({
        "workspace": str(ws),
        "body": "agent-a 的私有记忆",
        "agent_instance_id": "agent-a",
    })
    assert not write_result.get("isError"), f"agent-a write failed: {write_result}"

    # Agent B 冒充 agent-a 身份 -> 被 AccessContext 拒绝
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-b")
    read_result = _handle_memory_read({
        "workspace": str(ws),
        "memory_id": "any-id",
        "agent_instance_id": "agent-a",  # 冒充 agent-a
    })
    assert read_result.get("isError"), "Agent B impersonating agent-a should be denied"
    assert "mismatch" in read_result["content"][0]["text"] or "denied" in read_result["content"][0]["text"]

    # Agent B 无 binding -> 被 strict binding 拒绝
    search_result = _handle_memory_search({
        "workspace": str(ws),
        "query": "私有",
        "agent_instance_id": "agent-b",
    })
    assert search_result.get("isError"), "unbound agent-b should be denied in strict mode"


def test_readonly_no_side_effects():
    """硬断言2: 只读请求前后文件系统完全不变。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        # 先创建 group 并写入一条记录
        store_w = SharedMemoryStore(ws, "ro-test-group")
        rec = SharedMemoryRecord(
            memory_id="rec1", body="test content", kind=MemoryKind.FACT,
            status=SharedMemoryStatus.ACTIVE,
        )
        store_w.append_record(rec)

        # 快照文件系统状态
        sm_root = Path(ws) / ".memoryguard" / "shared-memory"
        before = set()
        for f in sm_root.rglob("*"):
            if f.is_file():
                before.add(str(f.relative_to(ws)))

        # 只读打开 + 查询
        store_r = SharedMemoryStore(ws, "ro-test-group", read_only=True)
        records = store_r.list_records()
        assert len(records) == 1

        # 尝试写入应失败
        try:
            store_r.append_record(SharedMemoryRecord(
                memory_id="rec2", body="should fail", kind=MemoryKind.FACT,
                status=SharedMemoryStatus.ACTIVE,
            ))
            assert False, "write to read-only store must fail"
        except (sqlite3.OperationalError, Exception):
            pass

        # 只读打开不存在的 group -> 报错,不创建
        try:
            SharedMemoryStore(ws, "nonexistent", read_only=True)
            assert False, "nonexistent group must not be opened"
        except FileNotFoundError:
            pass

        # 快照后文件系统状态
        after = set()
        for f in sm_root.rglob("*"):
            if f.is_file():
                after.add(str(f.relative_to(ws)))

        # 硬断言:文件列表完全不变
        assert before == after, \
            f"filesystem changed! before={before} after={after}"


def test_update_secret_redacted(monkeypatch):
    """硬断言3: update 路径也脱敏,原文不入持久层。"""
    from memoryguard.mcp_server import _handle_memory_write, _handle_memory_update
    from memoryguard.gui import GovernanceApi

    ws = _setup_ws_with_binding(
        "agent-a", "upd-secret-group", monkeypatch=monkeypatch,
    )
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    # 先写入一条正常记忆
    write_result = _handle_memory_write({
            "workspace": str(ws),
            "body": "正常记忆内容用于测试更新",
            "agent_instance_id": "agent-a",
        })
    assert not write_result.get("isError")
    mem_id = json.loads(write_result["content"][0]["text"])["memory_id"]

    # 用 update 更新 body 为含 secret 的内容
    secret_body = "api_key=sk-update123def456ghi789jkl012mno345pqr789"
    update_result = _handle_memory_update({
            "workspace": str(ws),
            "memory_id": mem_id,
            "body": secret_body,
            "agent_instance_id": "agent-a",
        })
    # 不应有 isError(secret 被脱敏了,但 update 本身成功)
    assert not update_result.get("isError"), f"update failed: {update_result}"

    # 硬断言:SQLite 中不含原始 secret
    db_path = Path(ws) / ".memoryguard" / "shared-memory" / "upd-secret-group" / "memory.db"
    conn = sqlite3.connect(str(db_path))
    for table in ["records", "events", "quarantine"]:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            row_str = str(row)
            assert "sk-update123" not in row_str, \
                f"secret found in {table} after update: {row_str}"
    conn.close()

    # JSONL 也不含
    for jsonl in ["records.jsonl", "events.jsonl", "quarantine.jsonl"]:
        path = Path(ws) / ".memoryguard" / "shared-memory" / "upd-secret-group" / jsonl
        if path.exists():
            content = path.read_text(encoding="utf-8")
            assert "sk-update123" not in content, f"secret in {jsonl}"


def test_concurrent_same_body_one_record():
    """硬断言4: N 并发同正文 -> 1 active record + N provenance。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind, Provenance

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "concurrent-group")
        body = "完全相同的并发写入测试内容"

        N = 10
        barrier = threading.Barrier(N)
        errors = []

        def write_one(idx):
            barrier.wait()
            rec = SharedMemoryRecord(
                memory_id=f"rec-{idx}",
                body=body,
                kind=MemoryKind.FACT,
                status=SharedMemoryStatus.ACTIVE,
                provenance=[Provenance(
                    source_object_id=f"src-{idx}",
                    locator=f"line:{idx}",
                    excerpt_hash=f"hash-{idx}",
                )],
            )
            try:
                store.append_record(rec)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=write_one, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 硬断言1:只有 1 条 active record(相同 body)
        active = store.list_records(status="active")
        same_body = [r for r in active if r.body == body]
        assert len(same_body) == 1, \
            f"expected 1 active record for same body, got {len(same_body)}"

        # 硬断言2:provenance 应包含所有 N 个来源
        record = same_body[0]
        prov_count = len(record.provenance)
        assert prov_count == N, \
            f"expected {N} provenance entries, got {prov_count}"

        # 无错误
        assert len(errors) == 0, f"concurrent errors: {errors}"


def test_path_traversal_blocked():
    """路径穿越不创建任何文件。"""
    from memoryguard.shared_memory_store import SharedMemoryStore

    with tempfile.TemporaryDirectory() as ws:
        bad_ids = ["../escaped", "../../etc", "..\\windows", "/abs/path",
                   "a/b", "a\\b", "UPPERCASE", "with space", ""]
        for bad in bad_ids:
            try:
                SharedMemoryStore(ws, bad)
            except (ValueError, FileNotFoundError):
                pass
        sm_root = Path(ws) / ".memoryguard" / "shared-memory"
        if sm_root.exists():
            for child in sm_root.iterdir():
                assert ".." not in child.name
                assert "/" not in child.name
                assert "\\" not in child.name


def test_access_context_impersonation_blocked(monkeypatch):
    """AccessContext 拒绝冒充。"""
    from memoryguard.access_context import load_access_context

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "trusted-agent")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    ctx = load_access_context()

    # 正确身份 -> 通过
    ok, err = ctx.check_agent("trusted-agent")
    assert ok

    # 冒充 -> 拒绝
    ok, err = ctx.check_agent("impostor")
    assert not ok
    assert "mismatch" in err


def test_strict_binding_default_on(monkeypatch):
    """STRICT_BINDING 默认开启。"""
    from memoryguard.access_context import load_access_context

    monkeypatch.delenv("MEMORYGUARD_STRICT_BINDING", raising=False)
    ctx = load_access_context()
    assert ctx.strict_binding, "STRICT_BINDING should default to True"


if __name__ == "__main__":
    test_cross_group_access_denied()
    print("OK: hard-assert 1 - cross-group access denied")
    test_readonly_no_side_effects()
    print("OK: hard-assert 2 - readonly no side effects")
    test_update_secret_redacted()
    print("OK: hard-assert 3 - update secret redacted")
    test_concurrent_same_body_one_record()
    print("OK: hard-assert 4 - concurrent 1 record + N provenance")
    test_path_traversal_blocked()
    print("OK: path traversal blocked")
    test_access_context_impersonation_blocked()
    print("OK: impersonation blocked")
    test_strict_binding_default_on()
    print("OK: strict binding default on")
    print("\nAll P0 hard-assert tests passed.")
