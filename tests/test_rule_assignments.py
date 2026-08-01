from memoryguard.context_bootstrap import build_context_packet
from memoryguard.rule_scope import canonical_project_ref
from memoryguard.schema_v3 import EffectiveAgentContext, MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(memory_id: str, writer: str = "writer"):
    return SharedMemoryRecord(
        memory_id=memory_id, body=f"rule {memory_id}", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        agent_instance_id=writer,
    )


def _packet(store, agent, **extra):
    return build_context_packet(
        store, task="unrelated work", effective_context=EffectiveAgentContext(
            agent_instance_id=agent, share_group_id=store.group_id, **extra,
        ),
    )


def test_agent_assignment_has_zero_cross_agent_leakage(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("only-a", "a"))
    store.set_rule_assignments("only-a", [{"target_type": "agent", "target_id": "a"}])
    assert _packet(store, "a")["mandatory_rule_ids"] == ["only-a"]
    assert _packet(store, "b")["mandatory_rule_ids"] == []


def test_group_exclude_wins_over_include(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("public"))
    store.set_rule_assignments("public", [
        {"target_type": "group"},
        {"target_type": "agent", "target_id": "a", "effect": "exclude"},
    ])
    assert _packet(store, "b")["mandatory_rule_ids"] == ["public"]
    assert _packet(store, "a")["mandatory_rule_ids"] == []


def test_project_provider_and_role_are_intersections(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    for key in ("project", "provider", "role"):
        store.append_record(_record(key))
    store.set_rule_assignments("project", [{"target_type": "agent_project", "target_id": "a", "project_ref": "p"}])
    store.set_rule_assignments("provider", [{"target_type": "provider", "target_id": "codex"}])
    store.set_rule_assignments("role", [{"target_type": "runtime_role", "target_id": "terra"}])
    packet = _packet(store, "a", project_ref="p", provider="codex", runtime_role="terra")
    assert set(packet["mandatory_rule_ids"]) == {"project", "provider", "role"}
    assert _packet(store, "a", project_ref="q", provider="claude")["mandatory_rule_ids"] == []


def test_windows_project_paths_are_canonicalized_for_matching(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("windows-project", "a"), assignments=[{
        "target_type": "agent_project",
        "target_id": "a",
        "project_ref": r"C:\Work\Demo",
    }])

    assignment = store.list_rule_assignments("windows-project")[0]
    assert assignment.project_ref == canonical_project_ref(r"c:/work/demo")
    assert _packet(
        store, "a", project_ref="c:/work/demo",
    )["mandatory_rule_ids"] == ["windows-project"]


def test_unscoped_legacy_rule_is_quarantined_from_injection_without_group_dos(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    # Insert without a writer to model a pre-audience historical record.
    store.append_record(_record("legacy", ""))
    packet = _packet(store, "a")
    assert packet["mandatory_overflow"] is False
    assert packet["mandatory_rule_ids"] == []
    assert packet["legacy_unscoped_rule_ids"] == ["legacy"]
    assert packet["assignment_receipt"]["skipped"][0]["reason"] == (
        "legacy_unscoped_governance_required"
    )


def test_equal_body_relevant_and_mandatory_do_not_collapse_in_either_order(tmp_path):
    for reverse in (False, True):
        store = SharedMemoryStore(tmp_path / str(reverse), "team")
        relevant = SharedMemoryRecord(
            memory_id="relevant", body="shared release process",
            kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
            injection_policy="relevant", agent_instance_id="a",
        )
        mandatory = SharedMemoryRecord(
            memory_id="mandatory", body="shared release process",
            kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
            injection_policy="always", agent_instance_id="b",
        )
        for record in ((mandatory, relevant) if reverse else (relevant, mandatory)):
            store.append_record(record)
        assert {item.memory_id for item in store.list_records()} == {
            "relevant", "mandatory",
        }
        packet = build_context_packet(
            store, task="shared release process",
            effective_context=EffectiveAgentContext("b", "team"),
        )
        assert packet["mandatory_rule_ids"] == ["mandatory"]
        assert packet["recalled_memory_ids"] == ["relevant"]


def test_mandatory_budget_is_per_agent(tmp_path):
    from memoryguard.shared_memory_store import MANDATORY_MAX_ITEMS
    store = SharedMemoryStore(tmp_path, "team")
    for index in range(MANDATORY_MAX_ITEMS):
        store.append_record(_record(f"a-{index}", "a"))
    store.append_record(_record("b-first", "b"))
    assert _packet(store, "b")["mandatory_rule_ids"] == ["b-first"]
    import pytest
    with pytest.raises(ValueError, match="mandatory_rule_budget_exceeded"):
        store.append_record(_record("a-over", "a"))


def test_priority_override_orders_and_receipts_stable_assignment_id(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    low = _record("low", "a")
    low.priority = 90
    high = _record("high", "a")
    high.priority = 0
    store.append_record(low)
    store.append_record(high, assignments=[{
        "target_type": "agent", "target_id": "a",
        "priority_override": 100,
    }])
    packet = _packet(store, "a")
    assert packet["mandatory_rule_ids"][:2] == ["high", "low"]
    receipt = next(
        item for item in packet["assignment_receipt"]["agent"]
        if item["memory_id"] == "high"
    )
    assert receipt["assignment_id"]
    assert receipt["base_priority"] == 0
    assert receipt["effective_priority"] == 100


def test_corrupt_rule_only_blocks_matching_audience(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("bad-a", "a"))
    with store._tx() as conn:
        conn.execute(
            "UPDATE records SET priority=999 WHERE memory_id='bad-a'"
        )
    assert _packet(store, "b")["mandatory_overflow"] is False
    assert _packet(store, "b")["mandatory_rule_ids"] == []
    assert _packet(store, "a")["mandatory_overflow"] is True


def test_assignments_survive_snapshot_and_clear_cascades(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("snap", "a"))
    version = store.create_version_snapshot("with audience")
    store.delete_rule_assignments("snap")
    store.rollback_to_version(version)
    assert store.list_rule_assignments("snap")[0].target_id == "a"
    store.clear_all()
    assert store.list_rule_assignments() == []
