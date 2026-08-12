"""V2 workspace lock, memory outbox, and evidence projection barriers."""
from __future__ import annotations

import multiprocessing
import sqlite3
import threading
from pathlib import Path

import pytest

from memoryguard.evidence import EvidenceStore
from memoryguard.governance_lock import GovernanceLockTimeout, WorkspaceGovernanceLock
from memoryguard.governance_v2 import GovernanceV2, V2MutationContext
from memoryguard.memory import MemoryAtom, MemoryAtomStore


def _context(root: Path, group: str = "lock-group", agent: str = "agent-a") -> V2MutationContext:
    return V2MutationContext(
        workspace_id=str(root.resolve()),
        share_group_id=group,
        agent_instance_id=agent,
        project_ref="",
        provider="",
        runtime_role="",
        actor=agent,
        authority="manual",
        admin=True,
    )


def _put(root: Path, memory_id: str, *, group: str = "lock-group", agent: str = "agent-a") -> MemoryAtomStore:
    memory = MemoryAtomStore(root)
    evidence = EvidenceStore(root)
    governance = GovernanceV2(root, memory_store=memory, evidence_store=evidence)
    governance.put_atom(
        MemoryAtom(
            memory_id=memory_id,
            body=f"governance-lock-{memory_id}",
            kind="procedure",
            injection_policy="always",
            priority=10,
            workspace_id=str(root.resolve()),
            share_group_id=group,
            agent_instance_id=agent,
        ),
        context=_context(root, group, agent),
        evidence=[{"source_ref": f"barrier:{memory_id}"}],
        reason="V2 barrier fixture",
        idempotency_key=f"barrier:{memory_id}",
    )
    return memory


def _drain(memory: MemoryAtomStore, evidence: EvidenceStore) -> dict[str, int]:
    result = {"projected": 0, "failed": 0, "pending": 0}
    while memory.pending_outbox(include_failed=True):
        current = memory.project_evidence(evidence)
        result["projected"] += current["projected"]
        result["failed"] += current["failed"]
        result["pending"] = current["pending"]
        if current["failed"] and current["projected"] == 0:
            break
    return result


def _watermark(root: Path) -> int:
    db = MemoryAtomStore(root).db_path
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT last_sequence FROM outbox_checkpoints WHERE domain='memory'"
        ).fetchone()
    return int(row[0] or 0) if row else 0


