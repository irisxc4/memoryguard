from __future__ import annotations

from memoryguard import codex_subagent_reconcile, host_hooks


def test_codex_lifecycle_runs_once_on_supported_events(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(host_hooks, "get_hook_mode", lambda *args, **kwargs: "observe")
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_mcp_lifecycle",
        lambda *, event, workspace, payload: calls.append(event) or {},
    )
    monkeypatch.setattr(host_hooks, "_best_effort_codex_global_reconcile", lambda **kwargs: {})
    monkeypatch.setattr(host_hooks, "_best_effort_codex_reconcile", lambda **kwargs: {})
    monkeypatch.setattr(host_hooks, "_v2_hook_cutover", lambda **kwargs: {})

    for event in ("session_start", "user_prompt", "post_tool", "stop", "pre_tool"):
        host_hooks.run_hook(
            provider="codex",
            event=event,
            workspace=tmp_path,
            agent_instance_id="agent-a",
            share_group_id="shared-a",
            payload={"session_id": "session-a"},
        )

    assert calls == ["session_start", "user_prompt", "post_tool", "stop"]


def test_verified_hook_thread_falls_back_to_db_authenticated_session(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "trusted_codex_thread_id",
        lambda environ=None: "",
    )
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "codex_thread_matches_workspace",
        lambda thread_id, workspace, **kwargs: thread_id == "session-a",
    )

    assert host_hooks._verified_codex_hook_thread_id(
        tmp_path,
        {"session_id": "session-a", "cwd": str(tmp_path / "project")},
    ) == "session-a"
    assert host_hooks._verified_codex_hook_thread_id(
        tmp_path,
        {"session_id": "forged-or-other-workspace", "cwd": str(tmp_path / "project")},
    ) == ""


def test_verified_hook_thread_prefers_payload_cwd_for_host_environment_id(
    monkeypatch, tmp_path
):
    project = tmp_path / "project"
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "trusted_codex_thread_id",
        lambda environ=None: "host-thread",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "codex_thread_matches_workspace",
        lambda thread_id, workspace, **kwargs: (
            calls.append((thread_id, str(workspace))) or workspace == str(project)
        ),
    )

    assert host_hooks._verified_codex_hook_thread_id(
        tmp_path / "control",
        {"cwd": str(project)},
    ) == "host-thread"
    assert calls == [("host-thread", str(project))]


def test_terminal_reconcile_passes_only_proven_thread_ids_to_cohort_cleanup(
    monkeypatch, tmp_path
):
    calls: list[tuple[str, ...]] = []
    import memoryguard.codex_mcp_lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle,
        "reclaim_terminal_codex_threads",
        lambda *, workspace, thread_ids, **kwargs: (
            calls.append(tuple(thread_ids))
            or {
                "status": "ok",
                "killed_pids": [101],
                "failed_pids": [],
                "skipped_shared_thread_ids": [],
                "skipped_ambiguous_thread_ids": [],
            }
        ),
    )

    result = host_hooks._attach_terminal_cohort_reclaim(
        tmp_path,
        {
            "closed_edge_ids": ["child-a"],
            "missing_thread_ids": ["child-b"],
            "candidate_thread_ids": ["not-terminal-only-candidate"],
        },
    )

    assert calls == [("child-a", "child-b")]
    assert result["terminal_cohort_killed_count"] == 1


def test_lifecycle_hook_persists_verified_host_thread_id(monkeypatch, tmp_path):
    import memoryguard.codex_mcp_lifecycle as lifecycle

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        host_hooks,
        "_verified_codex_hook_thread_id",
        lambda workspace, payload: "verified-thread",
    )
    monkeypatch.setattr(
        lifecycle,
        "handle_codex_mcp_lifecycle",
        lambda **kwargs: calls.append(kwargs) or {"status": "ok"},
    )
    monkeypatch.setattr(
        host_hooks,
        "_codex_lifecycle_lease_id",
        lambda payload: "session:lease-a",
    )

    result = host_hooks._best_effort_codex_mcp_lifecycle(
        event="post_tool",
        workspace=tmp_path,
        payload={"session_id": "verified-thread"},
    )

    assert result["status"] == "ok"
    assert calls[0]["thread_id"] == "session:lease-a"
    assert calls[0]["host_thread_id"] == "verified-thread"


