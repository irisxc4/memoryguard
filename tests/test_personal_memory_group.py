"""个人组/共享组统一记忆层契约。"""
import json
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest

from memoryguard.agent_binding import (
    AgentBindingStore,
    is_personal_group_id,
    personal_group_id,
)
from memoryguard.gui import GovernanceApi
from memoryguard.mcp_server import execute_tool
from memoryguard.memory_ir import MemoryIR, MemoryNormalizer
from memoryguard.shared_memory_store import SharedMemoryStore
from memoryguard.schema_v3 import (
    MemoryKind,
    Provenance,
    SharedMemoryRecord,
    SharedMemoryStatus,
    SourceRootType,
)
from memoryguard.source_registry import SourceRegistry


def test_personal_group_id_is_stable_and_safe(tmp_path: Path):
    gid = personal_group_id(r"unsafe/agent\\id")
    assert gid == personal_group_id(r"unsafe/agent\\id")
    assert is_personal_group_id(gid)
    assert "/" not in gid and "\\" not in gid and len(gid) <= 64


def test_ensure_personal_is_idempotent_and_preserves_shared_binding(tmp_path: Path):
    store = AgentBindingStore(tmp_path)
    first = store.ensure_personal_memory_group("agent-a")
    again = store.ensure_personal_memory_group("agent-a")
    assert first["created"] is True
    assert again["created"] is False and again["changed"] is False
    store.bind_agent("agent-b", "shared-team")
    # ensure 不会把已有共享绑定静默拉回个人组
    shared = store.bind_agent("agent-a", "shared-team")
    preserved = store.ensure_personal_memory_group("agent-a")
    assert preserved["group_id"] == "shared-team"
    assert store.get_binding(shared.binding_id).status.value == "active"


def test_leave_shared_rejects_unbound_agent(tmp_path: Path):
    result = AgentBindingStore(tmp_path).leave_shared_group_to_personal(
        "agent-a", confirmed=True,
    )
    assert result == {"ok": False, "error": "agent_not_bound_to_shared_group"}


def test_leave_shared_returns_to_same_personal_group_without_merge(tmp_path: Path):
    store = AgentBindingStore(tmp_path)
    personal = store.ensure_personal_memory_group("agent-a")["group_id"]
    store.bind_agent("agent-a", "shared-team")
    store.bind_agent("agent-b", "shared-team")
    result = store.leave_shared_group_to_personal("agent-a", confirmed=True)
    assert result["group_id"] == personal
    assert store.find_by_group("shared-team", include_inactive=False)[0].agent_instance_id == "agent-b"
    assert store.find_by_group(personal, include_inactive=False)[0].agent_instance_id == "agent-a"


def test_personal_and_shared_groups_use_separate_database_files_and_records(
    tmp_path: Path, monkeypatch,
):
    """个人/共享只复用协议，不复用 DB 文件，也不在切换时搬运记录。"""
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    bindings = AgentBindingStore(tmp_path)
    personal_id = bindings.ensure_personal_memory_group("agent-a")["group_id"]
    personal_write = execute_tool(
        "memoryguard_memory_write",
        {"agent_instance_id": "agent-a", "body": "personal-only marker"},
    )
    assert not personal_write.get("isError")

    bindings.bind_agent("agent-a", "shared-team")
    bindings.bind_agent("agent-b", "shared-team")
    shared_write = execute_tool(
        "memoryguard_memory_write",
        {"agent_instance_id": "agent-a", "body": "shared-only marker"},
    )
    assert not shared_write.get("isError")

    personal_store = SharedMemoryStore(tmp_path, personal_id, read_only=True)
    shared_store = SharedMemoryStore(tmp_path, "shared-team", read_only=True)
    assert personal_store.db_path != shared_store.db_path
    personal_bodies = {record.body for record in personal_store.list_records()}
    shared_bodies = {record.body for record in shared_store.list_records()}
    assert any("personal-only marker" in body for body in personal_bodies)
    assert not any("shared-only marker" in body for body in personal_bodies)
    assert any("shared-only marker" in body for body in shared_bodies)
    assert not any("personal-only marker" in body for body in shared_bodies)

    bindings.leave_shared_group_to_personal("agent-a", confirmed=True)
    assert {record.body for record in personal_store.list_records()} == personal_bodies
    assert {record.body for record in shared_store.list_records()} == shared_bodies


