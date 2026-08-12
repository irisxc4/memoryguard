from __future__ import annotations

import threading
import time

from memoryguard.runtime_v2.task_coordinator import TaskCoordinator
from memoryguard.runtime_v2.working_memory import RuntimeScope


def _scope(tmp_path):
    return RuntimeScope(
        workspace_id=str(tmp_path.resolve()),
        agent_instance_id="agent-a",
        share_group_id="group-a",
        runtime_scope="gui",
    )


def _wait_terminal(coordinator: TaskCoordinator, run_id: str, scope: RuntimeScope, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = coordinator.status(run_id, scope)
        if status.get("status") in {"succeeded", "failed", "cancelled"}:
            return status
        time.sleep(0.01)
    raise AssertionError("task did not become terminal")


def test_task_coordinator_persists_real_progress_and_success(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)

    def worker(execution):
        execution.progress(20, "scan", item_count=2)
        execution.progress(80, "project", item_count=4)
        return {"result_id": "projection-1"}

    accepted = coordinator.start(
        operation="projection_build",
        idempotency_key="req-1",
        scope=scope,
        worker=worker,
    )
    run_id = accepted["task"]["run_id"]
    final = _wait_terminal(coordinator, run_id, scope)
    assert final["status"] == "succeeded"
    assert final["task"]["progress"] == 100
    assert final["task"]["owned"] is False
    assert coordinator.owned_worker_count() == 0

    restarted = TaskCoordinator(tmp_path)
    recovered = restarted.status(run_id, scope)
    assert recovered["status"] == "succeeded"
    assert recovered["task"]["progress"] == 100
    assert recovered["task"]["owned"] is False


def test_task_coordinator_cancel_waits_for_owned_worker_exit(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    started = threading.Event()

    def worker(execution):
        started.set()
        for index in range(1000):
            execution.check_cancelled()
            if index % 10 == 0:
                execution.progress(min(index // 10, 90), "scan")
            time.sleep(0.002)
        return {"result_id": "unexpected"}

    accepted = coordinator.start(
        operation="knowledge_reingest",
        idempotency_key="req-cancel",
        scope=scope,
        worker=worker,
    )
    run_id = accepted["task"]["run_id"]
    assert started.wait(1.0)
    cancelled = coordinator.cancel(run_id, scope, timeout=5.0)
    assert cancelled["ok"] is True
    assert cancelled["status"] == "cancelled"
    assert coordinator.owned_worker_count() == 0


def test_task_coordinator_shutdown_leaves_no_owned_worker(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    started = threading.Event()

    def worker(execution):
        started.set()
        while True:
            execution.check_cancelled()
            time.sleep(0.002)

    accepted = coordinator.start(
        operation="history_backfill",
        idempotency_key="req-shutdown",
        scope=scope,
        worker=worker,
    )
    assert started.wait(1.0)
    result = coordinator.shutdown(timeout=5.0)
    assert result["ok"] is True
    assert result["alive_workers"] == []
    assert coordinator.owned_worker_count() == 0
    final = coordinator.status(accepted["task"]["run_id"], scope)
    assert final["status"] == "cancelled"


def test_task_coordinator_idempotent_start_reuses_durable_run(tmp_path):
    coordinator = TaskCoordinator(tmp_path)
    scope = _scope(tmp_path)
    calls = 0

    def worker(execution):
        nonlocal calls
        calls += 1
        return {"result_id": "one"}

    first = coordinator.start(
        operation="import_create",
        idempotency_key="same-request",
        scope=scope,
        worker=worker,
    )
    _wait_terminal(coordinator, first["task"]["run_id"], scope)
    second = coordinator.start(
        operation="import_create",
        idempotency_key="same-request",
        scope=scope,
        worker=worker,
    )
    assert second["task"]["run_id"] == first["task"]["run_id"]
    assert calls == 1
