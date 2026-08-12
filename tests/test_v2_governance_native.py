from __future__ import annotations

from pathlib import Path
import sqlite3

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom
from memoryguard.runtime_v2.governance_native import GovernanceNativeService
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _mutation_context(workspace: Path) -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="agent-a",
        authority="admin",
        admin=True,
    )


def _native_context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="governance-native-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-governance-native",
        sensitivity="normal",
        policy_class="private",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _seed(workspace: Path) -> GovernanceV2:
    boundary = GovernanceV2(workspace)
    context = _mutation_context(workspace)
    for memory_id, body, metadata in (
        ("quarantine-me", "private governed memory", {}),
        (
            "conflict-keep",
            "preferred fact",
            {
                "conflict_group_id": "conflict-group-1",
                "conflict_status": "unresolved",
                "conflict_reason": "same logical fact disagrees",
            },
        ),
        (
            "conflict-drop",
            "stale fact",
            {
                "conflict_group_id": "conflict-group-1",
                "conflict_status": "unresolved",
                "conflict_reason": "same logical fact disagrees",
            },
        ),
    ):
        boundary.put_atom(
            MemoryAtom(
                memory_id=memory_id,
                body=body,
                share_group_id="group-a",
                agent_instance_id="agent-a",
                project_ref=str(workspace.resolve()),
                provider="gui",
                runtime_role="gui",
                metadata=metadata,
            ),
            context=context,
            evidence=[{
                "source_ref": f"test:{memory_id}",
                "digest": f"digest-{memory_id}",
            }],
            reason=f"seed {memory_id}",
        )
    return boundary


def test_governance_read_queries_are_zero_write_without_v2_state(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _native_context(tmp_path)
    before = list(tmp_path.rglob("*"))

    for name in (
        "get_recent_events",
        "get_auto_actions",
        "get_conflicts",
        "get_quarantine",
        "get_supersede_decisions",
        "get_memory_ir",
    ):
        result = port.dispatch_gui(
            name,
            [],
            context=context,
            generation=11,
            state="V2_ACTIVE",
        )
        assert result["ok"] is True, (name, result)

    assert list(tmp_path.rglob("*")) == before
    assert not (tmp_path / ".memoryguard").exists()


def test_governance_native_quarantine_conflict_and_decision_outbox(tmp_path: Path) -> None:
    boundary = _seed(tmp_path)
    port = _port(tmp_path)
    context = _native_context(tmp_path)

    quarantined = port.dispatch_gui(
        "neuron_decide",
        ["quarantine-me", "quarantine", "manual review", True, None, "", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert quarantined["ok"] is True, quarantined
    assert quarantined["data"]["memory_status"] == "quarantined"

    queue = port.dispatch_gui(
        "get_quarantine", ["group-a"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert queue["ok"] is True, queue
    assert queue["data"]["total"] == 1
    entry = queue["data"]["quarantine"][0]
    assert entry["memory_id"] == "quarantine-me"
    assert "private governed memory" not in entry["masked_preview"]

    released = port.dispatch_gui(
        "release_quarantine", [entry["quarantine_id"], "group-a"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert released["ok"] is True, released
    assert port.dispatch_gui(
        "get_quarantine", [], context=context, generation=11, state="V2_ACTIVE"
    )["data"]["total"] == 0

    conflicts = port.dispatch_gui(
        "get_conflicts", ["group-a"], context=context, generation=11, state="V2_ACTIVE"
    )
    assert conflicts["ok"] is True, conflicts
    assert conflicts["data"]["conflicts"] == [{
        "group_id": "conflict-group-1",
        "member_ids": ["conflict-drop", "conflict-keep"],
        "status": "unresolved",
        "reason": "same logical fact disagrees",
        "created_at": conflicts["data"]["conflicts"][0]["created_at"],
    }]

    resolved = port.dispatch_gui(
        "resolve_conflict", ["conflict-group-1", "conflict-keep", "group-a"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert resolved["ok"] is True, resolved
    assert resolved["data"]["deleted_memory_ids"] == ["conflict-drop"]
    assert port.dispatch_gui(
        "get_conflicts", [], context=context, generation=11, state="V2_ACTIVE"
    )["data"]["total"] == 0

    memory = boundary.memory
    read_scope = _mutation_context(tmp_path).to_dict()
    kept = memory.get_atom("conflict-keep", scope=read_scope, include_building=True)
    dropped = memory.get_atom("conflict-drop", scope=read_scope, include_building=True)
    released_atom = memory.get_atom("quarantine-me", scope=read_scope, include_building=True)
    assert kept is not None and kept.metadata.get("conflict_status") == "resolved"
    assert dropped is not None and dropped.status == "deleted"
    assert released_atom is not None and released_atom.status == "active"

    recent = port.dispatch_gui(
        "get_recent_events", [], context=context, generation=11, state="V2_ACTIVE"
    )
    assert recent["ok"] is True, recent
    actions = [item["action"] for item in recent["data"]["events"]]
    assert "put" in actions
    assert "tombstone" in actions

    service = GovernanceNativeService(tmp_path)
    outbox = service.outbox_status(context)
    assert outbox["ok"] is True
    assert outbox["outbox"].get("pending", 0) >= 1

    with sqlite3.connect(service._decision_ledger_path) as conn:
        decision_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        outbox_count = conn.execute("SELECT COUNT(*) FROM decision_outbox").fetchone()[0]
    assert decision_count == outbox_count
