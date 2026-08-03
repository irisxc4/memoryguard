"""Workspace governance lock and legacy transaction projection barriers."""

from __future__ import annotations

import multiprocessing
import threading

import pytest

from memoryguard.governance_lock import (
    GovernanceLockTimeout,
    WorkspaceGovernanceLock,
)
from memoryguard.merge_governance_coordinator import (
    MergeGovernanceCoordinator,
    ProjectionBarrierState,
)
from memoryguard.schema_v3 import (
    MemoryKind,
    RuleMatchFeedback,
    RuleMatchReceipt,
    SharedMemoryRecord,
    SharedMemoryStatus,
    _now_iso,
)
from memoryguard.shared_memory_store import SharedMemoryStore


def _record(memory_id: str) -> SharedMemoryRecord:
    now = _now_iso()
    return SharedMemoryRecord(
        memory_id=memory_id,
        body=f"governance-lock-{memory_id}",
        kind=MemoryKind.PROCEDURE,
        status=SharedMemoryStatus.ACTIVE,
        injection_policy="always",
        agent_instance_id="agent-a",
        created_at=now,
        updated_at=now,
    )


def _receipt(group_id: str = "lock-group") -> RuleMatchReceipt:
    return RuleMatchReceipt(
        receipt_id="receipt-lock",
        memory_id="base-rule",
        share_group_id=group_id,
        agent_instance_id="agent-a",
        task_hash="task-lock",
        task="governance lock test",
        created_at=_now_iso(),
    )


def _feedback() -> RuleMatchFeedback:
    return RuleMatchFeedback(
        feedback_id="feedback-lock",
        receipt_id="receipt-lock",
        outcome="followed",
        actor="agent-a",
        source="agent",
        authority=3,
    )


def _feedback_with_id(feedback_id: str) -> RuleMatchFeedback:
    item = _feedback()
    item.feedback_id = feedback_id
    return item


def _projection_state(store: SharedMemoryStore) -> dict:
    state = {
        "scopes": [],
        "projection_lag": 0,
        "projection_error": "",
    }

    def sync() -> dict:
        high_water = store.rule_event_high_water()
        if high_water["total"]:
            state["scopes"] = [{
                "scope_id": store.group_id,
                "last_outbox_event_id": high_water["latest_event_id"],
                "last_projected_event_id": high_water["latest_event_id"],
            }]
        state["projection_lag"] = high_water["pending"]
        return state

    return {"state": state, "sync": sync}


def _drain_all(store: SharedMemoryStore, projection: dict) -> None:
    for event in store.list_unconsumed_rule_events():
        store.mark_rule_event_consumed(event["event_id"])
    projection["sync"]()


def _hold_lock_in_process(workspace: str, ready, release) -> None:
    with WorkspaceGovernanceLock(workspace, timeout=2.0, poll_interval=0.01):
        ready.set()
        release.wait(3.0)


def test_lock_is_reentrant_and_exception_safe(tmp_path):
    lock = WorkspaceGovernanceLock(tmp_path, timeout=0.2, poll_interval=0.01)

    with pytest.raises(RuntimeError, match="boom"):
        with lock:
            with WorkspaceGovernanceLock(tmp_path, timeout=0.2):
                raise RuntimeError("boom")

    assert lock.path == tmp_path / ".memoryguard" / "governance.lock"
    assert lock.path.exists()
    with WorkspaceGovernanceLock(tmp_path, timeout=0.2):
        pass


def test_lock_is_cross_process(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lock_in_process,
        args=(str(tmp_path), ready, release),
    )
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


def test_lock_timeout_is_explicit_and_does_not_bypass_owner(tmp_path):
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
    assert not errors
    assert not thread.is_alive()


