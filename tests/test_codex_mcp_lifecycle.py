from __future__ import annotations

import json
import sqlite3
import threading
import time

from memoryguard import codex_mcp_lifecycle as lifecycle
from memoryguard.codex_mcp_lifecycle import (
    ProcessInfo,
    ProcessSnapshot,
    ThreadLockEvidence,
    handle_codex_mcp_lifecycle,
    reclaim_indexed_terminal_codex_threads,
    reclaim_terminal_codex_threads,
)


class FakeController:
    def __init__(self, snapshot: ProcessSnapshot) -> None:
        self.codex_pid = snapshot.codex_pid
        self.codex_start_ms = snapshot.codex_start_ms
        self.processes = {p.pid: p for p in snapshot.direct_children}
        self.terminated: list[int] = []
        self.orphan_roots: tuple[ProcessInfo, ...] = ()
        self._lock = threading.Lock()

    def snapshot(self) -> ProcessSnapshot:
        with self._lock:
            return ProcessSnapshot(
                self.codex_pid,
                tuple(self.processes.values()),
                self.codex_start_ms,
            )

    def terminate_tree(self, process: ProcessInfo) -> bool:
        with self._lock:
            if self.processes.get(process.pid) != process:
                return False
            self.terminated.append(process.pid)
            self.processes.pop(process.pid, None)
            return True

    def add(self, *processes: ProcessInfo) -> None:
        with self._lock:
            for process in processes:
                self.processes[process.pid] = process

    def remove(self, *pids: int) -> None:
        with self._lock:
            for pid in pids:
                self.processes.pop(pid, None)

    def restart_codex(
        self,
        pid: int,
        processes: tuple[ProcessInfo, ...] = (),
        *,
        start_ms: int = 0,
    ) -> None:
        with self._lock:
            self.codex_pid = pid
            self.codex_start_ms = start_ms
            self.processes = {p.pid: p for p in processes}

    def orphan_mcp_roots(self, codex_pid: int) -> tuple[ProcessInfo, ...]:
        return self.orphan_roots if codex_pid == self.codex_pid else ()


def root(pid: int, name: str, start_ms: int, codex_pid: int = 900) -> ProcessInfo:
    return ProcessInfo(pid, codex_pid, name, start_ms)


def cohort(start_ms: int, base: int, codex_pid: int = 900) -> tuple[ProcessInfo, ...]:
    return (
        root(base, "node_repl.exe", start_ms, codex_pid),
        root(base + 1, "python.exe", start_ms + 20, codex_pid),
        root(base + 2, "cmd.exe", start_ms + 40, codex_pid),
        root(base + 3, "node.exe", start_ms + 60, codex_pid),
    )


def call(tmp_path, controller, event, now_ms, thread="thread-a", mode="auto"):
    return handle_codex_mcp_lifecycle(
        event=event,
        workspace=tmp_path,
        thread_id=thread,
        controller=controller,
        now_ms=now_ms,
        self_pid=99999,
        mode=mode,
    )


def lifecycle_state_path(tmp_path, controller) -> object:
    return lifecycle._state_path(tmp_path.resolve(), controller.snapshot())


def test_terminal_thread_reclaim_kills_only_exact_exclusive_cohort(tmp_path, monkeypatch):
    roots = cohort(995_000, 100)
    controller = FakeController(
        ProcessSnapshot(900, roots, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="child-thread",
        lock_mtime_ms=995_010,
        created_at_ms=900_100,
        updated_at_ms=1_000_000,
        thread_source="subagent",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )

    result = reclaim_terminal_codex_threads(
        workspace=tmp_path,
        thread_ids=["child-thread"],
        controller=controller,
        now_ms=1_010_000,
        self_pid=99999,
    )

    assert result["status"] == "ok"
    assert result["reclaimed_thread_ids"] == ["child-thread"]
    assert set(result["killed_pids"]) == {100, 101, 102, 103}
    assert set(controller.terminated) == {100, 101, 102, 103}


def test_terminal_thread_reclaim_never_kills_shared_cohort(tmp_path, monkeypatch):
    roots = cohort(995_000, 100)
    controller = FakeController(
        ProcessSnapshot(900, roots, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="child-thread",
        lock_mtime_ms=995_010,
        created_at_ms=900_100,
        updated_at_ms=1_000_000,
        thread_source="subagent",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )
    state_path = lifecycle_state_path(tmp_path, controller)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "codex_pid": 900,
                "codex_start_ms": 900_000,
                "generation_key": "900:900000",
                "threads": {
                    "other-live-owner": {
                        "cohort_key": "100:995000",
                        "last_seen_ms": 1_000_000,
                    }
                },
                "retired": {},
            }
        ),
        encoding="utf-8",
    )

    result = reclaim_terminal_codex_threads(
        workspace=tmp_path,
        thread_ids=["child-thread"],
        controller=controller,
        now_ms=1_010_000,
        self_pid=99999,
    )

    assert result["killed_pids"] == []
    assert result["skipped_shared_thread_ids"] == ["child-thread"]
    assert controller.terminated == []


