"""Regression coverage for Hook state-lock contention.

These tests exercise the small state transaction seam directly.  A Hook
process may spend time in the native bootstrap path, but that work must not
hold the per-session JSON sidecar lock.  The tests intentionally use a real
subprocess for cross-process contention because the in-process lock registry
cannot model the Windows file-lock boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import time

import pytest

import memoryguard.host_hooks as hooks


def _child_env() -> dict[str, str]:
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    old_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(source_root), old_pythonpath) if item
    )
    return env


def _state(path_root: Path, session_id: str, provider: str = "codex") -> dict:
    return json.loads(
        hooks._state_path(path_root, provider, session_id).read_text(
            encoding="utf-8"
        )
    )


def test_same_session_process_updates_are_serialized_without_lost_events(
    tmp_path: Path,
) -> None:
    """Concurrent adjacent Hook events keep one coherent state document."""
    workspace = tmp_path / "workspace"
    session_id = "same-session-lock-contention"
    worker_count = 4
    events_per_worker = 8
    child_code = r'''
import sys
import time
from pathlib import Path

from memoryguard.host_hooks import _update_state

workspace = Path(sys.argv[1])
session_id = sys.argv[2]
worker = sys.argv[3]
count = int(sys.argv[4])
for index in range(count):
    def append_event(state, worker=worker, index=index):
        state.setdefault("events", []).append(f"{worker}:{index}")
        # Model a short local read/modify interval without involving native
        # bootstrap or external I/O.
        time.sleep(0.002)
    _update_state(workspace, "codex", session_id, mutator=append_event)
'''
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(workspace),
                session_id,
                f"worker-{worker}",
                str(events_per_worker),
            ],
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for worker in range(worker_count)
    ]
    failures: list[str] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        if process.returncode:
            failures.append(
                f"returncode={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
    assert not failures, failures

    events = _state(workspace, session_id)["events"]
    expected = {
        f"worker-{worker}:{index}"
        for worker in range(worker_count)
        for index in range(events_per_worker)
    }
    assert set(events) == expected
    assert len(events) == len(expected)
    assert not list((workspace / ".memoryguard" / "hook-runtime").rglob("*.tmp"))


def test_native_bootstrap_can_wait_without_blocking_state_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked bootstrap cannot make a same-session state update time out."""
    workspace = tmp_path / "workspace"
    session_id = "bootstrap-outside-state-lock"
    bootstrap_started = Event()
    release_bootstrap = Event()

    def blocked_bootstrap(**_kwargs):
        bootstrap_started.set()
        assert release_bootstrap.wait(timeout=5), "test bootstrap was not released"
        return {}

    monkeypatch.setattr(hooks, "_v2_hook_cutover", blocked_bootstrap)
    result: list[dict] = []
    errors: list[BaseException] = []

    def run_bootstrap() -> None:
        try:
            result.append(
                hooks.run_hook(
                    provider="claude",
                    event="pre_tool",
                    workspace=workspace,
                    agent_instance_id="agent-a",
                    share_group_id="group-a",
                    payload={"session_id": session_id, "tool_name": "shell"},
                )
            )
        except BaseException as exc:  # report the worker error in the assertion
            errors.append(exc)

    thread = Thread(target=run_bootstrap)
    thread.start()
    assert bootstrap_started.wait(timeout=2), "bootstrap did not reach the seam"

    started = time.monotonic()
    hooks._update_state(
        workspace,
        "claude",
        session_id,
        updates={"neighbor_event": "post_tool"},
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert _state(workspace, session_id, "claude")["neighbor_event"] == "post_tool"

    release_bootstrap.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert result == [{}]


def test_lock_transaction_preserves_mandatory_fail_closed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock contention handling must not turn a mandatory failure into allow."""
    workspace = tmp_path / "workspace"
    session_id = "mandatory-failure-remains-closed"
    path = hooks._state_path(workspace, "claude", session_id)
    hooks._write_json_config(
        path,
        {
            "mandatory_overflow": True,
            "bootstrap_ok": False,
            "bootstrap_error": "mandatory_budget_exceeded",
        },
    )
    monkeypatch.setattr(hooks, "_v2_hook_cutover", lambda **_kwargs: {})

    output = hooks.run_hook(
        provider="claude",
        event="pre_tool",
        workspace=workspace,
        agent_instance_id="agent-a",
        share_group_id="group-a",
        payload={"session_id": session_id, "tool_name": "shell"},
    )

    denied = output.get("hookSpecificOutput", {})
    assert denied.get("permissionDecision") == "deny"
    assert "强制规则" in json.dumps(output, ensure_ascii=False)
    assert _state(workspace, session_id, "claude")["mandatory_overflow"] is True
