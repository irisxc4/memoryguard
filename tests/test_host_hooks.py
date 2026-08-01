import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.cli import main as cli_main
from memoryguard.host_hooks import (
    HostHookManager,
    _state_path,
    _flush_pending_rule_feedback,
    _save_state,
    run_hook,
    set_hook_mode,
)
from memoryguard.provider_adapters import CodexAdapter
from memoryguard.schema_v3 import (
    MemoryKind,
    SharedMemoryRecord,
    SharedMemoryStatus,
    RuleMatchFeedback,
    RuleMatchReceipt,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _bind(workspace: Path, agent_id: str, group_id: str) -> None:
    AgentBindingStore(workspace).bind_agent(agent_id, group_id)


def _record(memory_id: str, body: str, kind: MemoryKind) -> SharedMemoryRecord:
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=body,
        kind=kind,
        status=SharedMemoryStatus.ACTIVE,
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        agent_instance_id="source-agent",
    )


@pytest.mark.parametrize(
    ("provider", "relative_config", "event_name"),
    [
        ("claude", ".claude/settings.json", "UserPromptSubmit"),
        ("codex", ".codex/hooks.json", "UserPromptSubmit"),
        ("cursor", ".cursor/hooks.json", "beforeSubmitPrompt"),
    ],
)
def test_hook_install_is_idempotent_and_preserves_other_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    relative_config: str,
    event_name: str,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, f"{provider}-agent", "group-a")

    config_path = home / relative_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if provider == "cursor":
        original = {
            "version": 1,
            "hooks": {
                "stop": [{"command": "python user-stop.py"}],
            },
        }
    else:
        original = {
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "type": "command",
                        "command": "python user-stop.py",
                    }],
                }],
            },
        }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    manager = HostHookManager(workspace)
    first = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )
    second = manager.install(
        provider,
        agent_instance_id=f"{provider}-agent",
        share_group_id="group-a",
    )

    data = json.loads(config_path.read_text(encoding="utf-8"))
    serialized = json.dumps(data)
    assert first["configured"] and second["configured"]
    expected_handlers = 6 if provider == "cursor" else 7
    if provider == "cursor":
        owned_handlers = [
            entry
            for entries in data["hooks"].values()
            for entry in entries
            if "memoryguard.host_hooks" in entry.get("command", "")
        ]
    else:
        owned_handlers = [
            handler
            for groups in data["hooks"].values()
            for group in groups
            for handler in group.get("hooks", [])
            if "memoryguard.host_hooks" in handler.get("command", "")
        ]
    assert len(owned_handlers) == expected_handlers
    assert serialized.count("python user-stop.py") == 1
    assert event_name in data["hooks"]

    removed = manager.uninstall(provider)
    remaining = json.loads(config_path.read_text(encoding="utf-8"))
    remaining_text = json.dumps(remaining)
    assert removed["configured"] is False
    assert "memoryguard.host_hooks" not in remaining_text
    assert remaining_text.count("python user-stop.py") == 1


def test_codex_hook_config_uses_utf8_commands_for_each_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")

    HostHookManager(workspace).install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
    )

    data = json.loads(
        (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )
    limited_events = set()
    for event_name, groups in data["hooks"].items():
        handler = groups[0]["hooks"][0]
        assert "-X utf8" in handler["command"]
        assert "-X utf8" in handler["commandWindows"]
        if "additionalContextLimit" in handler:
            limited_events.add(event_name)
    assert limited_events == {
        "SessionStart",
        "SubagentStart",
        "UserPromptSubmit",
    }


def test_hook_cli_forces_utf8_stdio_when_windows_defaults_to_gbk(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    preference = "\u7528\u6237\u957f\u671f\u504f\u597d\uff1a\u56de\u7b54\u4fdd\u6301\u7b80\u6d01"
    prompt = "\u8bf7\u8bfb\u53d6\u4e2d\u6587\u957f\u671f\u504f\u597d"
    SharedMemoryStore(workspace, "group-a").append_record(_record(
        "pref",
        preference,
        MemoryKind.PREFERENCE,
    ))
    payload = {
        "session_id": "session-utf8",
        "turn_id": "turn-utf8",
        "prompt": prompt,
        "cwd": str(workspace),
    }
    env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH", "")) if part
    )
    env["PYTHONIOENCODING"] = "gbk"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "memoryguard.host_hooks",
            "run",
            "--provider",
            "codex",
            "--event",
            "user_prompt",
            "--workspace",
            str(workspace),
            "--agent-id",
            "codex-agent",
            "--share-group-id",
            "group-a",
        ],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )
    result = json.loads(completed.stdout.decode("utf-8"))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert prompt not in context
    assert "\u56de\u7b54\u4fdd\u6301\u7b80\u6d01" in context