def _write_codex_thread_state(home, rows):
    home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(home / "state_5.sqlite")
    connection.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, archived INTEGER NOT NULL DEFAULT 0)")
    connection.executemany("INSERT INTO threads(id,archived) VALUES(?,?)", rows)
    connection.commit()
    connection.close()


def _seed_idle_verified_lease(tmp_path, controller, *, thread_id="root-thread"):
    started = handle_codex_mcp_lifecycle(
        event="user_prompt",
        workspace=tmp_path,
        thread_id="session:" + lifecycle.hashlib.sha256(thread_id.encode()).hexdigest()[:24],
        host_thread_id=thread_id,
        controller=controller,
        now_ms=1_000_000,
        self_pid=99999,
        mode="auto",
    )
    assert started["assigned_cohort"]
    stopped = handle_codex_mcp_lifecycle(
        event="stop",
        workspace=tmp_path,
        thread_id="session:" + lifecycle.hashlib.sha256(thread_id.encode()).hexdigest()[:24],
        host_thread_id=thread_id,
        controller=controller,
        now_ms=1_001_000,
        self_pid=99999,
        mode="auto",
    )
    assert stopped["assigned_cohort"]


def test_indexed_archived_main_thread_reclaims_idle_exclusive_cohort(tmp_path, monkeypatch):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots, codex_start_ms=900_000))
    _seed_idle_verified_lease(tmp_path, controller)
    codex_home = tmp_path / "codex-home"
    _write_codex_thread_state(codex_home, [("root-thread", 1)])
    monkeypatch.setattr(lifecycle, "_codex_home", lambda: codex_home)

    result = reclaim_indexed_terminal_codex_threads(
        workspace=tmp_path,
        controller=controller,
        now_ms=1_010_000,
        self_pid=99999,
    )

    assert result["terminal_thread_ids"] == ["root-thread"]
    assert result["reclaimed_thread_ids"] == ["root-thread"]
    assert set(result["killed_pids"]) == {100, 101, 102, 103}


def test_indexed_live_main_thread_is_not_reclaimed(tmp_path, monkeypatch):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots, codex_start_ms=900_000))
    _seed_idle_verified_lease(tmp_path, controller)
    codex_home = tmp_path / "codex-home"
    _write_codex_thread_state(codex_home, [("root-thread", 0)])
    monkeypatch.setattr(lifecycle, "_codex_home", lambda: codex_home)

    result = reclaim_indexed_terminal_codex_threads(
        workspace=tmp_path,
        controller=controller,
        now_ms=1_010_000,
        self_pid=99999,
    )

    assert result["reason"] == "indexed_threads_live"
    assert result["killed_pids"] == []
    assert controller.terminated == []


def test_indexed_current_thread_is_protected_even_if_archived(tmp_path, monkeypatch):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots, codex_start_ms=900_000))
    _seed_idle_verified_lease(tmp_path, controller)
    codex_home = tmp_path / "codex-home"
    _write_codex_thread_state(codex_home, [("root-thread", 1)])
    monkeypatch.setattr(lifecycle, "_codex_home", lambda: codex_home)

    result = reclaim_indexed_terminal_codex_threads(
        workspace=tmp_path,
        protected_thread_ids={"root-thread"},
        controller=controller,
        now_ms=1_010_000,
        self_pid=99999,
    )

    assert result["reason"] == "no_indexed_idle_candidates"
    assert result["killed_pids"] == []
    assert controller.terminated == []


