"""Manual governance through the V2 memory and native GUI boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore, MemoryReadScope
from memoryguard.runtime_v2.native_ports import (
    NativeV2RuntimePort,
    bind_native_transport_context,
)
from memoryguard.schema_v3 import MemoryEvent


GROUP = "group-a"
AGENT = "agent-a"


def _context(root: Path, *, agent: str = AGENT, group: str = GROUP):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id=agent,
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id=f"manual-{agent}",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(root.resolve()),
        share_group_id=group,
        project_ref="",
        provider="",
        runtime_role="",
        entrypoint="gui",
    )


def _port(root: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        root,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 7},
    )


def _event(event_id: str, body: str, *, group: str = GROUP) -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        agent_instance_id=AGENT,
        share_group_id=group,
        raw_content=body,
        metadata={},
    )


def _fixture(root: Path) -> tuple[AutoOrganizer, MemoryAtomStore, GovernanceV2]:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    organizer = AutoOrganizer(
        root,
        GROUP,
        store=memory,
        engine=governance,
        threshold=0.85,
    )
    return organizer, memory, governance


def _read(memory: MemoryAtomStore, memory_id: str, *, group: str = GROUP):
    return memory.get_atom(
        memory_id,
        scope=MemoryReadScope(
            workspace_id=str(memory.layout.workspace),
            share_group_id=group,
            agent_instance_id=AGENT,
            project_ref="",
            provider="",
            runtime_role="",
            admin=True,
        ),
        include_building=True,
    )


def _publish(root: Path, memory: MemoryAtomStore) -> None:
    """Complete the V2 evidence projection before a native read/mutation."""
    evidence = EvidenceStore(root)
    while memory.pending_outbox(include_failed=True):
        memory.project_evidence(evidence)
    memory.set_visibility("active")


def _gui(root: Path, name: str, payload: dict):
    result = _port(root).dispatch_gui(
        name,
        payload,
        context=_context(root),
        generation=7,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, f"{result.get('code')}: {result.get('error')}"
    return result


def test_manual_edit_is_traceable_locked_and_not_auto_overwritten(tmp_path: Path):
    organizer, memory, _governance = _fixture(tmp_path)
    original, _ = organizer.organize(
        _event("original", "用户长期偏好 Python 作为主要编程语言"),
        kind_override="preference",
    )
    _publish(tmp_path, memory)
    edited = _gui(tmp_path, "edit_memory", {
        "memory_id": original.memory_id,
        "body": "用户长期偏好 Python 作为主要编程语言，人工确认",
    })
    locked = _gui(tmp_path, "lock_memory", {"memory_id": original.memory_id})
    current = _read(memory, original.memory_id)
    assert current is not None
    assert current.body.endswith("人工确认")
    assert current.locked is True
    assert edited["data"]["receipt"]
    first_lock_decision = locked["data"]["receipt"]["decision_id"]
    first_lock_revision = current.revision

    def repeat_lock(_index: int):
        return _gui(tmp_path, "lock_memory", {"memory_id": original.memory_id})

    with ThreadPoolExecutor(max_workers=3) as pool:
        lock_replays = list(pool.map(repeat_lock, range(3)))
    assert {item["data"]["receipt"]["decision_id"] for item in lock_replays} == {first_lock_decision}
    assert _read(memory, original.memory_id).revision == first_lock_revision

    candidate, _actions = organizer.organize(
        _event("correction", "纠正：用户长期偏好 Rust 作为主要编程语言，不是 Python"),
        kind_override="correction",
    )
    _publish(tmp_path, memory)
    refreshed = _read(memory, original.memory_id)
    assert refreshed is not None
    assert refreshed.locked is True
    assert refreshed.body.endswith("人工确认")
    assert candidate.memory_id != original.memory_id


def test_identical_auto_input_does_not_mutate_locked_manual_provenance(tmp_path: Path):
    organizer, memory, _governance = _fixture(tmp_path)
    original, _ = organizer.organize(
        _event("original", "始终运行定向测试"),
        kind_override="preference",
    )
    _publish(tmp_path, memory)
    _gui(tmp_path, "lock_memory", {"memory_id": original.memory_id})
    before = _read(memory, original.memory_id)
    assert before is not None

    result, _actions = organizer.organize(
        _event("repeat", "始终运行定向测试"),
        kind_override="preference",
    )
    after = _read(memory, original.memory_id)
    assert result.memory_id == original.memory_id
    assert after is not None
    assert after.locked is True
    assert after.body == before.body
    assert len(after.provenance) == len(before.provenance)
    assert any(item["action"] == "manual_override_suppressed" for item in _actions)


def test_manual_delete_tombstone_suppresses_recreation_until_unlock(tmp_path: Path):
    organizer, memory, _governance = _fixture(tmp_path)
    original, _ = organizer.organize(
        _event("original", "长期偏好：使用 pytest"),
        kind_override="preference",
    )
    _publish(tmp_path, memory)
    deleted = _gui(tmp_path, "delete_memory", {"memory_id": original.memory_id})
    tombstone = _read(memory, original.memory_id)
    assert deleted["data"]["receipt"]
    assert tombstone is not None and tombstone.status == "deleted"

    _gui(tmp_path, "unlock_memory", {"memory_id": original.memory_id})
    restored = _gui(tmp_path, "restore_memory", {"memory_id": original.memory_id})
    current = _read(memory, original.memory_id)
    assert restored["data"]["receipt"]
    assert current is not None and current.status == "active"
    assert current.body == original.body
    restore_decision = restored["data"]["receipt"]["decision_id"]
    restore_revision = current.revision
    replay = _gui(tmp_path, "restore_memory", {"memory_id": original.memory_id})
    assert replay["data"]["receipt"]["decision_id"] == restore_decision
    assert _read(memory, original.memory_id).revision == restore_revision


def test_manual_quarantine_is_traceable_and_suppresses_recreation(tmp_path: Path):
    organizer, memory, _governance = _fixture(tmp_path)
    original, _ = organizer.organize(
        _event("original", "项目规则：发布前运行回归测试"),
        kind_override="procedure",
    )
    _publish(tmp_path, memory)
    result = _port(tmp_path).dispatch_gui(
        "neuron_decide",
        [original.memory_id, "quarantine", "human review", True, None, "", ""],
        context=_context(tmp_path),
        generation=7,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert result["ok"] is True, result
    protected = _read(memory, original.memory_id)
    assert protected is not None
    assert protected.status == "quarantined"
    assert protected.metadata.get("quarantine_id")
    queue = _port(tmp_path).dispatch_gui(
        "get_quarantine", [GROUP], context=_context(tmp_path), generation=7,
        state="V2_ACTIVE",
    )
    assert queue["ok"] is True and queue["data"]["total"] == 1
    released = _port(tmp_path).dispatch_gui(
        "release_quarantine", [queue["data"]["quarantine"][0]["quarantine_id"], GROUP],
        context=_context(tmp_path), generation=7, mutation=True, state="V2_ACTIVE",
    )
    assert released["ok"] is True
    assert _read(memory, original.memory_id).status == "active"


def test_restore_old_memory_shadows_active_superseding_descendants(tmp_path: Path):
    _organizer, memory, governance = _fixture(tmp_path)
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=GROUP,
        agent_instance_id=AGENT,
        project_ref="",
        provider="",
        runtime_role="",
        actor="manual-fixture",
        authority="manual",
        admin=True,
    )
    atoms = []
    for memory_id, body in (("old", "旧偏好"), ("new", "新偏好"), ("newest", "最新偏好")):
        atom, _ = governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                kind="preference",
                status="active",
                workspace_id=str(tmp_path.resolve()),
                share_group_id=GROUP,
                agent_instance_id=AGENT,
                project_ref="",
                provider="",
                runtime_role="",
            ),
            context=context,
            evidence=[{"source_ref": f"restore:{memory_id}"}],
            reason="restore fixture",
            idempotency_key=f"restore:{memory_id}",
        )
        atoms.append(atom)
    memory.supersede("old", "new", context=context, reason="newer rule")
    memory.supersede("new", "newest", context=context, reason="newest rule")
    _publish(tmp_path, memory)
    restored = _gui(tmp_path, "restore_memory", {"memory_id": "old"})
    old = _read(memory, "old")
    new = _read(memory, "new")
    newest = _read(memory, "newest")
    assert restored["data"]["receipt"]
    assert old is not None and old.status == "active" and old.locked is True
    assert new is not None and new.status == "superseded"
    assert newest is not None and "new" in newest.supersedes
    assert newest.status == "shadowed"


def test_conflict_and_quarantine_queues_close_after_human_resolution(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    governance = GovernanceV2(tmp_path, memory_store=memory, evidence_store=evidence)
    context = V2MutationContext(
        workspace_id=str(tmp_path.resolve()),
        share_group_id=GROUP,
        agent_instance_id=AGENT,
        project_ref="",
        provider="",
        runtime_role="",
        actor=AGENT,
        authority="admin",
        admin=True,
    )
    for memory_id, body, metadata in (
        ("conflict-keep", "preferred fact", {"conflict_group_id": "conflict-1", "conflict_status": "unresolved", "conflict_reason": "disagreement"}),
        ("conflict-drop", "stale fact", {"conflict_group_id": "conflict-1", "conflict_status": "unresolved", "conflict_reason": "disagreement"}),
        ("quarantine-me", "private governed memory", {}),
    ):
        governance.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                kind="fact",
                metadata=metadata,
                workspace_id=str(tmp_path.resolve()),
                share_group_id=GROUP,
                agent_instance_id=AGENT,
                project_ref="",
                provider="",
                runtime_role="",
            ),
            context=context,
            evidence=[{"source_ref": f"queue:{memory_id}"}],
            reason="queue fixture",
        )
    _publish(tmp_path, memory)
    port = _port(tmp_path)
    quarantined = port.dispatch_gui(
        "neuron_decide", ["quarantine-me", "quarantine", "review", True, None, "", ""],
        context=_context(tmp_path), generation=7, mutation=True, state="V2_ACTIVE",
    )
    assert quarantined["ok"] is True
    conflicts = port.dispatch_gui(
        "get_conflicts", [GROUP], context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert conflicts["ok"] is True
    assert conflicts["data"]["total"] == 1
    resolved = port.dispatch_gui(
        "resolve_conflict", ["conflict-1", "conflict-keep", GROUP],
        context=_context(tmp_path), generation=7, mutation=True, state="V2_ACTIVE",
    )
    assert resolved["ok"] is True
    assert resolved["data"]["deleted_memory_ids"] == ["conflict-drop"]
    released = port.dispatch_gui(
        "get_quarantine", [], context=_context(tmp_path), generation=7, state="V2_ACTIVE",
    )
    assert released["ok"] is True and released["data"]["total"] == 1
    entry = released["data"]["quarantine"][0]
    done = port.dispatch_gui(
        "release_quarantine", [entry["quarantine_id"], GROUP],
        context=_context(tmp_path), generation=7, mutation=True, state="V2_ACTIVE",
    )
    assert done["ok"] is True
    assert memory.get_atom("conflict-drop", scope=context.to_dict(), include_building=True).status == "deleted"


def test_gui_governance_capabilities_remain_available():
    from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort

    entries = {
        item["name"]: item
        for item in NativeV2RuntimePort(
            Path("."), state_provider=lambda: {"state": "V2_ACTIVE", "generation": 1}
        ).coverage()["surfaces"]["gui"]["entries"]
    }
    assert all(
        entries[name]["status"] == "implemented" and entries[name]["mutation"]
        for name in ("edit_memory", "lock_memory", "unlock_memory", "restore_memory", "delete_memory")
    )