def test_read_only_store_reads_live_wal_without_forcing_clean_db_sidecars(
    tmp_path: Path,
):
    store = SharedMemoryStore(tmp_path, "wal-freshness")
    store.append_record(SharedMemoryRecord(
        memory_id="before-pin",
        body="before pin",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
    ))
    pin = sqlite3.connect(store.db_path)
    try:
        pin.execute("PRAGMA journal_mode=WAL")
        pin.execute("BEGIN")
        pin.execute("SELECT COUNT(*) FROM records").fetchone()
        store.append_record(SharedMemoryRecord(
            memory_id="in-live-wal",
            body="latest committed value",
            kind=MemoryKind.FACT,
            status=SharedMemoryStatus.ACTIVE,
        ))
        wal_path = Path(f"{store.db_path}-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0
        records = SharedMemoryStore(
            tmp_path, "wal-freshness", read_only=True,
        ).list_records()
        assert {item.memory_id for item in records} == {
            "before-pin", "in-live-wal",
        }
    finally:
        pin.close()


def test_read_only_store_tolerates_wal_disappearing_during_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A checkpoint may unlink -wal after the reader has started probing it."""
    store = SharedMemoryStore(tmp_path, "wal-disappears")
    store.append_record(SharedMemoryRecord(
        memory_id="stable-main-db", body="checkpoint-safe", kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
    ))
    wal_path = Path(f"{store.db_path}-wal")
    original_stat = Path.stat

    def disappear_once(path: Path, *args, **kwargs):
        if path == wal_path:
            raise FileNotFoundError(2, "sidecar checkpointed", str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", disappear_once)
    records = SharedMemoryStore(tmp_path, "wal-disappears", read_only=True).list_records()
    assert [record.memory_id for record in records] == ["stable-main-db"]


def test_read_only_store_survives_concurrent_wal_checkpoints(tmp_path: Path):
    """Stress the real read/write seam without asserting timing-sensitive WAL state."""
    store = SharedMemoryStore(tmp_path, "wal-concurrency")
    started = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            for index in range(40):
                store.append_record(SharedMemoryRecord(
                    memory_id=f"concurrent-{index}", body=f"record {index}",
                    kind=MemoryKind.FACT, status=SharedMemoryStatus.ACTIVE,
                ))
                started.set()
        except BaseException as exc:  # test must surface any worker failure
            failures.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    started.wait(timeout=5)
    try:
        while thread.is_alive():
            SharedMemoryStore(tmp_path, "wal-concurrency", read_only=True).list_records()
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not failures
    assert len(SharedMemoryStore(tmp_path, "wal-concurrency", read_only=True).list_records()) == 40


def test_group_listing_exposes_personal_or_shared_kind(tmp_path: Path):
    bindings = AgentBindingStore(tmp_path)
    personal_id = bindings.ensure_personal_memory_group("agent-a")["group_id"]
    bindings.bind_agent("agent-b", "shared-team")

    groups = {
        item["share_group_id"]: item
        for item in GovernanceApi(tmp_path).list_share_groups()["groups"]
    }
    assert groups[personal_id]["group_kind"] == "personal"
    assert groups["shared-team"]["group_kind"] == "shared"


def test_source_map_accepts_real_memory_ir_without_source_objects(
    tmp_path: Path,
):
    """SourceObject 属于快照，不是 MemoryIR 字段；真实 IR 不得触发 API 500。"""
    MemoryNormalizer(tmp_path).save(MemoryIR(snapshot_id="missing-snapshot"))
    SharedMemoryStore(tmp_path, "real-ir-shape").append_record(
        SharedMemoryRecord(
            memory_id="unresolved-source",
            body="memory with historical provenance",
            kind=MemoryKind.FACT,
            status=SharedMemoryStatus.ACTIVE,
            provenance=[
                Provenance(
                    source_object_id="historical-source-object",
                    locator="memory",
                    excerpt_hash="historical",
                ),
            ],
        )
    )
    result = GovernanceApi(tmp_path).get_memory_source_map("real-ir-shape")
    assert result["total_records"] == 1
    assert result["mappings"][0]["sources"][0]["origin_kind"] == "mcp_runtime"


@pytest.mark.parametrize(
    "operation",
    ("export_memory_group", "clear_memory_group", "archive_memory_group"),
)
def test_lifecycle_operation_on_missing_group_does_not_create_ghost_store(
    tmp_path: Path,
    operation: str,
):
    api = GovernanceApi(tmp_path)
    result = getattr(api, operation)(
        "missing-group", confirmed=True, _admin_override=True,
    )
    assert "group not found" in result["error"]
    assert not (
        tmp_path / ".memoryguard" / "shared-memory" / "missing-group"
    ).exists()


def test_export_contains_full_history_and_file_mapping(
    tmp_path: Path, monkeypatch,
):
    native_root = tmp_path / "native"
    native_root.mkdir()
    native_file = native_root / "memory.md"
    native_file.write_text("native source must not be bundled", encoding="utf-8")
    root = SourceRegistry(tmp_path).add(
        str(native_root), SourceRootType.SELECTED_DIRECTORY,
        display_name="Native notes",
    )
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    personal_id = AgentBindingStore(tmp_path).ensure_personal_memory_group(
        "agent-a",
    )["group_id"]
    written = execute_tool("memoryguard_memory_write", {
        "agent_instance_id": "agent-a",
        "body": "mapped memory",
        "metadata": {
            "source_root_id": root.root_id,
            "relative_path": "memory.md",
            "locator": "line:1",
        },
    })
    assert not written.get("isError")
    SharedMemoryStore(tmp_path, personal_id).create_version_snapshot("before export")

    result = GovernanceApi(tmp_path).export_memory_group(
        personal_id, confirmed=True, _admin_override=True,
    )
    export_path = Path(result["export_path"])
    assert export_path.is_file()
    with zipfile.ZipFile(export_path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json", "records.json", "events.json", "decisions.json",
            "conflicts.json", "quarantine.json", "versions.json",
            "bindings.json", "source-map.json",
        }
        manifest = json.loads(archive.read("manifest.json"))
        versions = json.loads(archive.read("versions.json"))
        source_map = json.loads(archive.read("source-map.json"))
        assert manifest["group_kind"] == "personal"
        assert manifest["native_files_included"] is False
        assert versions[0]["snapshot"]["records"]
        source = source_map["mappings"][0]["sources"][0]
        assert source["origin_kind"] == "local_file"
        assert source["absolute_path"] == str(native_file.resolve())
        assert source["exists"] is True
        assert b"native source must not be bundled" not in export_path.read_bytes()


def test_clear_exports_then_empties_only_target_group(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    bindings = AgentBindingStore(tmp_path)
    personal_id = bindings.ensure_personal_memory_group("agent-a")["group_id"]
    execute_tool("memoryguard_memory_write", {
        "agent_instance_id": "agent-a", "body": "remove from personal",
    })
    bindings.bind_agent("agent-a", "shared-team")
    bindings.bind_agent("agent-b", "shared-team")
    execute_tool("memoryguard_memory_write", {
        "agent_instance_id": "agent-a", "body": "keep in shared",
    })
    bindings.leave_shared_group_to_personal("agent-a", confirmed=True)

    result = GovernanceApi(tmp_path).clear_memory_group(
        personal_id, confirmed=True, _admin_override=True,
    )
    assert Path(result["export_path"]).is_file()
    assert result["after"]["total_records"] == 0
    assert result["after"]["total_events"] == 0
    assert result["after"]["total_decisions"] == 0
    assert result["binding_preserved"] is True
    assert bindings.find_by_agent("agent-a", include_inactive=False)[0].share_group_id == personal_id
    assert SharedMemoryStore(tmp_path, "shared-team", read_only=True).status()["total_records"] > 0
    # 重新打开空库也不能被旧 JSONL/版本侧车灌回。
    assert SharedMemoryStore(tmp_path, personal_id).status()["total_records"] == 0


def test_maintenance_window_rejects_mcp_writes(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    group_id = AgentBindingStore(tmp_path).ensure_personal_memory_group(
        "agent-a",
    )["group_id"]
    store = SharedMemoryStore(tmp_path, group_id)
    with store.maintenance("test"):
        result = execute_tool("memoryguard_memory_write", {
            "agent_instance_id": "agent-a",
            "body": "must be rejected during maintenance",
        })
        assert result.get("isError") is True
        assert "maintenance" in result["content"][0]["text"]
    assert not store.maintenance_marker.exists()


def test_archive_exports_unbinds_and_moves_only_target_group(tmp_path: Path):
    bindings = AgentBindingStore(tmp_path)
    personal_id = bindings.ensure_personal_memory_group("agent-a")["group_id"]
    shared_path = SharedMemoryStore(tmp_path, "shared-team").db_path
    result = GovernanceApi(tmp_path).archive_memory_group(
        personal_id, confirmed=True, _admin_override=True,
    )
    assert Path(result["export_path"]).is_file()
    assert Path(result["archived_to"]).is_dir()
    assert not (
        tmp_path / ".memoryguard" / "shared-memory" / personal_id
    ).exists()
    assert shared_path.is_file()
    assert bindings.find_by_agent("agent-a", include_inactive=False) == []


def test_bind_failure_restores_previous_active_binding(tmp_path: Path, monkeypatch):
    store = AgentBindingStore(tmp_path)
    old = store.bind_agent("agent-a", "shared-old")
    original = store._write_binding

    def fail_new(binding):
        if binding.status.value == "active" and binding.share_group_id == "shared-new":
            raise OSError("injected binding write failure")
        return original(binding)

    monkeypatch.setattr(store, "_write_binding", fail_new)
    with pytest.raises(OSError):
        store.bind_agent("agent-a", "shared-new")
    active = store.find_by_agent("agent-a", include_inactive=False)
    assert len(active) == 1 and active[0].binding_id == old.binding_id


def test_multi_agent_bind_failure_rolls_back_every_agent(
    tmp_path: Path, monkeypatch,
):
    """第二个 Agent 写盘失败时，第一个也不能留在新共享组。"""
    store = AgentBindingStore(tmp_path)
    old_a = store.bind_agent("agent-a", "old-a")
    old_b = store.bind_agent("agent-b", "old-b")
    original = store._write_binding

    def fail_second(binding):
        if (
            binding.status.value == "active"
            and binding.agent_instance_id == "agent-b"
            and binding.share_group_id == "shared-new"
        ):
            raise OSError("injected second-agent failure")
        return original(binding)

    monkeypatch.setattr(store, "_write_binding", fail_second)
    with pytest.raises(OSError):
        store.bind_agents_to_group(["agent-a", "agent-b"], "shared-new")

    active_a = store.find_by_agent("agent-a", include_inactive=False)
    active_b = store.find_by_agent("agent-b", include_inactive=False)
    assert [binding.binding_id for binding in active_a] == [old_a.binding_id]
    assert [binding.binding_id for binding in active_b] == [old_b.binding_id]
    assert store.find_by_group("shared-new", include_inactive=False) == []


def test_legacy_migration_status_requires_real_files(tmp_path: Path):
    store = AgentBindingStore(tmp_path)
    legacy = tmp_path / ".memoryguard" / "managed-memory" / "agent-a"
    legacy.mkdir(parents=True)
    empty = store.ensure_personal_memory_group("agent-a")
    assert empty["migration_required"] is False
    (legacy / "active.json").write_text("{}", encoding="utf-8")
    status = store.ensure_personal_memory_group("agent-a")
    assert status["migration_required"] is True


def test_personal_namespace_owner_and_multi_member_rejected(tmp_path: Path):
    store = AgentBindingStore(tmp_path)
    gid = personal_group_id("agent-a")
    with pytest.raises(ValueError, match="owner"):
        store.bind_agent("agent-b", gid)
    with pytest.raises(ValueError, match="personal_group_cannot_be_shared"):
        store.bind_agents_to_group(["agent-a", "agent-b"], gid)
    with pytest.raises(ValueError, match="at_least_two"):
        store.bind_agents_to_group(["agent-a"], "shared-single")
    assert store.list_bindings(include_inactive=False) == []


def test_unbound_read_is_fail_closed_and_personal_mcp_hits_store(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    denied = execute_tool("memoryguard_memory_status", {"workspace": str(tmp_path)})
    assert denied.get("isError") is True
    store = AgentBindingStore(tmp_path)
    ensured = store.ensure_personal_memory_group("agent-a")
    written = execute_tool(
        "memoryguard_memory_write",
        {
            "workspace": str(tmp_path),
            "agent_instance_id": "agent-a",
            "body": "remember this personal marker for future conversations",
        },
    )
    assert not written.get("isError")
    status = execute_tool("memoryguard_memory_status", {"workspace": str(tmp_path), "agent_instance_id": "agent-a"})
    assert not status.get("isError")
    assert ensured["group_id"] in (status["content"][0]["text"])
    search = execute_tool("memoryguard_memory_search", {"workspace": str(tmp_path), "agent_instance_id": "agent-a", "query": "personal marker"})
    assert "personal marker" in search["content"][0]["text"]


def test_mcp_search_defaults_to_active_records_only(tmp_path: Path, monkeypatch):
    """Conversation recall must not inject deleted history unless explicitly requested."""
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    group_id = AgentBindingStore(tmp_path).ensure_personal_memory_group("agent-a")["group_id"]
    store = SharedMemoryStore(tmp_path, group_id)
    store.append_record(SharedMemoryRecord(
        memory_id="active-recall",
        body="recall gate active marker",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
        agent_instance_id="agent-a",
    ))
    store.append_record(SharedMemoryRecord(
        memory_id="deleted-recall",
        body="recall gate deleted marker",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.DELETED,
        agent_instance_id="agent-a",
    ))

    default_search = execute_tool(
        "memoryguard_memory_search",
        {"agent_instance_id": "agent-a", "query": "recall gate"},
    )
    default_text = default_search["content"][0]["text"]
    assert "active marker" in default_text
    assert "deleted marker" not in default_text

    governance_search = execute_tool(
        "memoryguard_memory_search",
        {
            "agent_instance_id": "agent-a",
            "query": "recall gate",
            "status": "deleted",
        },
    )
    assert "deleted marker" in governance_search["content"][0]["text"]


def test_mutation_preflight_uses_trusted_binding_group(tmp_path: Path, monkeypatch):
    """更新预检必须读取可信 binding 的组，不能误查请求中的 default 组。"""
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    group_id = AgentBindingStore(tmp_path).ensure_personal_memory_group("agent-a")["group_id"]
    store = SharedMemoryStore(tmp_path, group_id)
    store.append_record(SharedMemoryRecord(
        memory_id="update-bound-record",
        body="old body",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        agent_instance_id="agent-a",
    ))

    result = execute_tool(
        "memoryguard_memory_update",
        {"memory_id": "update-bound-record", "body": "new body"},
    )

    assert not result.get("isError"), result
    assert store.get_record("update-bound-record").body == "new body"


def test_mcp_rejects_corrupt_multiple_active_bindings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    monkeypatch.setenv("MEMORYGUARD_ALLOW_ANON", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    store = AgentBindingStore(tmp_path)
    old = store.bind_agent("agent-a", "shared-old")
    store.bind_agent("agent-a", "shared-new")
    old.status = type(old.status).ACTIVE
    store._write_binding(old)

    result = execute_tool(
        "memoryguard_memory_status",
        {"agent_instance_id": "agent-a"},
    )
    assert result.get("isError") is True
    assert "multiple active bindings" in result["content"][0]["text"]


def test_native_file_unchanged_across_binding_switch(tmp_path: Path):
    native = tmp_path / "user_profile.md"
    native.write_bytes(b"native bytes\r\n")
    before = native.read_bytes()
    store = AgentBindingStore(tmp_path)
    store.ensure_personal_memory_group("agent-a")
    store.bind_agent("agent-a", "shared-team")
    store.leave_shared_group_to_personal("agent-a", confirmed=True)
    assert native.read_bytes() == before


def test_provider_install_requires_admin_before_personal_creation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_ADMIN", "0")
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    result = execute_tool("memoryguard_provider_install", {"provider": "claude", "workspace": str(tmp_path)})
    assert result.get("isError") is True
    assert not (tmp_path / ".memoryguard" / "agent-bindings").exists()
