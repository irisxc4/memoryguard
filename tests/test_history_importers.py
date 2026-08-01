import json
from pathlib import Path

from memoryguard.agent_binding import AgentBindingStore
from memoryguard.conversation_history import ConversationHistoryStore, HistoryScope
from memoryguard.history_importers import (
    backfill_local_history,
    discover_local_history_sources,
)
from memoryguard.security import is_mutation_method, is_readonly_method


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _bound_agents(workspace: Path) -> dict[str, str]:
    bindings = AgentBindingStore(workspace)
    result = {}
    for provider in ("codex", "claude", "cursor"):
        agent = f"{provider}-agent"
        bindings.ensure_personal_memory_group(agent)
        result[provider] = agent
    return result


def _write_discovery(workspace: Path, agents: dict[str, str]) -> None:
    path = workspace / ".memoryguard" / "discovery" / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"instances": [
        {"instance_id": agent_id, "product": "claude-code" if provider == "claude" else provider}
        for provider, agent_id in agents.items()
    ]}), encoding="utf-8")


def test_backfill_imports_stable_hosts_and_filters_private_event_types(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_jsonl(home / ".codex" / "sessions" / "codex-a.jsonl", [
        {"type": "session_meta", "payload": {"id": "codex-session", "title": "Codex title"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "visible codex user"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "content": "must not import"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "visible codex answer"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "role": "assistant", "content": "tool secret"}},
    ])
    _write_jsonl(home / ".claude" / "projects" / "project" / "claude-a.jsonl", [
        {"sessionId": "claude-session", "type": "user", "message": {"role": "user", "content": "visible claude user"}},
        {"sessionId": "claude-session", "type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "visible claude answer"}, {"type": "thinking", "text": "hidden thought"}]}},
    ])
    _write_jsonl(home / ".cursor" / "projects" / "one" / "agent-transcripts" / "cursor-a.jsonl", [
        {"role": "user", "message": {"content": [{"type": "text", "text": "visible cursor user"}]}},
        {"role": "assistant", "message": {"content": [{"type": "text", "text": "visible cursor answer"}, {"type": "tool_use", "text": "hidden tool"}]}},
    ])
    _write_jsonl(home / ".claude" / "projects" / "history.jsonl", [{"command": "not a conversation"}])
    agents = _bound_agents(workspace)
    _write_discovery(workspace, agents)

    result = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents)

    assert result["status"] == "complete"
    assert result["imported"] == 3
    inventory = result["inventory"]["sources"]
    assert any(item["status"] == "unsupported" and item["support_reason"] == "prompt_index_not_full_conversation" for item in inventory)
    assert all({"provider", "path", "file_count", "byte_count", "support_reason", "matched_agent_id"} <= item.keys() for item in inventory)
    store = ConversationHistoryStore(workspace)
    all_text = []
    for provider, agent in agents.items():
        sessions = store.list_sessions(HistoryScope(agent_instance_id=agent, provider=provider))["sessions"]
        assert len(sessions) == 1
        raw = store.read(HistoryScope(agent_instance_id=agent, provider=provider), session_id=sessions[0]["session_id"])
        all_text.extend(turn["content"] for turn in raw["turns"])
    assert "visible codex user" in all_text
    assert "visible cursor answer" in all_text
    assert not any("hidden" in value or "secret" in value for value in all_text)


def test_backfill_is_idempotent_bounded_and_resumable(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    for name in ("a", "b"):
        _write_jsonl(home / ".codex" / "sessions" / f"{name}.jsonl", [
            {"type": "session_meta", "payload": {"id": name}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": name}]}},
            "broken json line is safely skipped",
        ])
    agents = _bound_agents(workspace)
    _write_discovery(workspace, agents)
    first = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents, max_files=1)
    assert first["imported"] == 1
    assert first["status"] == "importing"
    assert first["continuation"]
    second = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents,
                                    continuation=first["continuation"], max_files=1)
    assert second["imported"] == 1
    assert second["status"] == "complete"
    replay = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents)
    assert replay["imported"] == 0
    assert replay["status"] == "complete"
    sessions = ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id=agents["codex"], provider="codex")
    )["sessions"]
    assert len(sessions) == 2