def _barrier(
    root: Path,
    memory: MemoryAtomStore,
    drain_callback,
    commit_callback,
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Run the native two-check barrier around the V2 memory outbox."""
    evidence = EvidenceStore(root)
    with WorkspaceGovernanceLock(root, timeout=timeout, poll_interval=0.01):
        pending_before = memory.pending_outbox(include_failed=True)
        before = max(
            [_watermark(root)]
            + [int(item.get("sequence") or 0) for item in pending_before]
        )
        drain_callback(memory, evidence)
        if memory.pending_outbox(include_failed=True):
            return {"state": "BLOCKED", "error": "projection_lag"}
        commit_callback(memory, evidence)
        pending_after = memory.pending_outbox(include_failed=True)
        after = max(
            [_watermark(root)]
            + [int(item.get("sequence") or 0) for item in pending_after]
        )
        if after != before:
            return {
                "state": "FAILED",
                "error": "projection_barrier_committed_high_water_drift",
            }
        return {"state": "COMMITTED", "error": ""}


def _hold_lock_in_process(workspace: str, ready, release) -> None:
    with WorkspaceGovernanceLock(workspace, timeout=2.0, poll_interval=0.01):
        ready.set()
        release.wait(3.0)


def test_lock_is_reentrant_and_exception_safe(tmp_path: Path):
    lock = WorkspaceGovernanceLock(tmp_path, timeout=0.2, poll_interval=0.01)
    with pytest.raises(RuntimeError, match="boom"):
        with lock:
            with WorkspaceGovernanceLock(tmp_path, timeout=0.2):
                raise RuntimeError("boom")
    assert lock.path == tmp_path / ".memoryguard" / "governance.lock"
    assert lock.path.exists()
    with WorkspaceGovernanceLock(tmp_path, timeout=0.2):
        pass


def test_lock_is_cross_process(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock_in_process, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(2.0)
        with pytest.raises(GovernanceLockTimeout):
            with WorkspaceGovernanceLock(tmp_path, timeout=0.15, poll_interval=0.01):
                pass
    finally:
        release.set()
        process.join(3.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
    assert process.exitcode == 0


def test_lock_timeout_is_explicit_and_does_not_bypass_owner(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def holder() -> None:
        try:
            with WorkspaceGovernanceLock(tmp_path, timeout=1.0):
                entered.set()
                release.wait(2.0)
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(1.0)
    with pytest.raises(GovernanceLockTimeout):
        with WorkspaceGovernanceLock(tmp_path, timeout=0.05, poll_interval=0.01):
            pass
    release.set()
    thread.join(2.0)
    assert not errors and not thread.is_alive()


def test_v2_memory_mutations_wait_then_commit_with_evidence_outbox(tmp_path: Path):
    memory = _put(tmp_path, "base-rule")
    evidence = EvidenceStore(tmp_path)
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def producer() -> None:
        started.set()
        try:
            with WorkspaceGovernanceLock(tmp_path, timeout=2.0, poll_interval=0.01):
                _put(tmp_path, "blocked-rule")
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)
        finally:
            finished.set()

    with WorkspaceGovernanceLock(tmp_path, timeout=1.0, poll_interval=0.01):
        thread = threading.Thread(target=producer)
        thread.start()
        assert started.wait(1.0)
        assert not finished.wait(0.15)
        assert memory.get_atom("blocked-rule", scope=_context(tmp_path).to_dict(), include_building=True) is None
    thread.join(2.0)
    assert not errors and finished.is_set()
    assert memory.get_atom("blocked-rule", scope=_context(tmp_path).to_dict(), include_building=True) is not None
    assert _drain(memory, evidence)["failed"] == 0
    assert memory.pending_outbox(include_failed=True) == []


def test_evidence_projection_failure_is_retryable_and_lock_releases(tmp_path: Path, monkeypatch):
    memory = _put(tmp_path, "rollback-rule")
    evidence = EvidenceStore(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("evidence fault")

    monkeypatch.setattr(evidence, "project_batch", fail)
    failed = memory.project_evidence(evidence)
    assert failed["failed"] > 0
    assert memory.pending_outbox(include_failed=True)
    monkeypatch.undo()
    assert _drain(memory, EvidenceStore(tmp_path))["failed"] == 0
    assert memory.pending_outbox(include_failed=True) == []
    with WorkspaceGovernanceLock(tmp_path, timeout=0.2):
        pass


def test_projection_barrier_serializes_producer_after_final_recheck(tmp_path: Path):
    memory = _put(tmp_path, "barrier-base")
    evidence = EvidenceStore(tmp_path)
    _drain(memory, evidence)
    drain_entered = threading.Event()
    allow_drain = threading.Event()
    producer_done = threading.Event()
    merge_called = threading.Event()

    def drain(_memory, _evidence):
        drain_entered.set()
        assert allow_drain.wait(2.0)
        _drain(_memory, _evidence)

    def producer() -> None:
        with WorkspaceGovernanceLock(tmp_path, timeout=2.0, poll_interval=0.01):
            _put(tmp_path, "producer-after")
            producer_done.set()

    result_box: list[dict[str, object]] = []
    coordinator = threading.Thread(target=lambda: result_box.append(
        _barrier(tmp_path, memory, drain, lambda _m, _e: merge_called.set())
    ))
    coordinator.start()
    assert drain_entered.wait(1.0)
    producer = threading.Thread(target=producer)
    producer.start()
    assert not producer_done.wait(0.15)
    allow_drain.set()
    coordinator.join(2.0)
    producer.join(2.0)
    assert result_box[0]["state"] == "COMMITTED"
    assert merge_called.is_set() and producer_done.is_set()
    assert memory.pending_outbox(include_failed=True)


def test_two_consumers_checkpoint_out_of_order_without_losing_event(tmp_path: Path):
    memory = _put(tmp_path, "consumer-old")
    _put(tmp_path, "consumer-new")
    evidence = EvidenceStore(tmp_path)
    partial = _barrier(
        tmp_path,
        memory,
        lambda m, e: m.project_evidence(e, limit=1),
        lambda _m, _e: pytest.fail("partial drain must not commit"),
    )
    assert partial["state"] == "BLOCKED"
    committed = _barrier(
        tmp_path,
        memory,
        lambda m, e: _drain(m, e),
        lambda _m, _e: "merged",
    )
    assert committed["state"] == "COMMITTED"
    assert memory.pending_outbox(include_failed=True) == []


def test_projection_barrier_detects_high_water_drift_and_retries(tmp_path: Path):
    memory = _put(tmp_path, "drift-base")
    _drain(memory, EvidenceStore(tmp_path))
    first = True

    def merge_with_drift(_memory, _evidence):
        nonlocal first
        if first:
            first = False
            _put(tmp_path, "drift-event")
            _drain(MemoryAtomStore(tmp_path), EvidenceStore(tmp_path))

    failed = _barrier(tmp_path, memory, lambda m, e: _drain(m, e), merge_with_drift)
    assert failed["state"] == "FAILED"
    assert failed["error"] == "projection_barrier_committed_high_water_drift"
    retried = _barrier(tmp_path, memory, lambda m, e: _drain(m, e), lambda _m, _e: "merged")
    assert retried["state"] == "COMMITTED"


def test_callback_exception_releases_lock_and_retry_succeeds(tmp_path: Path):
    memory = _put(tmp_path, "retry-base")
    _drain(memory, EvidenceStore(tmp_path))

    def explode(_memory, _evidence):
        raise RuntimeError("merge fault")

    with pytest.raises(RuntimeError, match="merge fault"):
        _barrier(tmp_path, memory, lambda m, e: _drain(m, e), explode)

    acquired = threading.Event()
    errors: list[BaseException] = []

    def producer() -> None:
        try:
            with WorkspaceGovernanceLock(tmp_path, timeout=1.0, poll_interval=0.01):
                acquired.set()
                _put(tmp_path, "retry-after-fault")
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)

    thread = threading.Thread(target=producer)
    thread.start()
    assert acquired.wait(1.0)
    thread.join(2.0)
    assert not errors
    assert _barrier(tmp_path, memory, lambda m, e: _drain(m, e), lambda _m, _e: "retry")["state"] == "COMMITTED"
