from __future__ import annotations

import json
from pathlib import Path
import time

from memoryguard.access_context import AccessContext
from memoryguard.content.store import ContentStore
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context
from memoryguard.runtime_v2.import_control import ImportControlService


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="agent-a",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="import-control-session",
            session_source="transport",
            session_trusted=True,
        ),
        workspace_id=str(workspace.resolve()),
        share_group_id="group-a",
        project_ref=str(workspace.resolve()),
        provider="codex",
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


def _write_source_connector(workspace: Path, root_id: str, path: Path) -> None:
    ContentStore(workspace).upsert_source_connector(
        source_id=root_id,
        provider="memoryguard-gui",
        source_type="selected_directory",
        external_root_key=str(path.resolve()),
        workspace_id=str(workspace.resolve()),
        enabled=True,
    )


def _business(result: dict) -> dict:
    data = result.get("data")
    return data if isinstance(data, dict) else result


def _wait(port: NativeV2RuntimePort, context, run_id: str) -> dict:
    deadline = time.monotonic() + 10.0
    latest = {}
    while time.monotonic() < deadline:
        latest = port.dispatch_gui(
            "get_build_progress", [run_id],
            context=context, generation=11, state="V2_ACTIVE",
        )
        if latest.get("status") in {"succeeded", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    return latest


def test_import_control_service_syncs_generic_bundle_directly(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.md"
    bundle.write_text(
        "# Session\n\nUser prefers deterministic tests and bounded previews.",
        encoding="utf-8",
    )
    result = ImportControlService(tmp_path).import_bundle(
        str(bundle),
        scope={
            "workspace_id": str(tmp_path.resolve()),
            "agent_instance_id": "agent-a",
            "project_ref": str(tmp_path.resolve()),
            "share_group_id": "group-a",
            "provider": "codex",
            "sensitivity": "normal",
            "policy_class": "private",
        },
    )
    assert result["status"] == "succeeded"
    assert result["turn_count"] >= 1
    assert result["memory_record_count"] == 0


def test_gui_bundle_preview_and_import_write_only_content_plane(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.md"
    bundle.write_text(
        "# Session\n\nUser prefers deterministic tests and bounded previews.",
        encoding="utf-8",
    )
    port = _port(tmp_path)
    context = _context(tmp_path)

    preview = port.dispatch_gui(
        "preview_import", [str(bundle)],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert preview["ok"] is True, preview
    preview_data = _business(preview)
    assert preview_data["provider"] == "generic"
    assert preview_data["writes_long_term_memory"] is False
    assert preview_data["inventory"]["file_count"] == 1

    accepted = port.dispatch_gui(
        "create_import", [str(bundle), True, "spoofed-agent", "spoofed-project", "spoofed-group"],
        context=context,
        generation=11,
        mutation=True,
        state="V2_ACTIVE",
    )
    assert accepted["ok"] is True, accepted
    assert accepted["writes_long_term_memory"] is False
    run_id = str((accepted.get("task") or {}).get("run_id") or "")
    assert run_id.startswith("gui-task-")
    final = _wait(port, context, run_id)
    assert final.get("error") in ({}, None), final.get("error")
    assert final["ok"] is True, final
    assert final["status"] == "succeeded", final
    result = final["result_ref"]
    assert result["memory_record_count"] == 0
    assert result["turn_count"] >= 1

    content_db = tmp_path / ".memoryguard" / "content" / "content.db"
    assert content_db.is_file()
    assert not (tmp_path / ".memoryguard" / "memory" / "memory.db").exists()
    import sqlite3
    with sqlite3.connect(content_db) as conn:
        sessions = conn.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]
        turns = conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()[0]
        providers = {
            row[0] for row in conn.execute("SELECT DISTINCT provider FROM conversation_sessions")
        }
        groups = {
            row[0] for row in conn.execute("SELECT DISTINCT share_group_id FROM conversation_sessions")
        }
    assert sessions >= 1 and turns >= 1
    # Authorization comes from the process-issued context, never positional spoofing.
    assert providers == {"codex"}
    assert groups == {"group-a"}
    port._task_service().shutdown(timeout=5.0)


def test_gui_source_summary_and_explicit_file_preview_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    note = source / "note.md"
    note.write_text("bounded source body", encoding="utf-8")
    _write_source_connector(tmp_path, "src-docs", source)
    port = _port(tmp_path)
    context = _context(tmp_path)

    summary = port.dispatch_gui(
        "get_raw_memory", [], context=context, generation=11, state="V2_ACTIVE"
    )
    assert summary["ok"] is True, summary
    summary_data = _business(summary)
    assert summary_data["groups"][0]["root_id"] == "src-docs"
    assert summary_data["groups"][0]["files"][0]["relative_path"] == "note.md"

    viewed = port.dispatch_gui(
        "get_source_file_content", ["src-docs", "note.md"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert viewed["ok"] is True, viewed
    viewed_data = _business(viewed)
    assert viewed_data["content"] == "bounded source body"
    assert viewed_data["read_only"] is True
    assert str(source) not in json.dumps(viewed, ensure_ascii=False)

    escaped = port.dispatch_gui(
        "get_source_file_content", ["src-docs", "../outside.md"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert escaped["ok"] is False
    assert escaped["code"] in {"relative_source_path_required", "path_out_of_scope"}


def test_bundle_preview_rejects_relative_and_symlink_paths(tmp_path: Path) -> None:
    port = _port(tmp_path)
    context = _context(tmp_path)
    relative = port.dispatch_gui(
        "preview_import", ["bundle.md"],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert relative["ok"] is False
    assert relative["code"] == "import_path_must_be_absolute"

    target = tmp_path / "real.md"
    target.write_text("body", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return
    blocked = port.dispatch_gui(
        "preview_import", [str(link)],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert blocked["ok"] is False
    assert blocked["code"] == "import_reparse_point_blocked"
