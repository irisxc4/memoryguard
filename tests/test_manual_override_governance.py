from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.schema_v3 import (
    MemoryEvent,
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _event(event_id: str, body: str) -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        agent_instance_id="agent-a",
        share_group_id="group-a",
        raw_content=body,
        metadata={},
    )


def test_manual_edit_is_traceable_locked_and_not_auto_overwritten(tmp_path):
    organizer = AutoOrganizer(tmp_path, "group-a", enricher_mode="heuristic")
    original, _ = organizer.organize(
        _event("original", "用户长期偏好 Python 作为主要编程语言"),
        kind_override="preference",
    )
    store = SharedMemoryStore(tmp_path, "group-a")

    store.edit(
        original.memory_id,
        "用户长期偏好 Python 作为主要编程语言，人工确认",
    )
    edited = store.get_record(original.memory_id)
    assert edited is not None
    assert edited.locked is True
    assert any(
        item.source_object_id.startswith("manual-override:")
        and item.locator == "governance:edit"
        for item in edited.provenance
    )
    edit_decision = next(
        item for item in store.list_decisions() if item.action == "edit"
    )
    assert edit_decision.actor == "user"
    assert edit_decision.before_hash
    assert edit_decision.after_hash

    candidate, actions = organizer.organize(
        _event(
            "correction",
            "纠正：用户长期偏好 Rust 作为主要编程语言，不是 Python",
        ),
        kind_override="correction",
    )
    refreshed = store.get_record(original.memory_id)
    assert refreshed is not None
    assert refreshed.status == SharedMemoryStatus.ACTIVE
    assert refreshed.locked is True
    assert candidate.status == SharedMemoryStatus.LOW_CONFIDENCE
    assert any(
        item["action"] == "manual_override_conflict_candidate"
        for item in actions
    )
    assert [
        item.memory_id for item in store.list_records(status="active")
    ] == [original.memory_id]


def test_identical_auto_input_does_not_mutate_locked_manual_provenance(tmp_path):
    organizer = AutoOrganizer(tmp_path, "group-a", enricher_mode="heuristic")
    original, _ = organizer.organize(
        _event("original", "始终运行定向测试"),
        kind_override="preference",
    )
    store = SharedMemoryStore(tmp_path, "group-a")
    store.lock(original.memory_id)
    before = store.get_record(original.memory_id)
    assert before is not None

    result, actions = organizer.organize(
        _event("repeat", "始终运行定向测试"),
        kind_override="preference",
    )
    after = store.get_record(original.memory_id)

    assert result.memory_id == original.memory_id
    assert len(after.provenance) == len(before.provenance)
    assert any(
        item["action"] == "manual_override_suppressed"
        for item in actions
    )
    assert len(store.list_records(status="active")) == 1


def test_manual_delete_tombstone_suppresses_recreation_until_unlock(tmp_path):
    organizer = AutoOrganizer(tmp_path, "group-a", enricher_mode="heuristic")
    original, _ = organizer.organize(
        _event("original", "长期偏好：使用 pytest"),
        kind_override="preference",
    )
    store = SharedMemoryStore(tmp_path, "group-a")
    store.delete(original.memory_id)

    suppressed, actions = organizer.organize(
        _event("repeat", "长期偏好：使用 pytest"),
        kind_override="preference",
    )
    assert suppressed.memory_id == original.memory_id
    assert suppressed.status == SharedMemoryStatus.DELETED
    assert any(
        item["action"] == "manual_override_suppressed"
        for item in actions
    )
    assert store.list_records(status="active") == []

    store.unlock(original.memory_id)
    recreated, actions = organizer.organize(
        _event("after-unlock", "长期偏好：使用 pytest"),
        kind_override="preference",
    )
    assert recreated.status == SharedMemoryStatus.ACTIVE
    assert any(item["action"] == "create_active" for item in actions)


def test_manual_quarantine_is_traceable_and_suppresses_recreation(tmp_path):
    organizer = AutoOrganizer(tmp_path, "group-a", enricher_mode="heuristic")
    original, _ = organizer.organize(
        _event("original", "项目规则：发布前运行回归测试"),
        kind_override="procedure",
    )
    store = SharedMemoryStore(tmp_path, "group-a")
    store.quarantine_memory(
        original.memory_id,
        reason="human review",
        pattern="user_quarantine",
        original_content=original.body,
        actor="user",
        manual_override=True,
    )

    protected = store.get_record(original.memory_id)
    assert protected.status == SharedMemoryStatus.QUARANTINED
    assert protected.locked is True
    assert any(
        item.action == "manual_quarantine" and item.actor == "user"
        for item in store.list_decisions()
    )
    suppressed, actions = organizer.organize(
        _event("repeat", "项目规则：发布前运行回归测试"),
        kind_override="procedure",
    )
    assert suppressed.memory_id == original.memory_id
    assert any(
        item["action"] == "manual_override_suppressed"
        for item in actions
    )
    assert store.list_records(status="active") == []


