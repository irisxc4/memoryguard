"""Req10: multi-MCP runtime lease + split-brain detection tests.

Covers the four required scenarios:
  * two live processes, same version + fingerprint  -> both granted, coexist
  * live lease with a different version/fingerprint -> split-brain, fail
    closed, restart_required=True, and the conflicting process is never killed
  * stale (dead-pid) lease                          -> pruned / ignored
  * release is idempotent
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from memoryguard.runtime_lease import (
    RuntimeLeaseStore,
    _pid_alive,
    check_runtime_lease,
    release_runtime_lease,
)

# Impossible pid: guaranteed to be dead on any real OS.
DEAD_PID = 2**30 + 7

VERSION = "0.5.2"
FP = "a" * 64


def _lease(store: RuntimeLeaseStore, *, pid: int, version: str, fingerprint: str) -> None:
    store.upsert({
        "pid": pid,
        "process_started_at": "2026-08-07T00:00:00+00:00",
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
    """No-conflict: re-acquiring from the same pid replaces its own lease
    (single entry) and stays granted."""
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


def test_stale_lease_is_pruned(tmp_path):
    """Stale: a lease whose pid is dead is cleaned up and never causes a
    conflict; the check proceeds and grants."""
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
