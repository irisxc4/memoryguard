import json
import shutil

import pytest

from memoryguard.context_bootstrap import build_context_packet
from memoryguard.schema_v3 import EffectiveAgentContext, MemoryKind, SharedMemoryRecord, SharedMemoryStatus
from memoryguard.shared_memory_store import MANDATORY_MAX_ITEMS, SharedMemoryStore


def _record(memory_id: str, writer: str = "writer") -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id, body=f"mandatory {memory_id}",
        kind=MemoryKind.PROCEDURE, status=SharedMemoryStatus.ACTIVE,
        injection_policy="always", agent_instance_id=writer,
    )


def _packet(store: SharedMemoryStore, agent: str = "a", **extra):
    return build_context_packet(
        store, task="unrelated", effective_context=EffectiveAgentContext(
            agent_instance_id=agent, share_group_id=store.group_id, **extra,
        ),
    )


@pytest.mark.parametrize("first, second", [
    ({"target_type": "agent", "target_id": "a"}, {"target_type": "agent_project", "target_id": "a", "project_ref": "p"}),
    ({"target_type": "group"}, {"target_type": "agent", "target_id": "a"}),
    ({"target_type": "system"}, {"target_type": "provider", "target_id": "codex"}),
    ({"target_type": "project", "project_ref": "p"}, {"target_type": "agent_project", "target_id": "a", "project_ref": "p"}),
    ({"target_type": "provider", "target_id": "codex"}, {"target_type": "runtime_role", "target_id": "worker"}),
])
def test_budget_rejects_any_overlapping_effective_context(tmp_path, first, second):
    store = SharedMemoryStore(tmp_path, "team")
    for index in range(MANDATORY_MAX_ITEMS):
        store.append_record(_record(f"first-{index}"), assignments=[first])
    with pytest.raises(ValueError, match="mandatory.*budget_exceeded"):
        store.append_record(_record("cross-scope"), assignments=[second])


def test_overlapping_but_non_equivalent_audiences_never_dedup(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    first = _record("agent-a", "a")
    store.append_record(first, assignments=[{"target_type": "agent", "target_id": "a"}])
    assert store.record_domain_overlaps(
        first, "always", [{"target_type": "group"}],
    ) is False
    second = _record("group", "writer")
    second.body = first.body
    store.append_record(second, assignments=[{"target_type": "group"}])
    assert {item.memory_id for item in store.list_records()} == {"agent-a", "group"}
    # Bootstrap deduplicates equal bodies for one recipient, but both durable
    # records (and their distinct audiences) remain intact.
    assert _packet(store, "a")["mandatory_rule_ids"] == ["agent-a"]
    assert _packet(store, "b")["mandatory_rule_ids"] == ["group"]


def test_corrupt_policy_only_fail_closes_the_matching_audience(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("bad", "b"))
    with store._tx() as conn:
        conn.execute("UPDATE records SET injection_policy='broken' WHERE memory_id='bad'")
    good = _packet(store, "a")
    blocked = _packet(store, "b")
    assert good["mandatory_overflow"] is False
    assert good["mandatory_rule_ids"] == []
    assert blocked["mandatory_overflow"] is True
    assert blocked["assignment_receipt"]["corrupt"][0]["reason"] == "corrupt_rule_matched_audience"


def test_corrupt_provenance_isolated_and_undetermined_rule_is_not_injected(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("bad-provenance", "a"))
    with store._tx() as conn:
        conn.execute("UPDATE records SET provenance='not-json' WHERE memory_id='bad-provenance'")
        conn.execute("UPDATE rule_assignments SET target_type='unknown' WHERE memory_id='bad-provenance'")
    packet = _packet(store, "a")
    assert packet["mandatory_overflow"] is False
    assert packet["mandatory_rule_ids"] == []
    assert packet["assignment_receipt"]["skipped"] or packet["assignment_receipt"]["corrupt"]


def test_malformed_snapshot_is_a_true_noop(tmp_path):
    store = SharedMemoryStore(tmp_path, "team")
    store.append_record(_record("kept", "a"))
    version = store.create_version_snapshot("valid")
    before_ids = [item.memory_id for item in store.list_records()]
    before_active = store.get_active_version_id()
    before_versions = len(store.list_versions())
    with store._tx() as conn:
        conn.execute("UPDATE versions SET snapshot=? WHERE version_id=?", ("{", version))
    with pytest.raises(ValueError, match="invalid_snapshot_json"):
        store.rollback_to_version(version)
    assert [item.memory_id for item in store.list_records()] == before_ids
    assert store.get_active_version_id() == before_active
    assert len(store.list_versions()) == before_versions


def test_jsonl_backup_restores_assignment_scope_and_clear_cannot_resurrect(tmp_path):
    source = SharedMemoryStore(tmp_path / "source", "team")
    source.append_record(_record("group"), assignments=[{"target_type": "group"}])
    source.append_record(_record("system"), assignments=[{"target_type": "system"}])
    source.export_jsonl_backup()
    destination = SharedMemoryStore(tmp_path / "destination", "team")
    for name in ("records.jsonl", "rule_assignments.jsonl"):
        shutil.copy2(source.root / name, destination.root / name)
    restored = SharedMemoryStore(tmp_path / "destination", "team")
    assert {item.memory_id for item in restored.list_records()} == {"group", "system"}
    assert {item.target_type for item in restored.list_rule_assignments()} == {"group", "system"}
    assert set(_packet(restored, "any")["mandatory_rule_ids"]) == {"group", "system"}

    source.clear_all()
    reopened = SharedMemoryStore(tmp_path / "source", "team")
    assert reopened.list_records() == []
    assert reopened.list_rule_assignments() == []


def test_old_backup_without_assignment_remains_legacy_unscoped(tmp_path):
    source = SharedMemoryStore(tmp_path / "old", "team")
    source.append_record(_record("legacy", "writer"))
    source.records_bak_path.write_text(
        json.dumps(_record("legacy", "writer").to_dict()) + "\n", encoding="utf-8",
    )
    target = SharedMemoryStore(tmp_path / "restored", "team")
    shutil.copy2(source.records_bak_path, target.records_bak_path)
    restored = SharedMemoryStore(tmp_path / "restored", "team")
    packet = _packet(restored, "writer")
    assert packet["mandatory_rule_ids"] == []
    assert packet["legacy_unscoped_rule_ids"] == ["legacy"]