def test_indexed_terminal_sweep_protects_current_hook_thread(monkeypatch, tmp_path):
    import memoryguard.codex_mcp_lifecycle as lifecycle

    calls: list[set[str]] = []
    monkeypatch.setattr(
        lifecycle,
        "reclaim_indexed_terminal_codex_threads",
        lambda *, workspace, protected_thread_ids, **kwargs: (
            calls.append(set(protected_thread_ids))
            or {
                "status": "ok",
                "reclaimed_thread_ids": ["old-thread"],
                "killed_pids": [101],
                "failed_pids": [],
            }
        ),
    )

    result = host_hooks._attach_indexed_terminal_cohort_reclaim(
        tmp_path,
        {},
        protected_thread_ids={"current-thread"},
    )

    assert calls == [{"current-thread"}]
    assert result["indexed_terminal_reclaim_count"] == 1
    assert result["indexed_terminal_killed_count"] == 1


def test_post_tool_runs_global_terminal_reconcile(monkeypatch, tmp_path):
    global_calls: list[str] = []
    monkeypatch.setattr(host_hooks, "get_hook_mode", lambda *args, **kwargs: "observe")
    monkeypatch.setattr(host_hooks, "_best_effort_codex_mcp_lifecycle", lambda **kwargs: {})
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_global_reconcile",
        lambda **kwargs: global_calls.append(str(kwargs.get("event"))) or {},
    )
    monkeypatch.setattr(host_hooks, "_v2_hook_cutover", lambda **kwargs: {})

    host_hooks.run_hook(
        provider="codex",
        event="post_tool",
        workspace=tmp_path,
        agent_instance_id="agent-a",
        share_group_id="shared-a",
        payload={"session_id": "session-a"},
    )

    assert global_calls == ["post_tool"]


def test_other_providers_never_call_codex_lifecycle(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(host_hooks, "get_hook_mode", lambda *args, **kwargs: "observe")
    monkeypatch.setattr(
        host_hooks,
        "_best_effort_codex_mcp_lifecycle",
        lambda *, event, workspace, payload: calls.append(event) or {},
    )
    monkeypatch.setattr(host_hooks, "_v2_hook_cutover", lambda **kwargs: {})

    for provider in ("claude", "cursor"):
        host_hooks.run_hook(
            provider=provider,
            event="post_tool",
            workspace=tmp_path,
            agent_instance_id="agent-a",
            share_group_id="shared-a",
            payload={"session_id": "session-a"},
        )

    assert calls == []


def test_lifecycle_lease_prefers_matching_host_thread_id(monkeypatch):
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "trusted_codex_thread_id",
        lambda environ=None: "thread-host",
    )
    assert host_hooks._codex_lifecycle_lease_id(
        {"session_id": "thread-host"}
    ) == "thread:thread-host"


def test_lifecycle_lease_rejects_inherited_stale_host_thread_id(monkeypatch):
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "trusted_codex_thread_id",
        lambda environ=None: "parent-thread",
    )
    lease_id = host_hooks._codex_lifecycle_lease_id(
        {"session_id": "nested-thread"}
    )
    assert lease_id.startswith("session:")
    assert lease_id != "thread:parent-thread"
    assert "nested-thread" not in lease_id


def test_lifecycle_lease_falls_back_to_hashed_hook_session(monkeypatch):
    monkeypatch.setattr(
        codex_subagent_reconcile,
        "trusted_codex_thread_id",
        lambda environ=None: "",
    )
    first = host_hooks._codex_lifecycle_lease_id({"session_id": "session-a"})
    second = host_hooks._codex_lifecycle_lease_id({"session_id": "session-a"})
    other = host_hooks._codex_lifecycle_lease_id({"session_id": "session-b"})

    assert first.startswith("session:")
    assert first == second
    assert first != other
    assert "session-a" not in first
    assert host_hooks._codex_lifecycle_lease_id({}) == ""
