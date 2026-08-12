from concurrent.futures import ThreadPoolExecutor

from memoryguard.auto_organizer import AutoOrganizer
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore, MemoryReadScope
from memoryguard.schema_v3 import MemoryEvent


def _organizer(tmp_path, group: str, *, threshold: float = 0.85) -> tuple[AutoOrganizer, MemoryAtomStore]:
    store = MemoryAtomStore(tmp_path)
    return (
        AutoOrganizer(
            tmp_path,
            group,
            store=store,
            engine=GovernanceV2(tmp_path, memory_store=store),
            threshold=threshold,
        ),
        store,
    )


def _atoms(store: MemoryAtomStore, tmp_path, group: str):
    return store.list_atoms(
        scope=MemoryReadScope(
            workspace_id=str(tmp_path),
            share_group_id=group,
            admin=True,
        ),
        include_building=True,
    )


def _event(agent: str, group: str, body: str, event_id: str) -> MemoryEvent:
    return MemoryEvent(
        event_id=event_id,
        agent_instance_id=agent,
        share_group_id=group,
        raw_content=body,
    )


def test_v2_same_group_exact_and_near_duplicate_merge_provenance(tmp_path):
    organizer, store = _organizer(tmp_path, "team", threshold=0.50)
    first, _ = organizer.organize(_event("agent-a", "team", "Team uses Python for backend tests.", "event-a"))
    second, actions = organizer.organize(_event("agent-b", "team", "Team uses Python for backend tests every day.", "event-b"))

    assert first.memory_id == second.memory_id
    assert any(item["action"] == "merge_provenance" for item in actions)
    atoms = _atoms(store, tmp_path, "team")
    assert len(atoms) == 1
    assert {item["agent_instance_id"] for item in atoms[0].provenance} == {"agent-a", "agent-b"}
    assert "body" not in atoms[0].metadata


def test_v2_same_event_is_idempotent_and_groups_are_isolated(tmp_path):
    organizer, store = _organizer(tmp_path, "team")
    event = _event("agent-a", "team", "Use the shared formatter.", "same-event")
    first, _ = organizer.organize(event)
    second, actions = organizer.organize(event)

    assert first.memory_id == second.memory_id
    assert any(item["action"] == "idempotent_replay" for item in actions)
    assert len(_atoms(store, tmp_path, "team")) == 1

    other, _ = _organizer(tmp_path, "other")
    other_atom, _ = other.organize(_event("agent-b", "other", "Use the shared formatter.", "other-event"))
    assert other_atom.memory_id != first.memory_id
    assert len(_atoms(store, tmp_path, "other")) == 1
    assert len(_atoms(store, tmp_path, "team")) == 1


def test_v2_concurrent_same_group_writes_keep_one_canonical_atom(tmp_path):
    store = MemoryAtomStore(tmp_path)
    engine = GovernanceV2(tmp_path, memory_store=store)

    def write(index: int):
        organizer = AutoOrganizer(
            tmp_path,
            "team",
            store=store,
            engine=engine,
        )
        return organizer.organize(
            _event(f"agent-{index}", "team", "The shared deployment rule is required.", f"event-{index}")
        )[0]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(write, range(6)))

    assert len({item.memory_id for item in results}) == 1
    atoms = _atoms(store, tmp_path, "team")
    assert len(atoms) == 1
    assert len(atoms[0].provenance) == 6