def test_runtime_receipt_is_bound_to_exact_hook_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")
    manager = HostHookManager(workspace)
    manager.install(
        "codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
    )

    run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    verified = manager.status("codex", agent_instance_id="codex-agent")
    assert verified["status"] == "operational"
    assert verified["runtime_verified"] is True
    inferred = manager.status("codex")
    assert inferred["status"] == "operational"
    assert inferred["agent_instance_id"] == "codex-agent"

    config_path = home / ".codex" / "hooks.json"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    handler = data["hooks"]["PreToolUse"][0]["hooks"][0]
    handler["commandWindows"] += " --changed"
    config_path.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )

    stale = manager.status("codex", agent_instance_id="codex-agent")
    assert stale["configured"] is True
    assert stale["status"] == "configured_pending_runtime"
    assert stale["runtime_verified"] is False


def test_trae_reports_verified_fallback_instead_of_writing_fake_hook(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    result = HostHookManager(workspace).install(
        "trae",
        agent_instance_id="trae-agent",
        share_group_id="group-a",
    )
    assert result["status"] == "unsupported"
    assert result["configured"] is False
    assert result["capability"]["context_mode"] == "mcp_and_rules_only"


def test_user_prompt_injects_bounded_context_and_receipt_has_no_raw_prompt(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(_record(
        "pref",
        "用户长期偏好：回答保持简洁",
        MemoryKind.PREFERENCE,
    ))
    store.append_record(_record(
        "project",
        "MemoryGuard 项目默认使用 RTK 运行测试",
        MemoryKind.PROJECT,
    ))
    prompt = "检查 MemoryGuard 项目的 RTK 测试规则"

    result = run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "turn_id": "turn-1",
            "prompt": prompt,
            "cwd": str(workspace),
        },
    )

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "回答保持简洁" in context
    assert "默认使用 RTK" in context
    state_files = list(
        (workspace / ".memoryguard" / "hook-runtime" / "state").rglob("*.json")
    )
    assert state_files
    assert prompt not in state_files[0].read_text(encoding="utf-8")


def test_codex_defers_precompact_reminder_to_compact_session_start(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    SharedMemoryStore(workspace, "group-a").append_record(_record(
        "pref",
        "用户长期偏好：回答保持简洁",
        MemoryKind.PREFERENCE,
    ))
    session_id = "session-compact"

    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "turn_id": "turn-1",
            "prompt": "以后默认使用结构化输出",
            "cwd": str(workspace),
        },
    )
    before = run_hook(
        provider="codex",
        event="pre_compact",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "trigger": "auto"},
    )
    resumed = run_hook(
        provider="codex",
        event="session_start",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "source": "compact"},
    )

    assert before == {}
    context = resumed["hookSpecificOutput"]["additionalContext"]
    assert "memoryguard_memory_write" in context
    assert "压缩前" in context


def test_pre_tool_blocks_native_memory_write_but_allows_project_file(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    base = {
        "session_id": "session-1",
        "tool_name": "Write",
    }

    denied = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {
                "file_path": str(
                    tmp_path / "home" / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    allowed = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            **base,
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )

    assert (
        denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    )
    assert allowed == {}


def test_cursor_requires_bootstrap_before_first_tool_and_stop_continues_once(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "cursor-agent", "group-a")
    prompt_payload = {
        "conversation_id": "conversation-1",
        "generation_id": "generation-1",
        "prompt": "以后默认使用 RTK",
    }
    assert run_hook(
        provider="cursor",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload=prompt_payload,
    ) == {"continue": True}

    blocked = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert blocked["permission"] == "deny"
    assert "memoryguard_context_bootstrap" in blocked["agent_message"]

    run_hook(
        provider="cursor",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "MCP:memoryguard_context_bootstrap",
        },
    )
    allowed = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert allowed == {}

    first_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 0},
    )
    second_stop = run_hook(
        provider="cursor",
        event="stop",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={"conversation_id": "conversation-1", "loop_count": 1},
    )
    assert "memoryguard_memory_write" in first_stop["followup_message"]
    assert second_stop == {}

    subagent_block = run_hook(
        provider="cursor",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="cursor-agent",
        share_group_id="group-a",
        payload={
            "conversation_id": "conversation-1",
            "subagent_id": "subagent-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(workspace / "README.md")},
        },
    )
    assert subagent_block["permission"] == "deny"
    assert "memoryguard_context_bootstrap" in subagent_block["agent_message"]


