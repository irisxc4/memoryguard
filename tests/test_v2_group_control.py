from __future__ import annotations

from pathlib import Path

import pytest

from memoryguard.runtime_v2.group_native import GroupControlError, GroupControlService, personal_group_id
from memoryguard.storage.layout import WorkspaceV2Layout


def _put_v2_atom(tmp_path: Path, group: str, memory_id: str, body: str, *, status: str = "active") -> None:
    from memoryguard.governance_v2 import V2MutationContext
    from memoryguard.memory import MemoryAtom, MemoryAtomStore
    from memoryguard.runtime_v2.dedup import canonical_hash

    store = MemoryAtomStore(tmp_path)
    store.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=body,
            status=status,
            canonical_hash=canonical_hash(body),
            workspace_id=str(tmp_path.resolve()),
            share_group_id=group,
            visibility="active",
        ),
        context=V2MutationContext(
            workspace_id=str(tmp_path.resolve()),
            share_group_id=group,
            actor="v2-group-test",
            admin=True,
            authority="manual",
        ),
    )


def test_v2_group_binding_is_idempotent_and_has_one_active_binding(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    first = service.bind_agent("agent-a", "shared-one")
    assert first["changed"] is True
    repeated = service.bind_agent("agent-a", "shared-one")
    assert repeated["changed"] is False
    assert repeated["binding_id"] == first["binding_id"]

    moved = service.bind_agent("agent-a", "shared-two")
    assert moved["changed"] is True
    active = service.list_bindings(include_inactive=False)["bindings"]
    assert len(active) == 1
    assert active[0]["share_group_id"] == "shared-two"
    history = service.list_bindings(include_inactive=True)["bindings"]
    assert len(history) == 2


def test_personal_group_is_stable_and_shared_binding_is_not_silently_replaced(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    group = personal_group_id("agent-a")
    first = service.ensure_personal("agent-a")
    assert first["share_group_id"] == group
    second = service.ensure_personal("agent-a")
    assert second["changed"] is False

    service.bind_agent("agent-a", "shared-a")
    preserved = service.ensure_personal("agent-a")
    assert preserved["share_group_id"] == "shared-a"
    assert preserved["changed"] is False
    left = service.leave_to_personal("agent-a")
    assert left["share_group_id"] == group
    assert left["previous_group_id"] == "shared-a"


def test_batch_shared_binding_is_atomic_and_duplicate_safe(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    first = service.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    assert first["member_count"] == 2
    second = service.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    assert second["changed"] is False
    preview = service.group_preview("shared-team")
    assert preview["members"] == ["agent-a", "agent-b"]
    with pytest.raises(GroupControlError, match="shared_group_requires_at_least_two_agents"):
        service.bind_agents(["agent-a"], share_group_id="bad")
    assert service.group_preview("bad")["member_count"] == 0


def test_scope_preference_is_binding_backed_and_non_admin_cannot_select_other_group(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    service.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    own = service.set_scope("agent-a", {"mode": "share_group", "share_group_id": "shared-team"})
    assert own["scope"]["share_group_id"] == "shared-team"
    with pytest.raises(GroupControlError, match="governance_scope_forbidden"):
        service.set_scope("agent-a", {"mode": "share_group", "share_group_id": "other"})
    state = service.scope_state("agent-a")
    assert state["scope"]["share_group_id"] == "shared-team"


def test_dissolve_deactivates_members_and_preserves_memory_domain(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    service.bind_agents(["agent-a", "agent-b"], share_group_id="shared-team")
    memory_path = WorkspaceV2Layout(tmp_path).memory_db
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_bytes(b"do-not-delete")
    dissolved = service.dissolve("shared-team")
    assert dissolved["unbound_count"] == 2
    assert service.group_preview("shared-team")["member_count"] == 0
    assert memory_path.read_bytes() == b"do-not-delete"


def test_drift_is_read_only_and_does_not_change_binding_status(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    result = service.bind_agent("agent-a", "shared-team", redirect_paths=[str(tmp_path / "missing")])
    drift = service.check_drift(result["binding_id"])
    assert drift["drifted"] is True
    row = service.list_bindings(include_inactive=False)["bindings"][0]
    assert row["status"] == "active"


def test_group_control_has_no_legacy_store_imports() -> None:
    source = Path("src/memoryguard/runtime_v2/group_native.py").read_text(encoding="utf-8")
    assert "from ..agent_binding import" not in source
    assert "from ..shared_memory_store import" not in source
    assert "import memoryguard.agent_binding" not in source
    assert "import memoryguard.shared_memory_store" not in source


def test_v2_group_aggregate_empty_library_is_stable(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=False)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    result = service.aggregate_groups()

    assert result["ok"] is True
    assert result["groups"] == []
    assert result["total_groups"] == 0
    assert result["total_records"] == 0
    assert result["active_records"] == 0
    assert result["deleted_count"] == 0
    assert result["conflict_count"] == 0
    assert result["quarantined_count"] == 0
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_v2_group_aggregate_counts_members_and_records_across_groups(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=True)
    service.bind_agent("agent-a", "group-a")
    service.bind_agent("agent-b", "group-b")
    _put_v2_atom(tmp_path, "group-a", "memory-a1", "group A active")
    _put_v2_atom(tmp_path, "group-a", "memory-a2", "group A deleted", status="deleted")
    _put_v2_atom(tmp_path, "group-b", "memory-b1", "group B active")

    result = service.aggregate_groups()
    groups = {item["share_group_id"]: item for item in result["groups"]}

    assert result["total_groups"] == 2
    assert result["total_records"] == 3
    assert result["active_records"] == 2
    assert result["deleted_count"] == 1
    assert groups["group-a"]["members"] == ["agent-a"]
    assert groups["group-a"]["record_count"] == 2
    assert groups["group-a"]["active_records"] == 1
    assert groups["group-a"]["deleted_count"] == 1
    assert groups["group-b"]["members"] == ["agent-b"]
    assert groups["group-b"]["record_count"] == 1
    assert groups["group-a"]["active_version"]


def test_v2_group_aggregate_read_is_non_creating(tmp_path: Path) -> None:
    service = GroupControlService(tmp_path, write=False)
    assert not (tmp_path / ".memoryguard").exists()

    first = service.aggregate_groups()
    second = service.list_share_groups()

    assert first == second
    assert not (tmp_path / ".memoryguard").exists()


def test_v2_global_status_finds_cross_group_duplicate_without_body_on_non_ascii_path(tmp_path: Path) -> None:
    workspace = tmp_path / "记忆库-只读聚合"
    workspace.mkdir()
    service = GroupControlService(workspace, write=True)
    service.bind_agent("agent-a", "group-a")
    service.bind_agent("agent-b", "group-b")
    _put_v2_atom(workspace, "group-a", "memory-a", "跨组相同内容")
    _put_v2_atom(workspace, "group-b", "memory-b", "跨组相同内容")
    before = {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    readonly = GroupControlService(workspace, write=False)

    result = readonly.get_global_memory_status()

    assert result["total_groups"] == 2
    assert result["total_records"] == 2
    assert len(result["cross_group_duplicates"]) == 1
    duplicate = result["cross_group_duplicates"][0]
    assert duplicate["match_type"] == "exact"
    assert duplicate["share_group_ids"] == ["group-a", "group-b"]
    assert duplicate["record_count"] == 2
    assert duplicate["canonical_hash"]
    assert all("body" not in duplicate for _ in [0])
    assert all("body" not in record for record in duplicate["records"])
    after = {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert after == before, sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
