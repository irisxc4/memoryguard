from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import time

from memoryguard.access_context import AccessContext
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.runtime_v2.history_control import HistoryControlService, discover_sources, parse_source
from memoryguard.runtime_v2.native_ports import NativeV2RuntimePort, bind_native_transport_context


def _context(workspace: Path):
    return bind_native_transport_context(
        AccessContext(
            trusted_agent_id="codex-agent",
            is_admin=True,
            strict_binding=True,
            allow_anon=False,
            session_id="history-control-session",
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


def _write_codex_session(home: Path) -> Path:
    root = home / ".codex" / "sessions" / "2026" / "08"
    root.mkdir(parents=True)
    path = root / "rollout.jsonl"
    rows = [
        {
            "type": "session_meta",
            "payload": {
                "id": "session-a",
                "title": "History sample",
                "cwd": "project-a",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "role": "user",
                "type": "message",
                "content": [{"type": "input_text", "text": "Remember bounded parsing."}],
                "id": "msg-user",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "role": "assistant",
                "type": "message",
                "content": [{"type": "output_text", "text": "Acknowledged."}],
                "id": "msg-assistant",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "role": "assistant",
                "type": "reasoning",
                "content": [{"type": "reasoning", "text": "private reasoning must not import"}],
                "id": "reasoning",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _write_many_codex_sessions(home: Path, count: int) -> None:
    root = home / ".codex" / "sessions" / "2026" / "08"
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        session_id = f"session-{index:03d}"
        rows = [
            {"type": "session_meta", "payload": {"id": session_id, "title": session_id, "cwd": f"project-{index % 3}"}},
            {"type": "response_item", "payload": {"role": "user", "type": "message", "content": [{"type": "input_text", "text": f"message {index}"}], "id": f"msg-{index}"}},
        ]
        (root / f"rollout-{index:03d}.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n",
            encoding="utf-8",
        )


def _patch_codex_agent(monkeypatch, workspace: Path) -> None:
    GroupControlService(workspace, write=True).bind_agent("codex-agent", "group-a")
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([SimpleNamespace(instance_id="codex-agent", product="codex")], {}),
    )


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


def test_history_discovery_is_metadata_only_and_parser_excludes_reasoning(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    source_path = _write_codex_session(home)
    sources = discover_sources(home)
    assert len(sources) == 1
    source = sources[0]
    assert source.provider == "codex" and source.supported
    parsed = parse_source(source)
    assert parsed.external_id == "session-a"
    assert [message["role"] for message in parsed.messages] == ["user", "assistant"]
    encoded = json.dumps([dict(message) for message in parsed.messages])
    assert "private reasoning" not in encoded
    assert source_path.name not in source.source_id


def test_gui_history_discover_and_backfill_write_content_plane_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    source_path = _write_codex_session(home)
    monkeypatch.setenv("USERPROFILE", str(home))
    _patch_codex_agent(monkeypatch, workspace)
    port = _port(workspace)
    context = _context(workspace)

    discovered = port.dispatch_gui(
        "discover_local_history_sources", [],
        context=context, generation=11, state="V2_ACTIVE",
    )
    assert discovered["ok"] is True, discovered
    data = discovered.get("data", discovered)
    assert len(data["sources"]) == 1
    item = data["sources"][0]
    assert item["provider"] == "codex"
    assert item["status"] == "importable"
    assert item["matched_agent_id"] == "codex-agent"
    assert str(source_path) not in json.dumps(discovered, ensure_ascii=False)

    accepted = port.dispatch_gui(
        "backfill_local_history", [None],
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
    assert final["ok"] is True, final
    assert final["status"] == "succeeded", final
    assert final["result_ref"]["session_count"] == 1
    assert final["result_ref"]["turn_count"] == 2
    assert final["result_ref"]["memory_record_count"] == 0

    content_db = workspace / ".memoryguard" / "content" / "content.db"
    assert content_db.is_file()
    assert not (workspace / ".memoryguard" / "history" / "history.sqlite").exists()
    assert not (workspace / ".memoryguard" / "memory" / "memory.db").exists()
    import sqlite3
    with sqlite3.connect(content_db) as conn:
        sessions = conn.execute(
            "SELECT external_id,provider,agent_instance_id,share_group_id FROM conversation_sessions"
        ).fetchall()
        turns = conn.execute(
            "SELECT role FROM conversation_turns ORDER BY ordinal"
        ).fetchall()
        bodies = [row[0] for row in conn.execute(
            "SELECT b.text FROM content_blobs b JOIN content_occurrences o ON o.blob_id=b.blob_id ORDER BY o.ordinal"
        ).fetchall()]
    assert sessions == [("session-a", "codex", "codex-agent", "group-a")]
    assert turns == [("user",), ("assistant",)]
    assert bodies == ["Remember bounded parsing.", "Acknowledged."]
    port._task_service().shutdown(timeout=5.0)


def test_v2_history_backfill_advances_across_multiple_bounded_batches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_many_codex_sessions(home, 30)
    _patch_codex_agent(monkeypatch, workspace)
    service = HistoryControlService(workspace, home=home)

    first = service.backfill()
    assert first["imported"] == 25
    assert first["processed_files"] == 25
    assert first["remaining_fresh_files"] == 5
    assert first["continuation"]

    second = service.backfill(continuation=first["continuation"])
    assert second["imported"] == 5
    assert second["processed_files"] == 5
    assert second["remaining_fresh_files"] == 0
    assert second["continuation"] is None
    assert second["skipped"] == 25

    replay = service.backfill()
    assert replay["imported"] == 0
    assert replay["processed_files"] == 0
    assert replay["skipped"] == 30
    assert replay["remaining_fresh_files"] == 0


def test_history_backfill_without_provider_binding_is_neutral_no_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    _write_codex_session(home)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        "memoryguard.agent_locator.AgentLocator.detect_instances",
        lambda self: ([SimpleNamespace(instance_id="codex-agent", product="codex")], {}),
    )
    service = HistoryControlService(workspace)
    discovered = service.discover()
    assert discovered["sources"][0]["status"] == "pending_binding"
    result = service.backfill()
    assert result["pending_binding"] == ["codex"]
    assert result["imported"] == 0
    assert not (workspace / ".memoryguard" / "content" / "content.db").exists()