def test_global_provider_install_includes_hook_without_duplicate_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")

    adapter = CodexAdapter(workspace)
    first = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )
    second = adapter.install(
        workspace,
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        global_scope=True,
    )

    hooks = json.loads(
        (home / ".codex" / "hooks.json").read_text(encoding="utf-8")
    )["hooks"]
    assert first["hook_configured"] is True
    assert second["hook_configured"] is True
    owned_handlers = [
        handler
        for groups in hooks.values()
        for group in groups
        for handler in group.get("hooks", [])
        if "memoryguard.host_hooks" in handler.get("command", "")
    ]
    assert len(owned_handlers) == 7


def test_paused_mode_is_emergency_bypass_for_tool_guard(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    set_hook_mode(workspace, "codex", "codex-agent", "paused")

    result = run_hook(
        provider="codex",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(
                    tmp_path / ".codex" / "memories" / "MEMORY.md"
                ),
            },
        },
    )
    assert result == {}


def test_history_without_stable_event_id_preserves_legitimate_repeats_and_marks_degraded(tmp_path: Path):
    from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    payload = {"session_id": "repeat-session", "prompt": "same legitimate prompt", "cwd": str(workspace)}
    for _ in range(2):
        run_hook(provider="codex", event="user_prompt", workspace=workspace,
                 agent_instance_id="codex-agent", share_group_id="group-a", payload=payload)
    scope = HistoryScope(agent_instance_id="codex-agent", project_ref=str(workspace),
                         provider="codex", share_group_id="group-a")
    session = ConversationHistoryStore(workspace).list_sessions(scope)["sessions"][0]
    raw = ConversationHistoryStore(workspace).read(scope, session_id=session["session_id"])
    assert [turn["content"] for turn in raw["turns"]] == ["same legitimate prompt"] * 2
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    history = json.loads(receipt.read_text(encoding="utf-8"))["history_archive"]
    assert history["coverage_degraded"] is True
    assert history["idempotency"] == "degraded"


def test_post_tool_memory_write_success_marks_write_seen(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-success"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
            "tool_result": {"isError": False, "ok": True, "memory_id": "m-1"},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is True
    assert state.get("write_failed") is None


def test_post_tool_memory_write_failure_keeps_write_seen_false(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-fail"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
            "tool_result": {"isError": True, "error": "write failed"},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is False
    assert state["write_failed"] is True
    assert state["write_error"]


def test_post_tool_memory_write_without_tool_result_does_not_auto_mark_success(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    session_id = "post-tool-unknown"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "turn_id": "turn-1", "prompt": "测试", "cwd": str(workspace)},
    )

    run_hook(
        provider="codex",
        event="post_tool",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "tool_name": "memoryguard_memory_write",
            "tool_input": {},
        },
    )

    state = json.loads(
        _state_path(workspace, "codex", session_id).read_text(encoding="utf-8")
    )
    assert state["write_seen"] is False
    assert state.get("write_failed") is None


def test_stop_flushes_pending_mandatory_rule_feedback(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(
        SharedMemoryRecord(
            memory_id="always",
            body="记住：测试项目下禁止持久记录未脱敏内容。",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            agent_instance_id="codex-agent",
        )
    )
    store.set_rule_assignments("always", [{
        "target_type": "agent",
        "target_id": "codex-agent",
    }])
    session_id = "feedback-session"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "turn_id": "turn-1",
            "prompt": "请检查项目代码。",
            "cwd": str(workspace),
        },
    )

    state_path = _state_path(workspace, "codex", session_id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    receipts = state.get("mandatory_match_receipts", [])
    assert len(receipts) == 1
    receipt_id = str(receipts[0].get("receipt_id"))
    assert store.get_rule_match_feedback_by_receipt(receipt_id) is None

    stop_result = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "loop_count": 0},
    )
    assert "memoryguard_memory_write" not in stop_result

    # No observation on Stop is "unknown", never "not applicable": absence of a host
    # observation may mean followed, violated, deferred, or simply not reached yet.
    events = store.list_rule_match_feedbacks(receipt_id=receipt_id)
    assert any(
        item.outcome == "unobserved"
        and item.source == "hook"
        and item.authority == 2
        and item.confidence == 0.0
        for item in events
    ), "stop with no observation must record unobserved (never not_applicable)"
    # unobserved is not an *effective* feedback, so the receipt stays pending for a real answer.
    assert store.get_rule_match_feedback_by_receipt(receipt_id) is None

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("mandatory_match_receipts") == []

    second_stop = run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "loop_count": 1},
    )
    assert second_stop == {}
    # the second stop must not re-write a duplicate unobserved event.
    assert sum(
        1 for item in store.list_rule_match_feedbacks(receipt_id=receipt_id)
        if item.outcome == "unobserved"
    ) == 1