def test_auto_mode_quarantines_replaced_cohort_without_terminating_processes(tmp_path):
    old = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, old))
    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"].startswith("100:")

    new = cohort(1_009_000, 200)
    controller.add(*new)
    replaced = call(tmp_path, controller, "post_tool", 1_010_000)
    assert replaced["assigned_cohort"].startswith("200:")
    assert controller.terminated == []

    observed = call(tmp_path, controller, "post_tool", 1_016_000)
    assert observed["action"] == "reclaim_pending"
    assert observed["termination_enabled"] is False
    assert set(observed["reclaim_candidate_pids"]) == {100, 101, 102, 103}
    assert observed["killed_pids"] == []
    assert controller.terminated == []


def test_force_mode_can_reclaim_a_previously_quarantined_cohort(tmp_path):
    old = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, old))
    call(tmp_path, controller, "user_prompt", 1_000_000)
    new = cohort(1_009_000, 200)
    controller.add(*new)
    call(tmp_path, controller, "post_tool", 1_010_000)

    reclaimed = call(tmp_path, controller, "post_tool", 1_016_000, mode="force")
    assert reclaimed["action"] == "reclaimed"
    assert reclaimed["termination_enabled"] is True
    assert set(reclaimed["killed_pids"]) == {100, 101, 102, 103}
    assert not ({200, 201, 202, 203} & set(controller.terminated))


def test_native_cleanup_wins_without_taskkill(tmp_path):
    old = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, old))
    call(tmp_path, controller, "user_prompt", 1_000_000)
    call(tmp_path, controller, "stop", 1_001_000)
    assert controller.terminated == []

    controller.remove(100, 101, 102, 103)
    new = cohort(1_009_000, 200)
    controller.add(*new)
    result = call(tmp_path, controller, "user_prompt", 1_010_000)
    assert result["native_cleanup_count"] == 1
    assert result["action"] == "observing"
    assert controller.terminated == []


