from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.governance_engine import GovernanceEngine
from memoryguard.gui import GovernanceApi
from memoryguard.mcp_server import (
    _handle_memory_delete,
    _handle_memory_update,
)
from memoryguard.schema_v3 import (
    MemoryEvent,
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(
    memory_id: str = "memory-a",
    agent_instance_id: str = "",
) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body="长期偏好：使用 pytest",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id=agent_instance_id,
    )


def test_engine_result_contract_locked_guard_and_idempotency(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    store.update_record(_record())
    engine = GovernanceEngine(tmp_path, "group-a", store=store)

    first = engine.agent_update(
        "memory-a",
        actor="agent:agent-a",
        body="长期偏好：使用 pytest -q",
        idempotency_key="retry-1",
    )
    assert set(first) == {
        "ok", "action", "actor", "before", "after", "decision_id",
        "version_id", "blocked_reason", "idempotency_key",
        "idempotent_replay",
    }
    assert first["ok"] is True
    assert first["version_id"]

    replay = engine.agent_update(
        "memory-a",
        actor="agent:agent-a",
        body="长期偏好：使用 pytest -q",
        idempotency_key="retry-1",
    )
    assert replay["ok"] is True
    assert replay["idempotent_replay"] is True
    assert replay["decision_id"] == first["decision_id"]

    conflict = engine.agent_update(
        "memory-a",
        actor="agent:agent-a",
        body="同 key 不同 payload",
        idempotency_key="retry-1",
    )
    assert conflict["ok"] is False
    assert conflict["blocked_reason"] == "idempotency_conflict"

    engine.human_lock("memory-a")
    blocked_update = engine.agent_update(
        "memory-a", actor="agent:agent-a", body="不得覆盖",
    )
    blocked_delete = engine.agent_delete(
        "memory-a", actor="agent:agent-a",
    )
    assert blocked_update["blocked_reason"] == "manual_override_locked"
    assert blocked_delete["blocked_reason"] == "manual_override_locked"

    engine.human_unlock("memory-a")
    assert engine.agent_delete(
        "memory-a", actor="agent:agent-a",
    )["ok"] is True


def test_invalid_quarantine_resolution_is_rejected(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    store.update_record(_record())
    engine = GovernanceEngine(tmp_path, "group-a", store=store)

    result = engine.resolve_quarantine(
        "missing", resolution="typo-delete",
    )
    assert result["ok"] is False
    assert result["blocked_reason"] == "invalid_quarantine_resolution"
    assert store.get_record("memory-a").status == SharedMemoryStatus.ACTIVE


def test_auto_write_idempotency_replays_original_record_without_new_rows(
    tmp_path,
):
    engine = GovernanceEngine(tmp_path, "group-a")

    def event(body: str) -> MemoryEvent:
        return MemoryEvent(
            event_id="caller-generated-event",
            agent_instance_id="agent-a",
            share_group_id="group-a",
            raw_content=body,
            metadata={"source": "test"},
        )

    first = engine.auto_write(
        event("长期偏好：运行 pytest"),
        kind_override="preference",
        idempotency_key="write-retry-1",
    )
    counts = (
        len(engine.store.list_events()),
        len(engine.store.list_records()),
        len(engine.store.list_decisions()),
    )

    replay = engine.auto_write(
        event("长期偏好：运行 pytest"),
        kind_override="preference",
        idempotency_key="write-retry-1",
    )
    assert replay["ok"] is True
    assert replay["idempotent_replay"] is True
    assert (
        replay["memory_id"],
        replay["status"],
        replay["kind"],
    ) == (
        first["memory_id"],
        first["status"],
        first["kind"],
    )
    assert (
        len(engine.store.list_events()),
        len(engine.store.list_records()),
        len(engine.store.list_decisions()),
    ) == counts

    conflict = engine.auto_write(
        event("同 key 不同 payload"),
        kind_override="preference",
        idempotency_key="write-retry-1",
    )
    assert conflict["ok"] is False
    assert conflict["blocked_reason"] == "idempotency_conflict"
    assert (
        len(engine.store.list_events()),
        len(engine.store.list_records()),
        len(engine.store.list_decisions()),
    ) == counts


def test_gui_and_mcp_mutations_delegate_to_engine(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, tuple, dict]] = []

    def _spy(name):
        def invoke(self, *args, **kwargs):
            calls.append((name, args, kwargs))
            return {
                "ok": True,
                "action": name,
                "actor": "test",
                "before": None,
                "after": None,
                "decision_id": "decision",
                "version_id": "version",
                "blocked_reason": "",
                "idempotency_key": "",
                "idempotent_replay": False,
            }
        return invoke

    for method in (
        "human_edit",
        "human_lock",
        "human_unlock",
        "human_delete",
        "human_restore",
        "resolve_conflict",
        "resolve_quarantine",
        "agent_update",
        "agent_delete",
    ):
        monkeypatch.setattr(GovernanceEngine, method, _spy(method))

    api = GovernanceApi(str(tmp_path))
    api.edit_memory("m", "body", "group-a", _admin_override=True)
    api.lock_memory("m", "group-a", _admin_override=True)
    api.unlock_memory("m", "group-a", _admin_override=True)
    api.delete_memory("m", "group-a", _admin_override=True)
    api.restore_memory("m", "group-a", _admin_override=True)
    api.resolve_conflict("c", "m", "group-a", _admin_override=True)
    api.release_quarantine("q", "group-a", _admin_override=True)
    api.delete_quarantine("q", "group-a", _admin_override=True)

    AgentBindingStore(tmp_path).bind_agent("agent-a", "group-a")
    SharedMemoryStore(tmp_path, "group-a").update_record(
        _record("mcp-memory", "agent-a")
    )
    monkeypatch.setenv("MEMORYGUARD_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MEMORYGUARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("MEMORYGUARD_STRICT_BINDING", "1")
    _handle_memory_update({
        "memory_id": "mcp-memory",
        "body": "new",
    })
    _handle_memory_delete({"memory_id": "mcp-memory"})

    names = [item[0] for item in calls]
    assert names == [
        "human_edit",
        "human_lock",
        "human_unlock",
        "human_delete",
        "human_restore",
        "resolve_conflict",
        "resolve_quarantine",
        "resolve_quarantine",
        "agent_update",
        "agent_delete",
    ]


def test_auto_organizer_delegates_protected_state_policy(
    tmp_path,
    monkeypatch,
):
    calls = []

    def allow(self, content, *, threshold):
        calls.append((content, threshold))
        return {
            "ok": True,
            "policy": "allow",
            "blocked_reason": "",
        }

    monkeypatch.setattr(
        GovernanceEngine, "evaluate_auto_write", allow,
    )
    organizer = AutoOrganizer(
        tmp_path, "group-a", enricher_mode="heuristic",
    )
    organizer.organize(MemoryEvent(
        event_id="event",
        agent_instance_id="agent-a",
        share_group_id="group-a",
        raw_content="长期偏好：运行 pytest",
        metadata={},
    ))
    assert calls


def test_adapters_do_not_call_store_business_mutations_directly():
    root = Path(__file__).resolve().parents[1]
    forbidden_manual_mutations = (
        "store.edit(",
        "store.lock(",
        "store.unlock(",
        "store.delete(",
        "store.restore(",
        "store.quarantine_memory(",
        "store.resolve_conflict_group(",
        "store.close_quarantine(",
        "store._update_record_field(",
    )
    production_adapters = (
        "src/memoryguard/gui.py",
        "src/memoryguard/mcp_server.py",
        "src/memoryguard/external_mcp_detector.py",
        "src/memoryguard/shared_memory_import.py",
    )
    forbidden_write_primitives = (
        ".append_record(",
        ".supersede(",
        ".conflict(",
        ".append_decision(",
        ".update_record(",
        ".append_event(",
        ".update_event(",
    )
    for relative in production_adapters:
        source = (root / relative).read_text(encoding="utf-8")
        assert "from .auto_organizer import AutoOrganizer" not in source
        assert "AutoOrganizer(" not in source
        for token in (
            *forbidden_manual_mutations,
            *forbidden_write_primitives,
        ):
            assert token not in source, f"{relative} bypasses engine via {token}"