def test_stop_skips_feedback_when_explicit_feedback_is_already_present(tmp_path: Path):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(
        SharedMemoryRecord(
            memory_id="always",
            body="以后默认走 RTK 进行测试验证。",
            kind=MemoryKind.PROCEDURE,
            status=SharedMemoryStatus.ACTIVE,
            injection_policy="always",
            agent_instance_id="codex-agent",
        )
    )
    store.set_rule_assignments("always", [{
        "target_type": "agent",
        "target_id": "codex-agent",
    }])
    session_id = "feedback-explicit-session"
    run_hook(
        provider="codex",
        event="user_prompt",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={
            "session_id": session_id,
            "turn_id": "turn-1",
            "prompt": "请继续任务。",
            "cwd": str(workspace),
        },
    )

    state_path = _state_path(workspace, "codex", session_id)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    receipts = state.get("mandatory_match_receipts", [])
    assert receipts
    receipt_id = str(receipts[0].get("receipt_id"))
    store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id=receipt_id,
        memory_id="always",
        share_group_id="group-a",
        agent_instance_id="codex-agent",
        task_hash="explicit",
        task="explicit session",
        assignment_ids=[],
        created_at=_now_iso(),
    ))

    preexisting = RuleMatchFeedback(
        feedback_id="manual-1",
        receipt_id=receipt_id,
        outcome="followed",
        actor="agent:codex-agent",
        evidence="显式记忆反馈",
        confidence=1.0,
    )
    store.append_rule_match_feedback(preexisting)

    run_hook(
        provider="codex",
        event="stop",
        workspace=workspace,
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        payload={"session_id": session_id, "loop_count": 0},
    )

    feedback = store.get_rule_match_feedback_by_receipt(receipt_id)
    assert feedback is not None
    assert feedback.outcome == "followed"
    assert feedback.feedback_id == "manual-1"


def test_internal_hook_feedback_cannot_infer_user_authority_from_actor_text(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    store = SharedMemoryStore(workspace, "group-a")
    store.append_record(SharedMemoryRecord(
        memory_id="always", body="始终先运行测试", kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE, injection_policy="always",
        agent_instance_id="codex-agent",
    ))
    store.set_rule_assignments("always", [{
        "target_type": "agent", "target_id": "codex-agent",
    }])
    receipt = store.append_rule_match_receipt(RuleMatchReceipt(
        receipt_id="hook-authority-receipt", memory_id="always",
        share_group_id="group-a", agent_instance_id="codex-agent",
        task_hash="task", task="task", session_id="hook-session",
    ))
    _save_state(workspace, "codex", "hook-session", {
        "mandatory_match_receipts": [{
            "receipt_id": receipt.receipt_id, "memory_id": receipt.memory_id,
        }],
    })

    # Actor text is display metadata only; Hook producer remains hook/authority 2.
    _flush_pending_rule_feedback(
        workspace=workspace,
        provider="codex",
        agent_instance_id="codex-agent",
        share_group_id="group-a",
        session_id="hook-session",
        actor="user",
        trigger="stop",
    )
    event = store.list_rule_match_feedbacks(receipt_id=receipt.receipt_id)[0]
    assert event.actor == "user"
    assert event.source == "hook"
    assert event.authority == 2
@pytest.mark.parametrize("disable_mode", ["env", "config"])
def test_history_capture_global_disable_is_visible_in_receipt(tmp_path: Path, monkeypatch, disable_mode: str):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    if disable_mode == "env":
        monkeypatch.setenv("MEMORYGUARD_HISTORY_ENABLED", "0")
        expected = "disabled_by_env"
    else:
        path = workspace / ".memoryguard" / "history" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"enabled": false}', encoding="utf-8")
        expected = "disabled_by_config"
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="codex-agent", share_group_id="group-a", payload={
                 "session_id": "disabled-session", "turn_id": "turn-1", "prompt": "not archived",
             })
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    history = json.loads(receipt.read_text(encoding="utf-8"))["history_archive"]
    assert history == {"attempted": False, "reason": expected, "capture_enabled": False}


def test_history_capture_blocks_obvious_secret_without_persisting_raw(tmp_path: Path):
    from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    secret = "api_key=super-secret-value"
    run_hook(provider="codex", event="user_prompt", workspace=workspace,
             agent_instance_id="codex-agent", share_group_id="group-a", payload={
                 "session_id": "secret-session", "turn_id": "secret-turn", "prompt": secret,
             })
    assert ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id="codex-agent")
    )["sessions"] == []
    receipt = next((workspace / ".memoryguard" / "hook-runtime" / "heartbeat").glob("*.json"))
    receipt_text = receipt.read_text(encoding="utf-8")
    assert secret not in receipt_text
    history = json.loads(receipt_text)["history_archive"]
    assert history["reason"] == "secret_detected_blocked"
    assert history["secret_blocked"] is True


