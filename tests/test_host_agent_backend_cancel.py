"""Cancellable owned CLI subprocess behaviour for the host Agent backend.

These tests pin the 0.7.1 guarantee that a background build which owns a CLI
subprocess releases it on cancellation and never leaves an orphan command
window or CLI process behind.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from memoryguard.host_agent_backend import _run_cli_cancellable
from memoryguard.runtime_v2.task_coordinator import TaskCancelled


class _Cancellable:
    cancelled = False

    def __init__(self):
        self._cleanups = []

    def own_cleanup(self, callback):
        self._cleanups.append(callback)

    def check_cancelled(self):
        if self.cancelled:
            raise TaskCancelled("test-cancel")


def _pid_alive(pid: int) -> bool:
    """Reliable Windows liveness probe (``os.kill(pid, 0)`` false-positives here)."""
    if sys.platform != "win32":
        try:
            import os

            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return True  # unknown -> treat as alive so the test fails closed
    return str(pid) in out


def _wait_pid_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_run_cli_cancellable_terminates_child_on_cancel(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    # 子进程写 PID 后长眠；取消后必须被 terminate/kill 且无残留。
    script = (
        "import time, os, pathlib;"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(120)"
    )
    execution = _Cancellable()

    def canceller():
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        execution.cancelled = True

    thread = threading.Thread(target=canceller, daemon=True)
    thread.start()
    with pytest.raises(TaskCancelled):
        _run_cli_cancellable(
            [sys.executable, "-c", script], None, timeout=60, execution=execution,
        )
    thread.join(timeout=5.0)
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    assert _wait_pid_dead(pid), "cancelled CLI subprocess was left running"


def test_run_cli_cancellable_returns_output_when_not_cancelled(tmp_path: Path) -> None:
    execution = _Cancellable()
    script = "import sys; print('hello-from-child')"
    out = _run_cli_cancellable(
        [sys.executable, "-c", script], None, timeout=30, execution=execution,
    )
    assert "hello-from-child" in out


def test_run_cli_cancellable_terminates_child_on_timeout(tmp_path: Path) -> None:
    pidfile = tmp_path / "timeout-child.pid"
    script = (
        "import time, os, pathlib;"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()));"
        "time.sleep(120)"
    )
    with pytest.raises(TimeoutError, match="cli_subprocess_timeout"):
        _run_cli_cancellable(
            [sys.executable, "-c", script], None, timeout=1, execution=_Cancellable(),
        )
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    assert _wait_pid_dead(pid), "timed-out CLI subprocess was left running"


def test_probe_cli_launch_rejects_spawn_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import memoryguard.host_agent_backend as backend

    def denied(*args, **kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(backend.subprocess, "run", denied)
    assert backend._probe_cli_launch("blocked-codex.exe", "--version") is False


def test_find_codex_cli_prefers_spawnable_user_launcher_over_path_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memoryguard.host_agent_backend as backend

    sandbox = tmp_path / ".codex" / ".sandbox-bin" / "codex.exe"
    sandbox.parent.mkdir(parents=True)
    sandbox.write_bytes(b"launcher")
    blocked = tmp_path / "WindowsApps" / "codex.exe"
    blocked.parent.mkdir(parents=True)
    blocked.write_bytes(b"protected")

    monkeypatch.setattr(backend.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(backend.shutil, "which", lambda name: str(blocked) if name == "codex" else None)
    monkeypatch.setattr(
        backend,
        "_probe_cli_launch",
        lambda path, *args: Path(path) == sandbox,
    )

    assert backend._find_codex_cli() == str(sandbox)