def test_native_long_lived_cohort_reuse_cancels_retirement(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    call(tmp_path, controller, "user_prompt", 1_000_000)
    call(tmp_path, controller, "stop", 1_001_000)

    reused = call(tmp_path, controller, "user_prompt", 1_030_000)
    assert reused["assigned_cohort"].startswith("100:")
    assert controller.terminated == []

    later = call(tmp_path, controller, "post_tool", 1_040_000)
    assert later["assigned_cohort"].startswith("100:")
    assert controller.terminated == []


def test_live_transport_lease_is_not_retired_by_bookkeeping_ttl(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"].startswith("100:")

    stopped = call(tmp_path, controller, "stop", 1_001_000)
    assert stopped["assignment_reason"] == "turn_stop_preserved"

    # Well beyond LEASE_TTL_MS. A live process cohort is still transport
    # evidence and must not be retired merely because no turn ran recently.
    later = call(tmp_path, controller, "post_tool", 3_000_000)
    assert later["assigned_cohort"].startswith("100:")
    assert controller.terminated == []
    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["retired"] == {}
    assert state["threads"]["thread-a"]["cohort_key"].startswith("100:")


def test_stop_is_turn_boundary_and_preserves_shared_transport_leases(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    call(tmp_path, controller, "user_prompt", 1_000_000, thread="a")
    call(tmp_path, controller, "user_prompt", 1_000_100, thread="b")

    stopped_a = call(tmp_path, controller, "stop", 1_001_000, thread="a")
    assert stopped_a["assignment_reason"] == "turn_stop_preserved"
    call(tmp_path, controller, "post_tool", 1_010_000, thread="b")

    stopped_b = call(tmp_path, controller, "stop", 1_011_000, thread="b")
    assert stopped_b["assignment_reason"] == "turn_stop_preserved"
    new = cohort(1_019_000, 200)
    controller.add(*new)
    call(tmp_path, controller, "user_prompt", 1_020_000, thread="c")

    # Stop does not mean thread/conversation close. The original shared stdio
    # transport remains leased by resumable threads a and b.
    assert controller.terminated == []
    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["a"]["turn_state"] == "idle"
    assert state["threads"]["b"]["turn_state"] == "idle"
    assert state["threads"]["a"]["cohort_key"].startswith("100:")
    assert state["threads"]["b"]["cohort_key"].startswith("100:")


def test_unknown_old_unowned_cohort_is_never_killed_by_age_alone(tmp_path):
    old = cohort(100_000, 100)
    new = cohort(1_000_000, 200)
    controller = FakeController(ProcessSnapshot(900, old + new))

    call(tmp_path, controller, "user_prompt", 1_000_000)
    call(tmp_path, controller, "post_tool", 1_010_000)
    assert not ({100, 101, 102, 103} & set(controller.terminated))


def test_processes_outside_direct_anchored_cohort_are_never_targeted(tmp_path):
    roots = cohort(995_000, 100)
    protected = (
        root(300, "pwsh.exe", 995_010),
        ProcessInfo(301, 777, "node.exe", 995_020),
    )
    controller = FakeController(ProcessSnapshot(900, roots + protected))
    call(tmp_path, controller, "user_prompt", 1_000_000, mode="force")
    stopped = call(tmp_path, controller, "stop", 1_001_000, mode="force")
    assert stopped["assignment_reason"] == "turn_stop_preserved"
    assert controller.terminated == []
    assert 300 not in controller.terminated
    assert 301 not in controller.terminated


def test_codex_pid_change_discards_previous_generation_state(tmp_path):
    old = cohort(995_000, 100, codex_pid=900)
    controller = FakeController(ProcessSnapshot(900, old))
    call(tmp_path, controller, "user_prompt", 1_000_000)
    call(tmp_path, controller, "stop", 1_001_000)

    fresh = cohort(1_010_000, 200, codex_pid=901)
    controller.restart_codex(901, fresh)
    result = call(tmp_path, controller, "user_prompt", 1_011_000)
    assert result["assigned_cohort"].startswith("200:")
    assert controller.terminated == []


def test_post_tool_probe_is_throttled(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    call(tmp_path, controller, "user_prompt", 1_000_000)
    result = call(tmp_path, controller, "post_tool", 1_002_000)
    assert result["action"] == "throttled"
    assert controller.terminated == []


def test_off_mode_has_no_side_effects(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    result = call(tmp_path, controller, "stop", 1_000_000, mode="off")
    assert result == {"status": "skipped", "reason": "disabled", "mode": "off"}
    assert controller.terminated == []


def test_legacy_adoption_never_age_kills_unknown_cohorts_in_auto_mode(tmp_path):
    old = cohort(100_000, 100)
    mid = cohort(200_000, 200)
    newest = cohort(300_000, 300)
    controller = FakeController(ProcessSnapshot(900, old + mid + newest))

    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"] == ""
    adopted = call(tmp_path, controller, "post_tool", 1_010_000)
    assert adopted["assigned_cohort"].startswith("300:")

    # Five minutes, five hours, or five human product meetings do not turn age
    # into ownership proof. Unknown legacy cohorts remain observe-only in auto.
    later = call(tmp_path, controller, "post_tool", 20_000_000)
    assert later["assigned_cohort"].startswith("300:")
    assert controller.terminated == []


def test_late_session_adopts_only_the_unique_unowned_cohort(tmp_path):
    owned = cohort(995_000, 100)
    unowned = cohort(500_000, 200)
    controller = FakeController(ProcessSnapshot(900, owned + unowned))

    first = call(tmp_path, controller, "user_prompt", 1_000_000, thread="a")
    assert first["assigned_cohort"].startswith("100:")

    late = call(tmp_path, controller, "user_prompt", 1_100_000, thread="b")
    assert late["assigned_cohort"].startswith("200:")
    assert controller.terminated == []

    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["a"]["cohort_key"].startswith("100:")
    assert state["threads"]["b"]["cohort_key"].startswith("200:")


def test_any_pulse_reconciles_one_empty_lease_with_one_unowned_cohort(tmp_path):
    first = cohort(995_000, 100)
    second = cohort(500_000, 200)
    controller = FakeController(ProcessSnapshot(900, first + second))
    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "codex_pid": 900,
                "threads": {
                    "a": {
                        "cohort_key": "100:995000",
                        "last_seen_ms": 1_000_000,
                        "turn_started_ms": 1_000_000,
                    },
                    "b": {
                        "cohort_key": "",
                        "last_seen_ms": 1_000_000,
                        "turn_started_ms": 1_000_000,
                    },
                },
                "retired": {},
            }
        ),
        encoding="utf-8",
    )

    call(tmp_path, controller, "post_tool", 2_000_000, thread="a")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["a"]["cohort_key"].startswith("100:")
    assert state["threads"]["b"]["cohort_key"].startswith("200:")
    assert controller.terminated == []


def test_snapshot_delta_binds_one_new_cohort_after_ambiguous_baseline(tmp_path):
    baseline = cohort(100_000, 100) + cohort(200_000, 200)
    controller = FakeController(ProcessSnapshot(900, baseline))

    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"] == ""

    # The new cohort is deliberately outside the nearest-assignment window;
    # only the persisted before/after snapshot proves which cohort appeared.
    spawned = cohort(900_000, 300)
    controller.add(*spawned)
    result = call(tmp_path, controller, "post_tool", 1_010_000)

    assert result["assigned_cohort"].startswith("300:")
    assert result["assignment_reason"] == "snapshot_delta"
    assert controller.terminated == []

    repeated = call(tmp_path, controller, "post_tool", 1_016_000)
    assert repeated["assigned_cohort"].startswith("300:")
    assert repeated["assignment_reason"] == "snapshot_delta"
    assert repeated["cohort_count"] == result["cohort_count"] == 3
    assert controller.terminated == []

    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    lease = state["threads"]["thread-a"]
    assert lease["assignment_reason"] == "snapshot_delta"
    assert lease["pulse_count"] >= 3
    assert lease["last_observed_cohort_count"] == 3
    assert state["last_assignment_reason"] == "snapshot_delta"


def test_concrete_generation_first_unique_cohort_is_snapshot_delta(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(
        ProcessSnapshot(900, roots, codex_start_ms=900_000)
    )

    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"].startswith("100:")
    assert first["assignment_reason"] == "snapshot_delta"
    assert first["cohort_count"] == 1

    repeated = call(tmp_path, controller, "post_tool", 1_006_000)
    assert repeated["assigned_cohort"] == first["assigned_cohort"]
    assert repeated["assignment_reason"] == "snapshot_delta"
    assert repeated["cohort_count"] == 1
    assert controller.terminated == []

    state = json.loads(
        lifecycle_state_path(tmp_path, controller).read_text(encoding="utf-8")
    )
    lease = state["threads"]["thread-a"]
    assert lease["assignment_reason"] == "snapshot_delta"
    assert lease["last_observed_cohort_count"] == 1
    assert lease["pulse_count"] >= 2


def test_snapshot_delta_preempts_conflicting_restored_writer_lock(
    tmp_path,
    monkeypatch,
):
    roots = cohort(995_000, 100)
    controller = FakeController(
        ProcessSnapshot(900, roots, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="restored-old-thread",
        lock_mtime_ms=995_010,
        created_at_ms=500_000,
        updated_at_ms=800_000,
        thread_source="user",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )

    first = call(
        tmp_path,
        controller,
        "user_prompt",
        1_000_000,
        thread="current-thread",
    )
    assert first["assigned_cohort"].startswith("100:")
    assert first["assignment_reason"] == "snapshot_delta"
    assert controller.terminated == []

    state_path = lifecycle_state_path(tmp_path, controller)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["current-thread"]["cohort_key"].startswith("100:")
    assert evidence.lease_id not in state["threads"]
    assert state["writer_evidence"]["snapshot_reserved_count"] == 1

    repeated = call(
        tmp_path,
        controller,
        "post_tool",
        1_006_000,
        thread="current-thread",
    )
    assert repeated["assigned_cohort"].startswith("100:")
    assert repeated["assignment_reason"] == "snapshot_delta"
    assert repeated["cohort_count"] == first["cohort_count"] == 1
    assert controller.terminated == []

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["current-thread"]["cohort_key"].startswith("100:")
    assert evidence.lease_id not in state["threads"]
    assert state["writer_evidence"]["snapshot_owner_preserved_count"] == 1


def test_snapshot_delta_stays_fail_open_for_multiple_new_cohorts(tmp_path):
    baseline = cohort(100_000, 100) + cohort(200_000, 200)
    controller = FakeController(ProcessSnapshot(900, baseline))
    first = call(tmp_path, controller, "user_prompt", 1_000_000)
    assert first["assigned_cohort"] == ""

    controller.add(*cohort(200_000, 200), *cohort(300_000, 300))
    result = call(tmp_path, controller, "post_tool", 1_000_000)

    assert result["assigned_cohort"] == ""
    assert controller.terminated == []


def test_snapshot_delta_stays_fail_open_with_two_unresolved_threads(tmp_path):
    baseline = cohort(100_000, 100) + cohort(200_000, 200)
    controller = FakeController(ProcessSnapshot(900, baseline))
    assert call(tmp_path, controller, "user_prompt", 1_000_000, thread="a")[
        "assigned_cohort"
    ] == ""
    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["threads"]["b"] = {
        "cohort_key": "",
        "last_seen_ms": 1_000_000,
        "turn_started_ms": 1_000_000,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    controller.add(*cohort(900_000, 300))
    result = call(tmp_path, controller, "post_tool", 1_010_000, thread="a")

    assert result["assigned_cohort"] == ""
    assert controller.terminated == []


def test_writer_evidence_preserves_restored_idle_cohort_until_replacement(
    tmp_path,
    monkeypatch,
):
    restored = cohort(995_000, 100)
    active = cohort(1_009_000, 200)
    controller = FakeController(
        ProcessSnapshot(900, restored + active, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="restored-thread",
        lock_mtime_ms=995_010,
        created_at_ms=500_000,
        updated_at_ms=800_000,
        thread_source="user",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )

    first = call(tmp_path, controller, "user_prompt", 1_010_000, thread="active")
    assert first["assigned_cohort"].startswith("200:")
    second = call(tmp_path, controller, "post_tool", 1_016_000, thread="active")
    assert second["killed_pids"] == []
    assert controller.terminated == []

    state_path = lifecycle_state_path(tmp_path, controller)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    restored_lease = state["threads"][evidence.lease_id]
    assert restored_lease["cohort_key"].startswith("100:")
    assert restored_lease["evidence"] == "writer_lock_restored"
    assert state["writer_evidence"]["restored_preserved_count"] == 1
    assert state["writer_evidence"]["idle_retired_count"] == 0


def test_writer_evidence_retires_exact_old_cohort_when_same_thread_has_newer_runtime(
    tmp_path,
    monkeypatch,
):
    old = cohort(995_000, 100)
    new = cohort(1_009_000, 200)
    controller = FakeController(
        ProcessSnapshot(900, old + new, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="active-thread",
        lock_mtime_ms=995_010,
        created_at_ms=900_100,
        updated_at_ms=1_010_000,
        thread_source="user",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )
    state_path = lifecycle_state_path(tmp_path, controller)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "codex_pid": 900,
                "codex_start_ms": 900_000,
                "threads": {
                    evidence.lease_id: {
                        "cohort_key": "200:1009000",
                        "last_seen_ms": 1_010_000,
                        "turn_started_ms": 900_100,
                    }
                },
                "retired": {},
            }
        ),
        encoding="utf-8",
    )

    first = call(
        tmp_path,
        controller,
        "post_tool",
        1_020_000,
        thread=evidence.lease_id,
    )
    assert first["assigned_cohort"].startswith("200:")
    assert first["killed_pids"] == []

    second = call(
        tmp_path,
        controller,
        "post_tool",
        1_026_000,
        thread=evidence.lease_id,
    )
    assert second["action"] == "reclaim_pending"
    assert set(second["reclaim_candidate_pids"]) == {100, 101, 102, 103}
    assert second["killed_pids"] == []
    assert controller.terminated == []


def test_writer_evidence_corrects_wrong_owner_without_retiring_live_transport(
    tmp_path,
    monkeypatch,
):
    restored = cohort(995_000, 100)
    controller = FakeController(
        ProcessSnapshot(900, restored, codex_start_ms=900_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="restored-thread",
        lock_mtime_ms=995_010,
        created_at_ms=500_000,
        updated_at_ms=800_000,
        thread_source="user",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )
    state_path = lifecycle_state_path(tmp_path, controller)
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "codex_pid": 900,
                "codex_start_ms": 900_000,
                "threads": {
                    "wrong-thread": {
                        "cohort_key": "100:995000",
                        "last_seen_ms": 1_000_000,
                        "turn_started_ms": 1_000_000,
                    }
                },
                "retired": {},
            }
        ),
        encoding="utf-8",
    )

    call(tmp_path, controller, "post_tool", 1_001_000, thread="wrong-thread")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["threads"]["wrong-thread"]["cohort_key"] == ""
    assert state["threads"][evidence.lease_id]["cohort_key"] == "100:995000"
    assert state["retired"] == {}
    assert controller.terminated == []


def test_writer_evidence_disables_ambiguous_unique_unowned_guess(
    tmp_path,
    monkeypatch,
):
    first = cohort(500_000, 100)
    second = cohort(600_000, 200)
    controller = FakeController(
        ProcessSnapshot(900, first + second, codex_start_ms=400_000)
    )
    evidence = ThreadLockEvidence(
        thread_id="some-live-thread",
        lock_mtime_ms=450_000,
        created_at_ms=400_100,
        updated_at_ms=700_000,
        thread_source="user",
    )
    monkeypatch.setattr(
        lifecycle,
        "_read_thread_lock_evidence",
        lambda snapshot: (evidence,),
    )

    result = call(tmp_path, controller, "user_prompt", 1_000_000, thread="new")
    assert result["assigned_cohort"] == ""
    assert controller.terminated == []


def test_legacy_migration_drains_only_proven_orphan_mcp_root(tmp_path):
    live = cohort(995_000, 100)
    orphan = root(500, "cmd.exe", 500_000)
    controller = FakeController(ProcessSnapshot(900, live + (orphan,)))
    controller.orphan_roots = (orphan,)

    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "codex_pid": 900,
                "threads": {
                    "a": {
                        "cohort_key": "100:995000",
                        "last_seen_ms": 1_000_000,
                        "turn_started_ms": 1_000_000,
                    }
                },
                "retired": {},
                "legacy_started_ms": 1_000_000,
                "legacy_candidates": {},
            }
        ),
        encoding="utf-8",
    )

    result = call(
        tmp_path,
        controller,
        "post_tool",
        1_400_000,
        thread="a",
        mode="force",
    )
    assert 500 in result["killed_pids"]
    assert set(controller.terminated) == {500}
    assert all(process.pid not in controller.terminated for process in live)


