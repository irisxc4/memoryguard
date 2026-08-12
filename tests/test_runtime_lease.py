"""Req10: multi-MCP runtime lease + split-brain detection tests.

Covers the four required scenarios:
  * two live processes, same version + fingerprint  -> both granted, coexist
  * live lease with a different version/fingerprint -> split-brain, fail
    closed, restart_required=True, and the conflicting process is never killed
  * stale (dead-pid) lease                          -> pruned / ignored
  * release is idempotent
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import memoryguard.runtime_lease as runtime_lease
from memoryguard.evidence import EvidenceStore
from memoryguard.governance_v2 import GovernanceV2
from memoryguard.memory import MemoryAtomStore
from memoryguard.runtime_lease import (
    RuntimeLeaseStore,
    _pid_alive,
    check_runtime_lease,
    default_database_paths,
    release_runtime_lease,
)
from memoryguard.runtime_v2.group_native import GroupControlService
from memoryguard.storage.layout import WorkspaceV2Layout
from memoryguard.storage.schema import initialize_all
from memoryguard.system.manifest import ManifestManager, ManifestState

# Impossible pid: guaranteed to be dead on any real OS.
DEAD_PID = 2**30 + 7

VERSION = "0.5.2"
FP = "a" * 64


def test_norm_path_normalizes_windows_case(tmp_path):
    from memoryguard.runtime_lease import _norm_path

    upper = _norm_path(tmp_path / "Case.sqlite")
    lower = _norm_path(tmp_path / "case.sqlite")
    if os.name == "nt":
        assert upper == lower
    else:
        assert upper != lower


def test_default_database_paths_follow_formal_v2_layout(tmp_path):
    expected = sorted(str(path.resolve()) for path in WorkspaceV2Layout(tmp_path).all_db_paths)
    assert default_database_paths(tmp_path) == expected


def _activate_v2_workspace(workspace: Path) -> None:
    """Build the real V2 schema and persist the active cutover manifest."""
    initialize_all(WorkspaceV2Layout(workspace))
    memory = MemoryAtomStore(workspace)
    evidence = EvidenceStore(workspace)
    GovernanceV2(workspace, memory_store=memory, evidence_store=evidence)
    manager = ManifestManager(workspace)
    manager.transition(ManifestState.V2_BUILDING, migration_id="runtime-lease-tests")
    manager.transition(
        ManifestState.V2_READY,
        source_digest="runtime-lease-source",
        target_digest="runtime-lease-target",
        manifest_digest="runtime-lease-manifest",
        digests={"validator_passed": True, "checkpoints": {"runtime": True}},
    )
    assert manager.transition(ManifestState.V2_ACTIVE).state is ManifestState.V2_ACTIVE
    GroupControlService(workspace, write=True).bind_agent(
        "test-agent", "default", idempotency_key="runtime-lease-test-binding",
    )


def _lease(
    store: RuntimeLeaseStore,
    *,
    pid: int,
    version: str,
    fingerprint: str,
    started_at: str | None = None,
) -> None:
    if started_at is None:
        observed = getattr(runtime_lease, "_process_started_at_for_pid", lambda _pid: None)(pid)
        started_at = observed.isoformat() if observed is not None else runtime_lease.process_started_at()
    store.upsert({
        "pid": pid,
        "process_started_at": started_at,
        "memoryguard_version": version,
        "code_fingerprint": fingerprint,
        "control_workspace": str(store.control_workspace),
        "database_paths": [str(store.control_workspace / "x" / "memory.db")],
    })


def test_same_build_multiple_acquires_coexist(tmp_path):
    """No-conflict: two live processes with the same version/fingerprint are
    both granted and both leases persist (multi-MCP is normal)."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0
        ),
    )
    try:
        assert _pid_alive(proc.pid)
        r1 = check_runtime_lease(
            tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
        )
        r2 = check_runtime_lease(
            tmp_path, [], version=VERSION, code_fingerprint=FP, pid=proc.pid,
        )
        assert r1["granted"] is True and r1["split_brain"] is False
        assert r2["granted"] is True and r2["split_brain"] is False
        leases = RuntimeLeaseStore(tmp_path).load()
        assert {int(l["pid"]) for l in leases} == {os.getpid(), proc.pid}
        assert all(str(l["memoryguard_version"]) == VERSION for l in leases)
        assert all(str(l["code_fingerprint"]) == FP for l in leases)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_reuse_same_process_lease(tmp_path):
    """A matching process instance reuses its lease; a reused PID does not."""
    r1 = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    r2 = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    assert r1["granted"] is True
    assert r2["granted"] is True and r2["split_brain"] is False
    leases = RuntimeLeaseStore(tmp_path).load()
    assert len([l for l in leases if int(l["pid"]) == os.getpid()]) == 1

    reused = dict(leases[0])
    reused["process_started_at"] = "2000-01-01T00:00:00+00:00"
    reused["memoryguard_version"] = "9.9.9"
    RuntimeLeaseStore(tmp_path).save([reused])
    r3 = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    assert r3["granted"] is True and r3["split_brain"] is False
    assert r3["pruned"] == 1
    current = RuntimeLeaseStore(tmp_path).load()
    assert len(current) == 1
    assert current[0]["process_started_at"] != reused["process_started_at"]
    assert current[0]["memoryguard_version"] == VERSION


