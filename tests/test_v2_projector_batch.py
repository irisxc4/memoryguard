from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from memoryguard.evidence import EvidenceStore
from memoryguard.memory import MemoryAtomStore
from memoryguard.storage.transaction import transaction


def _seed_events(memory: MemoryAtomStore, count: int) -> list[str]:
    """Seed pending memory outbox directly, avoiding per-event setup cost."""
    event_ids: list[str] = []
    conn = memory._checked_connect(readonly=False)
    try:
        with transaction(conn):
            for index in range(count):
                event_id = hashlib.sha256(f"batch-event-{index}".encode()).hexdigest()
                payload = {
                    "evidence": {
                        "evidence_id": f"batch-evidence-{index}",
                        "source_ref": f"fixture/source/{index}",
                        "revision": "r1",
                        "digest": hashlib.sha256(f"digest-{index}".encode()).hexdigest(),
                        "authority": "observed",
                        "status": "valid",
                        "metadata": {"index": index},
                    },
                    "subject_type": "atom",
                    "subject_id": f"batch-atom-{index}",
                    "relation": "supports",
                    "link_metadata": {"index": index},
                }
                conn.execute(
                    "INSERT INTO domain_outbox(event_id,sequence,event_type,aggregate_id,payload_json,status,attempts,created_at,projected_at,error_json) VALUES(?,?,?,?,?,'pending',0,?,?,?)",
                    (event_id, index + 1, "evidence.put_link", f"batch-atom-{index}", json.dumps(payload, sort_keys=True), "fixture", "", "{}"),
                )
                event_ids.append(event_id)
    finally:
        conn.close()
    return event_ids


def test_projector_batches_1000_events_and_replay_is_idempotent(tmp_path: Path):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    _seed_events(memory, 1000)

    started = time.perf_counter()
    result = memory.project_evidence(evidence)
    elapsed = time.perf_counter() - started

    assert result == {"projected": 1000, "failed": 0, "pending": 0}
    assert elapsed < 10.0
    with evidence._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1000
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 1000
    # No pending rows means replay does no writes and keeps stable IDs.
    assert memory.project_evidence(evidence) == {"projected": 0, "failed": 0, "pending": 0}


def test_projector_failure_marks_only_failed_batch_then_replays(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    _seed_events(memory, 150)
    original = evidence.project_batch
    state = {"failed": False}

    def fail_once(events):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("injected evidence transaction failure")
        return original(events)

    monkeypatch.setattr(evidence, "project_batch", fail_once)
    first = memory.project_evidence(evidence)
    assert first["failed"] == 100
    assert first["projected"] == 50
    assert first["pending"] == 100
    with memory._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_projection_receipts").fetchone()[0] == 50
        assert conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='failed'").fetchone()[0] == 100
    # Failed is an outstanding, retryable state.  No manual SQL state rewrite
    # is required before the next safe idempotent projection attempt.
    monkeypatch.setattr(evidence, "project_batch", original)
    replay = memory.project_evidence(evidence)
    assert replay == {"projected": 100, "failed": 0, "pending": 0}
    with memory._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_projection_receipts").fetchone()[0] == 150
    with evidence._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 150
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 150


def test_projector_crash_after_evidence_commit_replays_without_duplicates(tmp_path: Path, monkeypatch):
    memory = MemoryAtomStore(tmp_path)
    evidence = EvidenceStore(tmp_path)
    _seed_events(memory, 2)
    original_mark = memory._mark_projected_batch
    state = {"raised": False}

    def crash_once(events, evidence_ids):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("injected memory receipt failure")
        return original_mark(events, evidence_ids)

    monkeypatch.setattr(memory, "_mark_projected_batch", crash_once)
    try:
        memory.project_evidence(evidence)
    except RuntimeError as exc:
        assert "receipt failure" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("injected receipt failure was not raised")
    with evidence._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 2
    with memory._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence_projection_receipts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM domain_outbox WHERE status='pending'").fetchone()[0] == 2
    monkeypatch.setattr(memory, "_mark_projected_batch", original_mark)
    assert memory.project_evidence(evidence) == {"projected": 2, "failed": 0, "pending": 0}
    with evidence._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 2
