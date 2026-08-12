from __future__ import annotations

from pathlib import Path
import time

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="request-compat-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="gui",
        runtime_role="gui",
        entrypoint="gui",
        sensitivity="normal",
        policy_class="private",
    )


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def _wait(port: NativeV2RuntimePort, context, run_id: str) -> dict:
    deadline = time.monotonic() + 5.0
    latest = {}
    while time.monotonic() < deadline:
        latest = port.dispatch_gui(
            "get_request_status",
            [run_id],
            context=context,
            generation=11,
            state="V2_ACTIVE",
        )
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    return latest


def test_submit_request_wraps_sync_mutation_in_taskrun_without_request_queue(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)

    accepted = port.dispatch_gui(
        "submit_request",
        ["enter_multi_agent_mode", []],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    task = accepted.get("task") or accepted.get("data", {}).get("task")
    assert task and task["run_id"].startswith("gui-task-")

    final = _wait(port, context, task["run_id"])
    assert final["ok"] is True, final
    assert final["status"] == "succeeded", final
    assert final["operation"] == "request_mutation"
    assert not (tmp_path / ".memoryguard" / "requests").exists()
    assert not (tmp_path / ".memoryguard" / "requests.json").exists()
    port._task_service().shutdown(timeout=5.0)


def test_submit_request_reuses_native_task_instead_of_nesting(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)

    accepted = port.dispatch_gui(
        "submit_request",
        ["start_build_projection", [True, "reconstructed", None, "", "", "", "", "auto"]],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    task = accepted.get("task") or accepted.get("data", {}).get("task")
    assert task and task["run_id"].startswith("gui-task-")
    # The run is the projection task itself, not a request_mutation wrapper.
    latest = _wait(port, context, task["run_id"])
    assert latest["operation"] == "projection_build"
    assert not (tmp_path / ".memoryguard" / "requests").exists()
    port._task_service().shutdown(timeout=5.0)


def test_submit_request_rejects_reads_and_recursive_targets(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)
    for method in ("get_audit", "submit_request", "request_mutation"):
        result = port.dispatch_gui(
            "submit_request",
            [method, []],
            context=context,
            generation=11,
            mutation=True,
            state="V2_ACTIVE",
        )
        assert result["ok"] is False, (method, result)
        assert result["code"] in {"request_target_not_mutation", "request_target_recursive"}
