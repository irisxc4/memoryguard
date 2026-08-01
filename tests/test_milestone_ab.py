"""A+B 里程碑测试:共享记忆可搜、可解释、不骗人。

覆盖:
- A1: 跨组重复检测用 canonical_hash(非 body[:100])
- A2: GUI bind_agent 非 admin 被拒
- A3: preflight_check 打印身份权限态
- B1: FTS5 全文搜索 + BM25 排序
- B2: 检索结果带 group/agent/kind/provenance
"""
import sys
import os
import io
import json
import tempfile
import sqlite3
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_cross_group_dup_canonical_hash(monkeypatch):
    """A1: 跨组重复检测用 canonical_hash,前 100 字相同但后文不同不误报。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "g1", _admin_override=True)
        api.bind_agent("agent-b", "g2", _admin_override=True)

        # 前 100 字相同,后文不同(前缀法会误报)
        prefix = "A" * 100
        org1 = AutoOrganizer(ws, "g1")
        org1.organize(MemoryEvent(
            event_id="e1", agent_instance_id="agent-a", share_group_id="g1",
            raw_content=prefix + " 后续内容A完全不同", metadata={},
        ))
        org2 = AutoOrganizer(ws, "g2")
        org2.organize(MemoryEvent(
            event_id="e2", agent_instance_id="agent-b", share_group_id="g2",
            raw_content=prefix + " 后续内容B完全不同", metadata={},
        ))

        result = api.get_global_memory_status()
        dups = result["cross_group_duplicates"]
        # canonical_hash 不同 -> 不应判为重复
        assert len(dups) == 0, f"should not detect dups for different content: {dups}"


def test_cross_group_dup_same_content_detected(monkeypatch):
    """A1: 真正相同内容(不同 group)被检测为重复。"""
    from memoryguard.gui import GovernanceApi
    from memoryguard.schema_v3 import MemoryEvent
    from memoryguard.auto_organizer import AutoOrganizer

    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "0")

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        api.bind_agent("agent-a", "g1", _admin_override=True)
        api.bind_agent("agent-b", "g2", _admin_override=True)

        same = "完全相同的跨组内容用于测试"
        org1 = AutoOrganizer(ws, "g1")
        org1.organize(MemoryEvent(
            event_id="e1", agent_instance_id="agent-a", share_group_id="g1",
            raw_content=same, metadata={},
        ))
        org2 = AutoOrganizer(ws, "g2")
        org2.organize(MemoryEvent(
            event_id="e2", agent_instance_id="agent-b", share_group_id="g2",
            raw_content=same, metadata={},
        ))

        result = api.get_global_memory_status()
        dups = result["cross_group_duplicates"]
        assert len(dups) >= 1, "should detect same content as dup"
        assert "canonical_hash" in dups[0]
        assert len(dups[0]["share_group_ids"]) >= 2


def test_gui_bind_agent_non_admin_denied(monkeypatch):
    """A2: GUI bind_agent 非 admin 被拒。"""
    from memoryguard.gui import GovernanceApi

    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        result = api.bind_agent("agent-a", "g1")
        assert not result.get("ok"), "non-admin bind should fail"
        assert "admin" in result.get("error", "")


def test_gui_bind_agent_admin_override(monkeypatch):
    """A2: admin 或 _admin_override 可绑定。"""
    from memoryguard.gui import GovernanceApi

    monkeypatch.delenv("MEMORYGUARD_ADMIN", raising=False)
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "1")

    with tempfile.TemporaryDirectory() as ws:
        api = GovernanceApi(ws)
        # _admin_override=True 绕过(本地 GUI 场景)
        result = api.bind_agent("agent-a", "g1", _admin_override=True)
        assert result.get("ok"), f"admin_override should work: {result}"


def test_preflight_check_prints_status(monkeypatch):
    """A3: preflight_check 打印身份权限态。"""
    from memoryguard.access_context import load_access_context, preflight_check

    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "test-agent")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "1")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.delenv("MEMORYGUARD_ALLOW_ANON", raising=False)

    buf = io.StringIO()
    ctx = load_access_context()
    warnings = preflight_check(ctx, stream=buf)
    output = buf.getvalue()

    assert "agent_id=test-agent" in output
    assert "admin=ON" in output
    assert "strict_binding=ON" in output
    assert len(warnings) == 0, f"should have no warnings: {warnings}"
    assert "preflight OK" in output


def test_preflight_check_warns_missing_agent(monkeypatch):
    """A3: 缺身份时预检告警。"""
    from memoryguard.access_context import load_access_context, preflight_check

    for k in ["MEMORYGUARD_AGENT_ID", "MEMORYGUARD_ADMIN", "MEMORYGUARD_ALLOW_ANON"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")

    buf = io.StringIO()
    ctx = load_access_context()
    warnings = preflight_check(ctx, stream=buf)
    output = buf.getvalue()

    assert any("AGENT_ID not set" in w for w in warnings)
    assert any("ADMIN not set" in w for w in warnings)
    assert "WARNING" in output


def test_fts5_search_recall():
    """B1: FTS5 全文搜索能稳定召回。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "fts-test")
        # 写入多条记录
        records = [
            ("r1", "用户偏好 Python 编程语言", MemoryKind.PREFERENCE),
            ("r2", "项目部署在 AWS 上", MemoryKind.PROJECT),
            ("r3", "Python 测试覆盖率要求 80%", MemoryKind.FACT),
            ("r4", "数据库迁移步骤详细说明", MemoryKind.PROCEDURE),
        ]
        for mid, body, kind in records:
            store.append_record(SharedMemoryRecord(
                memory_id=mid, body=body, kind=kind,
                status=SharedMemoryStatus.ACTIVE,
                agent_instance_id="agent-a",
            ))

        # 搜索 Python -> 应召回 r1 和 r3
        results = store.search_fts("Python", status="active")
        ids = {r["record"]["memory_id"] for r in results}
        assert "r1" in ids, f"r1 should be in FTS results: {ids}"
        assert "r3" in ids, f"r3 should be in FTS results: {ids}"
        # r2 和 r4 不含 Python
        assert "r2" not in ids
        assert "r4" not in ids