def test_distinct_codex_generations_use_isolated_state_shards(tmp_path):
    now_ms = int(time.time() * 1000)
    first_roots = cohort(now_ms - 5_000, 100, codex_pid=900)
    second_roots = cohort(now_ms - 4_000, 200, codex_pid=901)
    first = FakeController(
        ProcessSnapshot(900, first_roots, codex_start_ms=now_ms - 60_000)
    )
    second = FakeController(
        ProcessSnapshot(901, second_roots, codex_start_ms=now_ms - 50_000)
    )

    first_result = call(tmp_path, first, "user_prompt", now_ms, thread="desktop")
    second_result = call(
        tmp_path,
        second,
        "user_prompt",
        now_ms + 1_000,
        thread="ephemeral",
    )

    first_path = lifecycle_state_path(tmp_path, first)
    second_path = lifecycle_state_path(tmp_path, second)
    assert first_result["state_path"] == str(first_path)
    assert second_result["state_path"] == str(second_path)
    assert first_result["generation_key"] == f"900:{now_ms - 60_000}"
    assert second_result["generation_key"] == f"901:{now_ms - 50_000}"
    assert first_path != second_path
    assert set(json.loads(first_path.read_text(encoding="utf-8"))["threads"]) == {
        "desktop"
    }
    assert set(json.loads(second_path.read_text(encoding="utf-8"))["threads"]) == {
        "ephemeral"
    }

    index_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["index_version"] == 1
    assert set(index["generations"]) == {
        f"900:{now_ms - 60_000}",
        f"901:{now_ms - 50_000}",
    }