def test_split_brain_different_version(tmp_path):
    """Split-brain: a live lease from a different build must fail closed,
    flag restart, and must NOT kill the conflicting process."""
    store = RuntimeLeaseStore(tmp_path)
    _lease(store, pid=os.getpid(), version="9.9.9", fingerprint=FP)
    assert _pid_alive(os.getpid())  # the "other" process is alive

    result = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid() + 1,
    )
    assert result["granted"] is False
    assert result["split_brain"] is True
    assert result["restart_required"] is True
    pids = [int(c["pid"]) for c in result["conflicting"]]
    assert os.getpid() in pids
    # fail-closed: the new process did not take a lease
    assert all(int(l["pid"]) != os.getpid() + 1 for l in store.load())
    # the conflicting process was never killed
    assert _pid_alive(os.getpid())


def test_split_brain_different_fingerprint(tmp_path):
    """Split-brain also triggers on the same version but a different code
    fingerprint (same DB, different source tree)."""
    store = RuntimeLeaseStore(tmp_path)
    _lease(store, pid=os.getpid(), version=VERSION, fingerprint="f" * 64)
    result = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid() + 1,
    )
    assert result["granted"] is False
    assert result["split_brain"] is True
    assert result["restart_required"] is True
    assert os.getpid() in [int(c["pid"]) for c in result["conflicting"]]
    assert _pid_alive(os.getpid())


def test_stale_lease_is_pruned(monkeypatch, tmp_path):
    """Stale: a lease whose pid is dead is cleaned up and never causes a
    conflict; the check proceeds and grants."""
    if os.name == "nt":
        ghost_pid = 35092

        class _Call:
            def __init__(self, result):
                self.result = result

            def __call__(self, *_args):
                return self.result

        class _Kernel32:
            OpenProcess = _Call(None)
            GetExitCodeProcess = _Call(False)
            CloseHandle = _Call(True)

        with monkeypatch.context() as patch:
            patch.setattr(runtime_lease, "_win_process_ids", lambda: set())
            patch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: _Kernel32())
            patch.setattr(ctypes, "get_last_error", lambda: 5)
            assert runtime_lease._win_pid_alive(ghost_pid) is False

    store = RuntimeLeaseStore(tmp_path)
    _lease(store, pid=DEAD_PID, version="9.9.9", fingerprint=FP)
    assert not _pid_alive(DEAD_PID)

    result = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    assert result["granted"] is True
    assert result["split_brain"] is False
    assert result["conflicting"] == []
    # the dead entry is gone; only the current process remains
    leases = store.load()
    assert all(int(l["pid"]) != DEAD_PID for l in leases)
    assert int(leases[0]["pid"]) == os.getpid()


def test_release_idempotent(tmp_path):
    result = check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    assert result["granted"] is True
    assert release_runtime_lease(tmp_path, pid=os.getpid()) is True
    # second release removes nothing -> False (idempotent)
    assert release_runtime_lease(tmp_path, pid=os.getpid()) is False
    assert RuntimeLeaseStore(tmp_path).load() == []




def _mcp_call(workspace, requests, extra_env):
    """Run one real stdio MCP subprocess and return parsed response objects."""
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra_env)
    env.setdefault("MEMORYGUARD_AGENT_ID", "test-agent")
    env.setdefault("MEMORYGUARD_ADMIN", "1")
    env.setdefault("MEMORYGUARD_STRICT_BINDING", "0")
    env.setdefault("MEMORYGUARD_ALLOW_ANON", "1")
    payload = "".join(json.dumps(req, ensure_ascii=False) + "\n" for req in requests)
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "from memoryguard.mcp_server import serve_stdio; "
            "raise SystemExit(serve_stdio())",
        ],
        input=payload,
        text=True,
        encoding="utf-8",
        errors="strict",
        capture_output=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        timeout=60,
    )
    responses = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            raise AssertionError(
                f"non-JSON MCP stdout line: {line!r}; stderr={proc.stderr[-1000:]}"
            )
    return responses, proc