def test_restore_old_memory_shadows_active_superseding_descendants(tmp_path):
    store = SharedMemoryStore(tmp_path, "group-a")
    old = SharedMemoryRecord(
        memory_id="old",
        body="旧偏好",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.SHADOWED,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    new = SharedMemoryRecord(
        memory_id="new",
        body="新偏好",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE,
        supersedes=["old"],
        created_at="2026-01-02T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    newest = SharedMemoryRecord(
        memory_id="newest",
        body="最新偏好",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE,
        supersedes=["new"],
        created_at="2026-01-03T00:00:00+00:00",
        updated_at="2026-01-03T00:00:00+00:00",
    )
    for item in (old, new, newest):
        store.update_record(item)

    store.restore("old")

    assert store.get_record("old").status == SharedMemoryStatus.ACTIVE
    assert store.get_record("old").locked is True
    assert store.get_record("new").status == SharedMemoryStatus.SHADOWED
    assert store.get_record("newest").status == SharedMemoryStatus.SHADOWED
    assert [item.memory_id for item in store.list_records(status="active")] == [
        "old"
    ]
    decision = next(
        item for item in store.list_decisions() if item.action == "restore"
    )
    assert decision.target_ids == ["old"]
    assert "shadow current descendants" in decision.reason


def test_conflict_and_quarantine_queues_close_after_human_resolution(
    tmp_path,
):
    from memoryguard.gui import GovernanceApi

    store = SharedMemoryStore(tmp_path, "group-a")
    first = SharedMemoryRecord(
        memory_id="first",
        body="偏好 A",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE,
    )
    second = SharedMemoryRecord(
        memory_id="second",
        body="偏好 B",
        kind=MemoryKind.PREFERENCE,
        status=SharedMemoryStatus.ACTIVE,
    )
    store.update_record(first)
    store.update_record(second)
    conflict_id = store.conflict(["first", "second"], "互斥偏好")
    api = GovernanceApi(str(tmp_path))

    resolved = api.resolve_conflict(
        conflict_id,
        "first",
        "group-a",
        _admin_override=True,
    )
    assert resolved["ok"] is True
    conflict = next(
        item for item in store.list_conflicts()
        if item.group_id == conflict_id
    )
    assert conflict.status.value == "resolved"
    assert conflict.resolution == "keep:first"
    assert api.get_conflicts("group-a")["total"] == 0

    third = SharedMemoryRecord(
        memory_id="third",
        body="待释放",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
    )
    store.update_record(third)
    store.quarantine_memory("third", "risk", "test", third.body)
    entry = next(
        item for item in store.list_quarantine()
        if item.memory_id == "third"
    )
    released = api.release_quarantine(
        entry.quarantine_id,
        "group-a",
        _admin_override=True,
    )
    assert released["ok"] is True
    assert next(
        item for item in store.list_quarantine()
        if item.quarantine_id == entry.quarantine_id
    ).released is True
    assert api.get_quarantine("group-a")["total"] == 0

    fourth = SharedMemoryRecord(
        memory_id="fourth",
        body="待软删除",
        kind=MemoryKind.FACT,
        status=SharedMemoryStatus.ACTIVE,
    )
    store.update_record(fourth)
    store.quarantine_memory("fourth", "risk", "test", fourth.body)
    delete_entry = next(
        item for item in store.list_quarantine()
        if item.memory_id == "fourth"
    )
    deleted = api.delete_quarantine(
        delete_entry.quarantine_id,
        "group-a",
        _admin_override=True,
    )
    assert deleted["ok"] is True
    assert store.get_record("fourth").status == SharedMemoryStatus.DELETED
    assert next(
        item for item in store.list_quarantine()
        if item.quarantine_id == delete_entry.quarantine_id
    ).released is True
    assert api.get_quarantine("group-a")["total"] == 0


def test_gui_governance_capabilities_remain_available():
    from memoryguard.gui import GovernanceApi

    for method in (
        "edit_memory",
        "lock_memory",
        "unlock_memory",
        "restore_memory",
        "delete_memory",
        "resolve_conflict",
        "release_quarantine",
        "delete_quarantine",
        "rollback_memory",
    ):
        assert callable(getattr(GovernanceApi, method))
