from __future__ import annotations

from pathlib import Path
import time

from memoryguard.access_context import AccessContext
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


def _port(workspace: Path) -> NativeV2RuntimePort:
    return NativeV2RuntimePort(
        workspace,
        state_provider=lambda: {"state": "V2_ACTIVE", "generation": 11},
    )


def test_native_gui_knowledge_add_positional_args_use_registry_and_taskrun(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "guide.md").write_text("# Guide\n\nNative V2 knowledge body", encoding="utf-8")
    port = _port(tmp_path)
    context = _context(tmp_path)

    accepted = port.dispatch_gui(
        "knowledge_add",
        [str(source), "Guide"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    assert accepted["operation"] == "knowledge_source_add"
    run_id = str(accepted["task"]["run_id"])
    assert run_id.startswith("gui-task-")

    deadline = time.monotonic() + 10.0
    latest = {}
    while time.monotonic() < deadline:
        latest = port.dispatch_gui(
            "knowledge_job_status",
            [run_id],
            context=context,
            generation=11,
            state="V2_ACTIVE",
        )
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.02)
    assert latest["ok"] is True, latest
    assert latest["status"] == "succeeded", latest

    deleted = port.dispatch_gui(
        "knowledge_deleted_list",
        [],
        context=context,
        generation=11,
        state="V2_ACTIVE",
    )
    assert deleted["ok"] is True
    assert deleted["data"]["total"] == 0

    port._task_service().shutdown(timeout=5.0)


def test_gui_knowledge_registry_has_no_retired_or_blocker(tmp_path: Path) -> None:
    port = _port(tmp_path)
    entries = port.coverage()["surfaces"]["gui"]["entries"]
    knowledge = [item for item in entries if item.get("domain") == "knowledge" or item["name"].startswith("knowledge_")]
    assert knowledge
    assert all(item["status"] == "implemented" for item in knowledge), knowledge
    assert all(item["status"] != "retired" for item in entries)
    port._task_service().shutdown(timeout=5.0)