def test_discovery_and_backfill_fail_closed_without_provider_binding(tmp_path: Path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    _write_jsonl(home / ".claude" / "projects" / "p" / "only.jsonl", [
        {"sessionId": "c", "message": {"role": "user", "content": "must not bind to codex"}},
    ])
    bindings = AgentBindingStore(workspace)
    bindings.ensure_personal_memory_group("codex-agent")
    _write_discovery(workspace, {"codex": "codex-agent"})
    discovery = discover_local_history_sources(home, workspace=workspace,
                                               agent_ids_by_provider={"codex": "codex-agent"})
    claude = next(item for item in discovery["sources"] if item["provider"] == "claude")
    assert claude["status"] == "pending_binding"
    assert claude["matched_agent_id"] == ""
    result = backfill_local_history(workspace, home=home, agent_ids_by_provider={"codex": "codex-agent"})
    assert result["status"] == "pending_binding"
    assert result["pending_binding"] == ["claude"]
    assert ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id="codex-agent")
    )["sessions"] == []


def test_history_backfill_security_registration_is_not_read_only():
    assert is_readonly_method("discover_local_history_sources")
    assert is_mutation_method("backfill_local_history")
    assert not is_readonly_method("backfill_local_history")


def test_pending_provider_is_not_skipped_by_other_provider_continuation(tmp_path: Path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    _write_jsonl(home / ".codex" / "sessions" / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "codex"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "codex later"}]}},
    ])
    _write_jsonl(home / ".claude" / "projects" / "p" / "claude.jsonl", [
        {"sessionId": "claude", "message": {"role": "user", "content": "claude now"}},
    ])
    bindings = AgentBindingStore(workspace)
    bindings.ensure_personal_memory_group("claude-agent")
    _write_discovery(workspace, {"claude": "claude-agent"})
    first = backfill_local_history(workspace, home=home, agent_ids_by_provider={"claude": "claude-agent"})
    assert first["imported"] == 1
    assert first["pending_binding"] == ["codex"]
    bindings.ensure_personal_memory_group("codex-agent")
    _write_discovery(workspace, {"claude": "claude-agent", "codex": "codex-agent"})
    second = backfill_local_history(workspace, home=home, agent_ids_by_provider={
        "claude": "claude-agent", "codex": "codex-agent",
    }, continuation=first["continuation"])
    assert second["imported"] == 1
    assert ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id="codex-agent", provider="codex")
    )["sessions"]


def test_large_jsonl_creates_partial_history_index_instead_of_being_dropped(tmp_path: Path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    path = home / ".codex" / "sessions" / "large.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session_meta", "payload": {"id": "large", "title": "large session"}}) + "\n")
        for _ in range(17):  # >16MiB: regression for the former hard reject
            handle.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x" * (1024 * 1024)}]}}) + "\n")
    agents = _bound_agents(workspace)
    _write_discovery(workspace, agents)
    result = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents)
    assert result["imported"] == 1
    assert result["partial"] == 1
    codex_source = next(item for item in result["inventory"]["sources"] if item["provider"] == "codex")
    assert codex_source["status"] == "partial"
    sessions = ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id=agents["codex"], provider="codex")
    )["sessions"]
    assert len(sessions) == 1
    assert "部分导入" in sessions[0]["title"]


def test_provider_identity_mismatch_fails_closed_even_with_active_binding(tmp_path: Path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    _write_jsonl(home / ".claude" / "projects" / "p" / "only.jsonl", [
        {"sessionId": "c", "message": {"role": "user", "content": "never bind to codex"}},
    ])
    AgentBindingStore(workspace).ensure_personal_memory_group("codex-agent")
    _write_discovery(workspace, {"codex": "codex-agent"})
    result = backfill_local_history(workspace, home=home, agent_ids_by_provider={"claude": "codex-agent"})
    assert result["status"] == "pending_binding"
    assert result["pending_binding"] == ["claude"]


def test_backfill_derives_readable_title_when_host_jsonl_has_none(tmp_path: Path):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    _write_jsonl(home / ".codex" / "sessions" / "rollout-20260730.jsonl", [
        {"type": "session_meta", "payload": {"id": "rollout-20260730"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "请把历史会话映射进神经图"},
        ]}},
    ])
    agents = _bound_agents(workspace)
    _write_discovery(workspace, agents)
    result = backfill_local_history(workspace, home=home, agent_ids_by_provider=agents)
    assert result["imported"] == 1
    sessions = ConversationHistoryStore(workspace).list_sessions(
        HistoryScope(agent_instance_id=agents["codex"], provider="codex")
    )["sessions"]
    assert sessions[0]["title"] == "请把历史会话映射进神经图"
    assert "rollout" not in sessions[0]["title"].lower()