def test_legacy_rule_and_feedback_mutations_wait_then_commit_with_outbox(tmp_path):
    store = SharedMemoryStore(tmp_path, "lock-group")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))

    started = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    errors: list[BaseException] = []

    def append_rule() -> None:
        started[0].set()
        try:
            store.append_record(_record("blocked-rule"), emit_lifecycle_outbox=True)
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)
        finally:
            finished[0].set()

    def append_feedback() -> None:
        started[1].set()
        try:
            store.append_rule_match_feedback(_feedback())
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            errors.append(exc)
        finally:
            finished[1].set()

    with store.governance_lock(timeout=1.0, poll_interval=0.01):
        threads = [
            threading.Thread(target=append_rule),
            threading.Thread(target=append_feedback),
        ]
        for thread in threads:
            thread.start()
        for event in started:
            assert event.wait(1.0)
        assert not any(event.wait(0.15) for event in finished)
        assert store.get_record("blocked-rule") is None
        assert store.list_rule_match_feedbacks(receipt_id="receipt-lock") == []

    for thread in threads:
        thread.join(2.0)
    assert not errors
    assert all(event.is_set() for event in finished)
    assert store.get_record("blocked-rule") is not None
    assert [item.feedback_id for item in store.list_rule_match_feedbacks(
        receipt_id="receipt-lock"
    )] == ["feedback-lock"]
    outbox = store.list_unconsumed_rule_events()
    assert any(item["memory_id"] == "blocked-rule" for item in outbox)
    assert any(item["feedback_id"] == "feedback-lock" for item in outbox)


def test_feedback_and_outbox_roll_back_together_and_lock_releases(tmp_path, monkeypatch):
    store = SharedMemoryStore(tmp_path, "lock-group")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))

    def fail_outbox(*_args, **_kwargs):
        raise RuntimeError("outbox fault")

    monkeypatch.setattr(store, "_enqueue_rule_feedback_event", fail_outbox)
    with pytest.raises(RuntimeError, match="outbox fault"):
        store.append_rule_match_feedback(_feedback())
    assert store.list_rule_match_feedbacks(receipt_id="receipt-lock") == []
    assert store.list_unconsumed_rule_events() == []

    monkeypatch.undo()
    store.append_rule_match_feedback(_feedback())
    assert len(store.list_rule_match_feedbacks(receipt_id="receipt-lock")) == 1
    assert len(store.list_unconsumed_rule_events()) == 1


def test_projection_barrier_serializes_producer_after_final_recheck(tmp_path):
    store = SharedMemoryStore(tmp_path, "barrier-producer")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))
    store.append_rule_match_feedback(_feedback_with_id("feedback-before"))
    projection = _projection_state(store)
    drain_entered = threading.Event()
    allow_drain = threading.Event()
    producer_done = threading.Event()
    merge_called = threading.Event()
    result_box: list = []

    def drain() -> None:
        drain_entered.set()
        assert allow_drain.wait(2.0)
        _drain_all(store, projection)

    coordinator = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[store],
        drain_callback=drain,
        projection_status=lambda: projection["state"],
        timeout=2.0,
        poll_interval=0.01,
    )

    def producer() -> None:
        store.append_rule_match_feedback(_feedback_with_id("feedback-after"))
        producer_done.set()

    coordinator_thread = threading.Thread(target=lambda: result_box.append(
        coordinator.run_merge(lambda: merge_called.set())
    ))
    coordinator_thread.start()
    assert drain_entered.wait(1.0)
    # Coordinator owns lock before producer starts its mutation.
    producer_thread = threading.Thread(target=producer)
    producer_thread.start()
    assert not producer_done.wait(0.15)
    allow_drain.set()
    coordinator_thread.join(2.0)
    producer_thread.join(2.0)

    assert not coordinator_thread.is_alive()
    assert not producer_thread.is_alive()
    assert result_box[0].state is ProjectionBarrierState.COMMITTED
    assert merge_called.is_set()
    # Producer committed only after coordinator's final check and release.
    assert [item["feedback_id"] for item in store.list_unconsumed_rule_events()] == [
        "feedback-after"
    ]


