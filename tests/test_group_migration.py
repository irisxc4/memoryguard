# -*- coding: utf-8 -*-
"""Part C: legacy-group discovery, idempotent migration, no-silent-empty-group guards."""
from __future__ import annotations

import logging

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.group_migration import (
    copy_group_records,
    discover_legacy_group,
    find_nonempty_shared_groups,
)
from memoryguard.schema_v3 import SharedMemoryRecord
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(mid: str, body: str, **extra) -> SharedMemoryRecord:
    d = {
        "memory_id": mid, "body": body, "kind": "fact", "status": "active",
        "confidence": 0.9,
        "injection_policy": "relevant", "priority": 0,
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        "agent_instance_id": "agent-0",
    }
    d.update(extra)
    return SharedMemoryRecord.from_dict(d)


def test_find_nonempty_shared_groups_lists_only_populated_groups(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    store = SharedMemoryStore(ws, "shared-a")
    store.append_record(_record("r1", "one"))
    SharedMemoryStore(ws, "shared-empty")
    found = find_nonempty_shared_groups(ws)
    groups = [g["group_id"] for g in found if g["records"] > 0]
    assert groups == ["shared-a"]
    assert found[0]["records"] == 1


def test_discover_legacy_group_returns_largest(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    a = SharedMemoryStore(ws, "shared-a")
    for i in range(2):
        a.append_record(_record(f"r{i}", f"body {i}"))
    b = SharedMemoryStore(ws, "shared-b")
    b.append_record(_record("r9", "solo"))
    legacy = discover_legacy_group(ws)
    assert legacy["group_id"] == "shared-a"
    assert legacy["records"] == 2


def test_copy_group_records_round_trip_and_idempotency(tmp_path):
    src_ws = tmp_path / "src"
    tgt_ws = tmp_path / "tgt"
    src_ws.mkdir()
    tgt_ws.mkdir()
    src = SharedMemoryStore(src_ws, "shared-legacy")
    src.append_record(_record("mig-0", "rule 0"))
    src.append_record(_record("mig-1", "rule 1"))
    always = _record("mig-rule", "must rule", kind="preference",
                     injection_policy="always", priority=5)
    src.append_record(always)
    SharedMemoryStore(tgt_ws, "shared-new")  # target exists, empty

    # dry-run writes nothing
    dry = copy_group_records(src_ws, "shared-legacy", tgt_ws, "shared-new", dry_run=True)
    assert dry["copied"] == 3
    assert SharedMemoryStore(tgt_ws, "shared-new").list_records() == []

    # apply
    res = copy_group_records(src_ws, "shared-legacy", tgt_ws, "shared-new")
    assert res["copied"] == 3 and not res["failed"]
    tgt_records = SharedMemoryStore(tgt_ws, "shared-new").list_records()
    by_id = {r.memory_id: r for r in tgt_records}
    assert len(by_id) == 3
    assert by_id["mig-0"].body == "rule 0"
    assert by_id["mig-rule"].injection_policy == "always"
    assert by_id["mig-rule"].priority == 5
    assert any(p.source_object_id == "migrated-from:shared-legacy"
               for p in by_id["mig-1"].provenance)

    # idempotent re-run: merged, not duplicated
    res2 = copy_group_records(src_ws, "shared-legacy", tgt_ws, "shared-new")
    assert res2["copied"] == 0 and res2["updated"] == 3
    assert len(SharedMemoryStore(tgt_ws, "shared-new").list_records()) == 3


def test_bind_agents_to_group_auto_new_group_fails_closed_when_legacy_exists(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    legacy = SharedMemoryStore(ws, "shared-legacy")
    legacy.append_record(_record("r1", "data"))
    bind = AgentBindingStore(ws)
    with pytest.raises(ValueError, match="legacy_shared_group_data_detected"):
        bind.bind_agents_to_group(["a", "b", "c"])
    # Explicit group id + operator opt-in is allowed.
    ok = bind.bind_agents_to_group(["a", "b", "c"], share_group_id="shared-legacy",
                                   allow_empty_group_creation=True)
    assert ok["ok"]


def test_shared_memory_store_warns_but_does_not_raise_on_new_group(tmp_path, caplog):
    ws = tmp_path / "ws"
    ws.mkdir()
    SharedMemoryStore(ws, "shared-legacy").append_record(_record("r1", "data"))
    with caplog.at_level(logging.WARNING):
        SharedMemoryStore(ws, "shared-another-fresh")
    assert any("non-empty legacy groups" in r.message for r in caplog.records)
