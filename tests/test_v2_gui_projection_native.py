from __future__ import annotations

import time
from pathlib import Path

from memoryguard.access_context import AccessContext
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory.store import MemoryAtom, MemoryAtomStore
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    access = AccessContext(
        trusted_agent_id="agent-a",
        is_admin=True,
        strict_binding=True,
        allow_anon=False,
        session_id="gui-session",
        session_source="transport",
        session_trusted=True,
    )
    return bind_native_transport_context(
        access,
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        namespace_id="knowledge-native-namespace",
        sensitivity="normal",
        policy_class="private",
    )


def _seed(workspace: Path) -> None:
    memory = MemoryAtomStore(workspace, readonly=False)
    governance = GovernanceV2(workspace, memory_store=memory)
    ctx = V2MutationContext(
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        agent_instance_id="agent-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        actor="test",
    )
    evidence, _ = governance.put_evidence(
        context=ctx,
        reason="projection native fixture",
        source_ref="fixture:projection",
        digest="a" * 64,
        authority="governance",
    )
    atom, _ = governance.put_atom(
        MemoryAtom(
            memory_id="native-m1",
            body="native private body",
            workspace_id=str(workspace.resolve()),
            share_group_id="group-a",
            agent_instance_id="agent-a",
            project_ref=str(workspace.resolve()),
            provider="gui",
            runtime_role="gui",
        ),
        context=ctx,
        evidence=[evidence.to_dict()],
        reason="projection native fixture atom",
        idempotency_key="projection-native-fixture",
    )
    for _ in range(4):
        state = memory.project_evidence(governance.evidence)
        if int(state.get("pending", 0)) == 0:
            break
    assert not memory.pending_outbox(include_failed=True)
    memory.set_visibility("active", atom_ids=[atom.atom_id])


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _wait(port: NativeV2RuntimePort, context, run_id: str) -> dict:
    latest: dict = {}
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = port.dispatch_gui(
            "get_build_progress", [run_id], context=context, generation=11, state="V2_ACTIVE"
        )
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    return latest


def test_native_projection_build_and_release_transport(tmp_path: Path) -> None:
    _seed(tmp_path)
    port = _port(tmp_path)
    context = _context(tmp_path)

    accepted = port.dispatch_gui(
        "start_build_projection",
        [True, "reconstructed", {}, "agent-a", "group-a", "", "", "auto"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    assert accepted["operation"] == "projection_build"
    run_id = str(accepted["task"]["run_id"])
    final = _wait(port, context, run_id)
    assert final["status"] == "succeeded", final

    plan = port.dispatch_gui(
        "create_build_plan",
        ["published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert plan["ok"] is True, plan
    plan_id = str(plan["data"]["plan_id"] if "data" in plan else plan["plan_id"])

    apply = port.dispatch_gui(
        "apply_build",
        [plan_id, True, "published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert apply["ok"] is True, apply
    apply_run = str(apply["task"]["run_id"])
    applied = _wait(port, context, apply_run)
    assert applied["status"] == "succeeded", applied

    releases = port.dispatch_gui(
        "list_native_memory_releases", [{}, "agent-a"],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert releases["ok"] is True, releases
    rows = releases.get("releases") or releases.get("data", {}).get("releases") or []
    assert len(rows) == 1
    release_id = rows[0]["release_id"]

    verified = port.dispatch_gui(
        "verify_release",
        [release_id, "published/native.json", {}, "agent-a", ""],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert verified["ok"] is True, verified

    rolled = port.dispatch_gui(
        "rollback_native_memory_release",
        [release_id, False, True, {}, "agent-a", ""],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert rolled["ok"] is True, rolled
    assert not (tmp_path / "published" / "native.json").exists()
    port._task_service().shutdown(timeout=5.0)


def test_projection_gui_registry_is_implemented(tmp_path: Path) -> None:
    port = _port(tmp_path)
    entries = port.coverage()["surfaces"]["gui"]["entries"]
    names = {
        "get_projection_source_map", "get_build_progress", "build_projection",
        "start_build_projection", "cancel_build_projection", "delete_projection",
        "set_projection_source_enabled", "create_build_plan", "apply_build",
        "publish_reconstructed_memory", "verify_release", "rollback_release",
        "rollback_native_memory_release", "list_native_memory_releases",
        "list_publish_targets", "list_releases", "choose_publish_target_path",
    }
    selected = [item for item in entries if item["name"] in names]
    assert {item["name"] for item in selected} == names
    assert all(item["status"] == "implemented" for item in selected), selected