def test_two_consumers_checkpoint_out_of_order_without_losing_event(tmp_path):
    store = SharedMemoryStore(tmp_path, "barrier-consumers")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))
    store.append_rule_match_feedback(_feedback_with_id("feedback-old"))
    store.append_rule_match_feedback(_feedback_with_id("feedback-new"))
    projection = _projection_state(store)
    first_done = threading.Event()
    second_attempted = threading.Event()
    release_first = threading.Event()
    results: dict[str, object] = {}

    def consume_newest() -> None:
        events = store.list_unconsumed_rule_events()
        store.mark_rule_event_consumed(events[-1]["event_id"])
        projection["sync"]()
        first_done.set()
        assert release_first.wait(2.0)

    def consume_oldest() -> None:
        assert first_done.wait(2.0)
        for event in store.list_unconsumed_rule_events():
            store.mark_rule_event_consumed(event["event_id"])
        projection["sync"]()

    first = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[store],
        drain_callback=consume_newest,
        projection_status=lambda: projection["state"],
        timeout=2.0,
        poll_interval=0.01,
    )
    second = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[store],
        drain_callback=consume_oldest,
        projection_status=lambda: projection["state"],
        timeout=2.0,
        poll_interval=0.01,
    )
    t1 = threading.Thread(target=lambda: results.setdefault(
        "first", first.run_merge(lambda: pytest.fail("partial drain must not merge"))
    ))
    def run_second() -> None:
        second_attempted.set()
        results.setdefault("second", second.run_merge(lambda: "merged"))

    t2 = threading.Thread(target=run_second)
    t1.start()
    assert first_done.wait(1.0)
    t2.start()
    # Second consumer contends on same workspace lock while first holds it.
    assert second_attempted.wait(1.0)
    release_first.set()
    t1.join(2.0)
    t2.join(2.0)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert results["first"].state is ProjectionBarrierState.BLOCKED
    assert results["second"].state is ProjectionBarrierState.COMMITTED
    assert store.list_unconsumed_rule_events() == []


def test_projection_barrier_detects_high_water_drift_and_retries(tmp_path):
    store = SharedMemoryStore(tmp_path, "barrier-drift")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))
    store.append_rule_match_feedback(_feedback_with_id("feedback-before"))
    projection = _projection_state(store)
    _drain_all(store, projection)
    first = True

    def merge_with_drift() -> str:
        nonlocal first
        if first:
            first = False
            store.append_rule_match_feedback(_feedback_with_id("feedback-drift"))
            for event in store.list_unconsumed_rule_events():
                store.mark_rule_event_consumed(event["event_id"])
            projection["sync"]()
        return "not committed"

    coordinator = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[store],
        drain_callback=lambda: _drain_all(store, projection),
        projection_status=lambda: projection["state"],
        timeout=2.0,
        poll_interval=0.01,
    )
    failed = coordinator.run_merge(merge_with_drift)
    assert failed.state is ProjectionBarrierState.FAILED
    assert failed.error == "projection_barrier_committed_high_water_drift"
    retried = coordinator.run_merge(lambda: "merged")
    assert retried.state is ProjectionBarrierState.COMMITTED


def test_callback_exception_releases_lock_and_retry_succeeds(tmp_path):
    store = SharedMemoryStore(tmp_path, "barrier-retry")
    store.append_record(_record("base-rule"))
    store.append_rule_match_receipt(_receipt(store.group_id))
    store.append_rule_match_feedback(_feedback_with_id("feedback-before"))
    projection = _projection_state(store)

    coordinator = MergeGovernanceCoordinator(
        tmp_path,
        legacy_stores=[store],
        drain_callback=lambda: _drain_all(store, projection),
        projection_status=lambda: projection["state"],
        timeout=2.0,
        poll_interval=0.01,
    )
    failed = coordinator.run_merge(lambda: (_ for _ in ()).throw(RuntimeError("merge fault")))
    assert failed.state is ProjectionBarrierState.FAILED
    assert failed.exception_type == "RuntimeError"

    producer_acquired = threading.Event()
    producer_errors: list[BaseException] = []

    def producer() -> None:
        try:
            # Signal the synchronization point that matters here: a different
            # thread can acquire the workspace lock after callback failure.
            # The write itself also updates a JSONL backup, so using its
            # completion as the lock-release signal makes this test depend on
            # unrelated filesystem latency.
            with store.governance_lock(timeout=1.0, poll_interval=0.01):
                producer_acquired.set()
                store.append_rule_match_feedback(
                    _feedback_with_id("feedback-after-fault")
                )
        except BaseException as exc:  # pragma: no cover - diagnostic guard
            producer_errors.append(exc)

    producer_thread = threading.Thread(target=producer)
    producer_thread.start()
    assert producer_acquired.wait(1.0)
    producer_thread.join()
    assert not producer_errors
    assert coordinator.run_merge(lambda: "retry").state is ProjectionBarrierState.COMMITTED