def _write_request(workspace):
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "memoryguard_memory_write",
            "arguments": {
                "workspace": str(workspace),
                "memory_id": "runtime-lease-subprocess",
                "body": "runtime lease subprocess write",
                "idempotency_key": "runtime-lease-subprocess-write",
            },
        },
    }


def test_execute_tool_rejects_split_brain_in_real_mcp_subprocess(tmp_path):
    """The mutating-tool lease guard must be wired into execute_tool(), not
    only into runtime_lease unit checks.  A live conflicting build is rejected
    by a real ``serve_stdio()`` subprocess without killing the other process."""
    _activate_v2_workspace(tmp_path)
    store = RuntimeLeaseStore(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP") else 0
        ),
    )
    try:
        assert _pid_alive(proc.pid)
        peer_started = runtime_lease._process_started_at_for_pid(proc.pid)
        assert peer_started is not None
        store.upsert({
            "pid": proc.pid,
            "process_started_at": peer_started.isoformat(),
            "memoryguard_version": "9.9.9",
            "code_fingerprint": "f" * 64,
            "control_workspace": str(tmp_path.resolve()),
            "database_paths": default_database_paths(tmp_path),
        })
        responses, run = _mcp_call(
            tmp_path, [_write_request(tmp_path)],
            {
                "MEMORYGUARD_WORKSPACE": str(tmp_path),
                "MEMORYGUARD_AGENT_ID": "test-agent",
                "MEMORYGUARD_STRICT_BINDING": "1",
                "MEMORYGUARD_ALLOW_ANON": "0",
                "MEMORYGUARD_SESSION_ID": "runtime-lease-session",
                "MEMORYGUARD_SESSION_SOURCE": "transport",
                "MEMORYGUARD_SESSION_TRUSTED": "1",
                "MEMORYGUARD_PROJECT_CWD": str(tmp_path),
            },
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert len(responses) == 1, (responses, run.stderr[-2000:])
        result = responses[0]["result"]
        assert result.get("isError") is True, result
        payload = json.loads(result["content"][0]["text"])
        assert payload.get("error") == "runtime_split_brain", payload
        assert payload.get("restart_required") is True
        pids = [str(item.get("pid", "")) for item in payload.get("conflicting", [])]
        assert str(proc.pid) in pids
        assert _pid_alive(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_execute_tool_acquires_lease_and_writes_in_real_mcp_subprocess(tmp_path):
    """With no conflict, a real MCP subprocess write passes the guard, takes
    its lease, and persists the memory through the normal tool handler."""
    _activate_v2_workspace(tmp_path)
    responses, run = _mcp_call(
        tmp_path, [_write_request(tmp_path)],
        {
            "MEMORYGUARD_WORKSPACE": str(tmp_path),
            "MEMORYGUARD_AGENT_ID": "test-agent",
            "MEMORYGUARD_STRICT_BINDING": "1",
            "MEMORYGUARD_ALLOW_ANON": "0",
            "MEMORYGUARD_SESSION_ID": "runtime-lease-session",
            "MEMORYGUARD_SESSION_SOURCE": "transport",
            "MEMORYGUARD_SESSION_TRUSTED": "1",
            "MEMORYGUARD_PROJECT_CWD": str(tmp_path),
        },
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert len(responses) == 1, (responses, run.stderr[-2000:])
    result = responses[0]["result"]
    assert result.get("isError") is not True, result
    leases = RuntimeLeaseStore(tmp_path).load()
    assert len(leases) == 1
    assert str(leases[0]["control_workspace"]) == str(tmp_path.resolve())


def test_lease_file_has_required_fields(tmp_path):
    """Every lease entry carries the Req10 field set."""
    check_runtime_lease(
        tmp_path, [], version=VERSION, code_fingerprint=FP, pid=os.getpid(),
    )
    leases = RuntimeLeaseStore(tmp_path).load()
    assert len(leases) == 1
    lease = leases[0]
    for field in (
        "pid", "process_started_at", "memoryguard_version",
        "code_fingerprint", "control_workspace", "database_paths",
    ):
        assert field in lease, f"missing lease field: {field}"
