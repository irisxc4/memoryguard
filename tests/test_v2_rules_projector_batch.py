from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from memoryguard.rules.v2_store import EvidenceProjectionError, EvidenceProjector, RuleV2Store
from memoryguard.evidence import EvidenceStore


def _seed(store: RuleV2Store, count: int, migration_id: str = "batch") -> None:
    def op(conn):
        for index in range(count):
            conn.execute(
                "INSERT INTO rule_evidence_outbox(event_id,migration_id,evidence_id,definition_id,evidence_ref,content_digest,polarity,source_kind,source_group_id,payload_json,created_at,consumed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"event-{index}", migration_id, f"evidence-{index}", f"definition-{index}", f"source/{index}", f"digest-{index}", "positive", "fixture", "group", json.dumps({"source_ref": f"source/{index}", "content_digest": f"digest-{index}"}), "t0", ""),
            )
    store._write(op)


class _BatchSink:
    def __init__(self, *, fail_once: bool = False):
        self.batches: list[int] = []
        self.fail_once = fail_once

    def write_batch(self, references):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected sink batch failure")
        self.batches.append(len(references))


def test_rules_projector_1000_batch_benchmark_and_replay(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    _seed(store, 1000)
    sink = _BatchSink()
    started = time.perf_counter()
    result = EvidenceProjector(store, sink).project(migration_id="batch")
    elapsed = time.perf_counter() - started
    assert result == {"seen": 1000, "consumed": 1000, "pending": 0}
    assert elapsed < 10.0
    assert sink.batches == [100] * 10
    assert EvidenceProjector(store, sink).project(migration_id="batch") == {"seen": 0, "consumed": 0, "pending": 0}


def test_rules_projector_batch_failure_and_replay_no_false_consumed(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    _seed(store, 150)
    sink = _BatchSink(fail_once=True)
    with pytest.raises(EvidenceProjectionError, match="injected sink batch failure"):
        EvidenceProjector(store, sink).project(migration_id="batch")
    assert store.list_evidence_outbox(migration_id="batch", unconsumed=True)
    result = EvidenceProjector(store, sink).project(migration_id="batch")
    assert result == {"seen": 150, "consumed": 150, "pending": 0}


def test_rules_projector_crash_after_sink_success_replays_safely(tmp_path: Path, monkeypatch):
    store = RuleV2Store(tmp_path)
    _seed(store, 100)
    sink = _BatchSink()
    projector = EvidenceProjector(store, sink)
    original_mark = store.mark_evidence_consumed_batch
    state = {"raised": False}

    def crash_once(event_ids):
        if not state["raised"]:
            state["raised"] = True
            raise RuntimeError("injected rules receipt failure")
        return original_mark(event_ids)

    monkeypatch.setattr(store, "mark_evidence_consumed_batch", crash_once)
    with pytest.raises(EvidenceProjectionError, match="rules receipt failure"):
        projector.project(migration_id="batch")
    assert len(store.list_evidence_outbox(migration_id="batch", unconsumed=True)) == 100
    monkeypatch.setattr(store, "mark_evidence_consumed_batch", original_mark)
    assert projector.project(migration_id="batch") == {"seen": 100, "consumed": 100, "pending": 0}
    assert sink.batches == [100, 100]


def test_rules_projector_adapts_coordinator_evidence_store_closure(tmp_path: Path):
    store = RuleV2Store(tmp_path)
    evidence = EvidenceStore(tmp_path)
    _seed(store, 2)

    def coordinator_sink(reference):
        # Closure shape mirrors V2 coordinator sink; projector discovers the
        # EvidenceStore and uses its one-transaction project_batch API.
        return evidence.path

    result = EvidenceProjector(store, coordinator_sink).project(migration_id="batch")
    assert result == {"seen": 2, "consumed": 2, "pending": 0}
    with evidence._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] == 2