def test_concurrent_runtime_receipt_writes_remain_valid_json(tmp_path: Path):
    from memoryguard.host_hooks import _write_json_config

    path = tmp_path / ".memoryguard" / "hook-runtime" / "heartbeat" / "agent.json"
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda index: _write_json_config(path, {"index": index}), range(80)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["index"] in range(80)
    assert not list(path.parent.glob("*.tmp"))


def test_atomic_runtime_replace_retries_windows_access_denied(tmp_path: Path, monkeypatch):
    import memoryguard.host_hooks as hooks

    path = tmp_path / "heartbeat.json"
    real_replace = hooks.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("temporarily locked")
            error.winerror = 5
            raise error
        return real_replace(source, target)

    monkeypatch.setattr(hooks.os, "replace", flaky_replace)
    hooks._atomic_write_text(path, '{"ok": true}')
    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}


def test_heartbeat_write_failure_never_blocks_pretool_hook(tmp_path: Path, monkeypatch):
    import memoryguard.host_hooks as hooks

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")

    def fail_write(*_args, **_kwargs):
        raise PermissionError("simulated heartbeat lock")

    monkeypatch.setattr(hooks, "_write_json_config", fail_write)
    result = run_hook(provider="codex", event="pre_tool", workspace=workspace,
                      agent_instance_id="codex-agent", share_group_id="group-a", payload={
                          "session_id": "session-1", "tool_name": "Read",
                          "tool_input": {"file_path": str(workspace / "README.md")},
                      })
    assert result == {}


def test_mandatory_state_write_failure_is_fail_closed_and_diagnosed(tmp_path: Path, monkeypatch, capsys):
    import memoryguard.host_hooks as hooks

    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "codex-agent", "group-a")
    SharedMemoryStore(workspace, "group-a")
    real_write = hooks._write_json_config

    def fail_state_only(path, data):
        if "state" in path.parts:
            raise PermissionError("simulated mandatory state lock")
        return real_write(path, data)

    monkeypatch.setattr(hooks, "_write_json_config", fail_state_only)
    with pytest.raises(PermissionError, match="mandatory state lock"):
        run_hook(provider="codex", event="user_prompt", workspace=workspace,
                 agent_instance_id="codex-agent", share_group_id="group-a", payload={
                     "session_id": "session-1", "turn_id": "turn-1",
                     "prompt": "regular prompt", "cwd": str(workspace),
                 })
    diagnostic = capsys.readouterr().err
    assert "mandatory_state_write_failed" in diagnostic
    assert "regular prompt" not in diagnostic


def test_invalid_existing_hook_config_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _bind(workspace, "codex-agent", "group-a")
    path = home / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON hook config"):
        HostHookManager(workspace).install(
            "codex",
            agent_instance_id="codex-agent",
            share_group_id="group-a",
        )
    assert path.read_text(encoding="utf-8") == "{broken"


def test_subagent_start_receives_bounded_governance_context(
    tmp_path: Path,
):
    workspace = tmp_path / "control"
    workspace.mkdir()
    _bind(workspace, "claude-agent", "group-a")
    SharedMemoryStore(workspace, "group-a").append_record(_record(
        "project",
        "MemoryGuard Hook 适配必须保持配置幂等",
        MemoryKind.PROJECT,
    ))

    result = run_hook(
        provider="claude",
        event="subagent_start",
        workspace=workspace,
        agent_instance_id="claude-agent",
        share_group_id="group-a",
        payload={
            "session_id": "session-1",
            "task": "检查 MemoryGuard Hook 配置幂等",
        },
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "配置幂等" in context
    assert "不得写入宿主原生记忆" in context


def test_cli_ensure_installs_only_explicit_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    home = tmp_path / "home"
    workspace = tmp_path / "control"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _bind(workspace, "claude-agent", "group-a")

    rc = cli_main([
        "hooks",
        "ensure",
        "--provider",
        "claude",
        "--workspace",
        str(workspace),
        "--agent-id",
        "claude-agent",
        "--share-group-id",
        "group-a",
    ])
    output = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert output["configured"] is True
    assert (home / ".claude" / "settings.json").exists()
    assert not (home / ".codex" / "hooks.json").exists()
    assert not (home / ".cursor" / "hooks.json").exists()