def test_pid_reuse_with_new_start_time_gets_a_new_state_shard(tmp_path):
    now_ms = int(time.time() * 1000)
    first = FakeController(
        ProcessSnapshot(
            900,
            cohort(now_ms - 5_000, 100, codex_pid=900),
            codex_start_ms=now_ms - 60_000,
        )
    )
    second = FakeController(
        ProcessSnapshot(
            900,
            cohort(now_ms + 5_000, 200, codex_pid=900),
            codex_start_ms=now_ms + 1_000,
        )
    )

    call(tmp_path, first, "user_prompt", now_ms, thread="old-generation")
    call(tmp_path, second, "user_prompt", now_ms + 10_000, thread="new-generation")

    first_path = lifecycle_state_path(tmp_path, first)
    second_path = lifecycle_state_path(tmp_path, second)
    assert first_path != second_path
    assert "old-generation" in json.loads(first_path.read_text(encoding="utf-8"))["threads"]
    assert "new-generation" in json.loads(second_path.read_text(encoding="utf-8"))["threads"]


def test_concurrent_leases_do_not_overwrite_each_other(tmp_path):
    roots = cohort(995_000, 100)
    controller = FakeController(ProcessSnapshot(900, roots))
    barrier = threading.Barrier(2)

    def worker(thread_id: str) -> None:
        barrier.wait()
        call(tmp_path, controller, "user_prompt", 1_000_000, thread=thread_id)

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state_path = tmp_path / ".memoryguard" / "hook-runtime" / "codex-mcp-lifecycle.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["shim_version"] == "0.7.1.post17"
    assert set(state["threads"]) == {"a", "b"}