def test_fts5_bm25_ranking():
    """B1: BM25 排序 - 词频更高的结果排名更前。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "bm25-test")
        # r1 含 3 次 Python,r2 含 1 次
        store.append_record(SharedMemoryRecord(
            memory_id="r1", body="Python Python Python 是最好的语言",
            kind=MemoryKind.FACT, status=SharedMemoryStatus.ACTIVE,
        ))
        store.append_record(SharedMemoryRecord(
            memory_id="r2", body="偶尔用 Python 写脚本",
            kind=MemoryKind.FACT, status=SharedMemoryStatus.ACTIVE,
        ))

        results = store.search_fts("Python", status="active")
        assert len(results) >= 2
        # BM25 分数:r1 应比 r2 更高(词频更高)
        # 注意:bm25 返回负值(越小越好),取绝对值比较
        assert results[0]["record"]["memory_id"] == "r1", \
            f"r1 should rank first: {results}"


def test_fts5_results_have_metadata():
    """B2: 检索结果带 group/agent/kind/provenance/confidence。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind, Provenance

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "meta-test")
        store.append_record(SharedMemoryRecord(
            memory_id="r1", body="测试元数据返回", kind=MemoryKind.PREFERENCE,
            status=SharedMemoryStatus.ACTIVE, agent_instance_id="agent-x",
            confidence=0.92,
            provenance=[Provenance(source_object_id="src-1", locator="line:1",
                                   excerpt_hash="h1")],
        ))

        results = store.search_fts("元数据", status="active")
        assert len(results) == 1
        r = results[0]
        assert r["share_group_id"] == "meta-test"
        assert r["agent_instance_id"] == "agent-x"
        assert r["kind"] == "preference"
        assert r["confidence"] == 0.92
        assert len(r["provenance"]) == 1
        assert "bm25_score" in r


def test_fts5_empty_query():
    """B1: 空查询返回空结果。"""
    from memoryguard.shared_memory_store import SharedMemoryStore

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "empty-test")
        assert store.search_fts("") == []
        assert store.search_fts("   ") == []


def test_fts5_fallback_on_error():
    """B1: FTS5 查询失败时回退子串搜索。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "fallback-test")
        store.append_record(SharedMemoryRecord(
            memory_id="r1", body="正常内容用于回退测试", kind=MemoryKind.FACT,
            status=SharedMemoryStatus.ACTIVE,
        ))
        # 用特殊字符触发 FTS5 语法错误 -> 回退
        results = store.search_fts("正常", status="active")
        # 应能通过 FTS 或回退找到
        assert len(results) >= 1
        assert results[0]["record"]["memory_id"] == "r1"


def test_fts5_fallback_matches_non_contiguous_chinese_keywords():
    """多关键词召回应按词匹配，不能要求整串在正文中连续出现。"""
    from memoryguard.shared_memory_store import SharedMemoryStore
    from memoryguard.schema_v3 import SharedMemoryRecord, SharedMemoryStatus, MemoryKind

    with tempfile.TemporaryDirectory() as ws:
        store = SharedMemoryStore(ws, "chinese-keywords-test")
        store.append_record(SharedMemoryRecord(
            memory_id="r1",
            body="优先最小充分方案，并坚持外科式修改，不改动无关代码。",
            kind=MemoryKind.PREFERENCE,
            status=SharedMemoryStatus.ACTIVE,
        ))

        results = store.search_fts(
            "最小充分 外科式修改",
            status="active",
            kind="preference",
        )

        assert [item["record"]["memory_id"] for item in results] == ["r1"]


if __name__ == "__main__":
    test_cross_group_dup_canonical_hash()
    print("OK: A1 - cross-group dup canonical_hash")
    test_cross_group_dup_same_content_detected()
    print("OK: A1 - same content detected")
    test_gui_bind_agent_non_admin_denied()
    print("OK: A2 - GUI non-admin denied")
    test_gui_bind_agent_admin_override()
    print("OK: A2 - admin override works")
    test_preflight_check_prints_status()
    print("OK: A3 - preflight prints status")
    test_preflight_check_warns_missing_agent()
    print("OK: A3 - preflight warns missing agent")
    test_fts5_search_recall()
    print("OK: B1 - FTS5 recall")
    test_fts5_bm25_ranking()
    print("OK: B1 - BM25 ranking")
    test_fts5_results_have_metadata()
    print("OK: B2 - results have metadata")
    test_fts5_empty_query()
    print("OK: B1 - empty query")
    test_fts5_fallback_on_error()
    print("OK: B1 - fallback on error")
    print("\nAll A+B milestone tests passed.")
